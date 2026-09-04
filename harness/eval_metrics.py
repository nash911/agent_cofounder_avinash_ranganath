"""Offline metrics for one snapshotted challenge run. **No subprocesses here.**

:mod:`harness.eval` owns the *running* of a case; this module owns the
*reading* of what the run left behind. The split exists because everything in
here is a pure function over a directory of files, which makes the arithmetic
(the points formula, the gate predicate) and the readiness proxies testable
without spawning anything.

Two kinds of numbers live here.

**Measured facts** come from ``result.json``: the status, the gate, the token
counts and the cost. The gate is the organizer's bar restated: the file parses,
``status == "success"``, and every ``harness_checks`` entry passed. An *empty*
``harness_checks`` list fails the gate on purpose -- ``all([])`` is ``True`` in
Python, but a run whose app was never typechecked, tested, built and started is
exactly the run this evaluation exists to catch.

**Readiness proxies** come from static analysis of the generated ``src/``
tree. They are proxies, not verdicts: a config with four ``fields`` and ten
``it(`` blocks is *evidence* that the pipeline understood the idea, not proof.
They are cheap, deterministic and comparable across cases, which is what a
blind evaluation needs from them.

The markdown rendering lives here too, at the bottom: turning the numbers into
a table is the same kind of pure, testable work as computing them, and keeping
it out of :mod:`harness.eval` leaves that module about the run loop alone.

One proxy needs its rationale spelled out. The repository-boundary check asks
whether ``localStorage`` appears outside ``src/lib/repository.ts`` -- but the
*scaffold* legitimately touches ``localStorage`` in its storage adapter, its
error boundary and its test setup. Flagging those would make the boolean
constant-true and therefore useless, so the check only inspects the files the
agent actually wrote or changed relative to ``app-template/src/``.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

#: Points weight the organizer's efficiency metric puts on each token class.
POINTS_OUTPUT_WEIGHT = 3.0
POINTS_CACHE_READ_WEIGHT = 0.1

#: Snapshot layout written by :mod:`harness.eval` (see its module docstring).
CONFIG_RELATIVE = "app-config.ts"
TESTS_RELATIVE = "journeys.test.tsx"
REPOSITORY_RELATIVE = "lib/repository.ts"

#: The ``app-config.ts`` arrays whose entry counts stand in for "how much of
#: the idea reached the configuration".
CONFIG_ARRAYS = ("fields", "filters", "badges", "summary", "actions")

_STRING_OPENERS = "\"'`"


def points(input_tokens: float, output_tokens: float, cache_read_tokens: float) -> float:
    """``input + 3*output + 0.1*cache_read`` -- the efficiency number we track."""
    return (
        float(input_tokens)
        + POINTS_OUTPUT_WEIGHT * float(output_tokens)
        + POINTS_CACHE_READ_WEIGHT * float(cache_read_tokens)
    )


def read_json(path: pathlib.Path) -> Optional[Any]:
    """Parse ``path`` as JSON. Any failure -- missing, unreadable, malformed --
    is a ``None``, because an evaluation run must survive a broken artifact."""
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def read_text(path: pathlib.Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


# --------------------------------------------------------------------------
# result.json
# --------------------------------------------------------------------------


def gate(result: Optional[Any]) -> Tuple[bool, str]:
    """The organizer's bar. Returns ``(passed, reason)``; the reason is only
    interesting when it did not pass, and it goes straight into the report."""
    if not isinstance(result, dict):
        return False, "result.json missing or not a JSON object"
    status = result.get("status")
    checks = result.get("harness_checks")
    if status != "success":
        return False, "status is {0!r}, not \"success\"".format(status)
    if not isinstance(checks, list) or not checks:
        return False, "harness_checks is empty -- the app was never verified"
    failed = [
        str(entry.get("journey", "?"))
        for entry in checks
        if not isinstance(entry, dict) or entry.get("result") != "passed"
    ]
    if failed:
        return False, "harness checks failed: {0}".format("; ".join(failed))
    return True, ""


def result_metrics(result: Optional[Any]) -> Dict[str, Any]:
    """Usage, cost and test counts, defaulted so a broken run still tabulates."""
    passed, reason = gate(result)
    record: Dict[str, Any] = {
        "gate": passed,
        "gate_reason": reason,
        "status": None,
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cost_total": 0.0,
        "points": 0.0,
        "tests_run": 0,
        "tests_failed": 0,
        "harness_checks": 0,
        "harness_checks_failed": 0,
        "pi_exit_code": None,
    }
    if not isinstance(result, dict):
        return record
    record["status"] = result.get("status")
    record["model_calls"] = _int(result.get("model_calls"))
    record["input_tokens"] = _int(result.get("input_tokens"))
    record["output_tokens"] = _int(result.get("output_tokens"))
    record["cache_read_tokens"] = _int(result.get("cache_read_tokens"))
    record["cost_total"] = round(_number(result.get("cost_total")), 6)
    record["points"] = round(
        points(record["input_tokens"], record["output_tokens"], record["cache_read_tokens"]), 1
    )
    if isinstance(result.get("pi_exit_code"), int):
        record["pi_exit_code"] = result["pi_exit_code"]
    for source, count_key, failed_key in (
        ("tests_run", "tests_run", "tests_failed"),
        ("harness_checks", "harness_checks", "harness_checks_failed"),
    ):
        entries = result.get(source)
        if isinstance(entries, list):
            record[count_key] = len(entries)
            record[failed_key] = sum(
                1
                for entry in entries
                if not isinstance(entry, dict) or entry.get("result") != "passed"
            )
    return record


# --------------------------------------------------------------------------
# app-config.ts
# --------------------------------------------------------------------------


def _skip_string(text: str, index: int) -> int:
    """Index just past the string literal that starts at ``text[index]``.

    Scanning past strings is what keeps a ``"]"`` inside a label from closing
    an array section early.
    """
    quote = text[index]
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return index


def array_section(text: str, key: str) -> Optional[str]:
    """The body of a ``<key>: [ ... ]`` literal, brackets excluded.

    ``None`` when the key is absent or its bracket never closes.
    """
    match = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"\s*:\s*\[", text)
    if match is None:
        return None
    start = match.end()
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char in _STRING_OPENERS:
            index = _skip_string(text, index)
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start:index]
        index += 1
    return None


def iter_entries(section: str) -> Iterator[str]:
    """Each top-level ``{ ... }`` object literal inside an array body."""
    depth = 0
    start = 0
    index = 0
    while index < len(section):
        char = section[index]
        if char in _STRING_OPENERS:
            index = _skip_string(section, index)
            continue
        if char in "{[(":
            if char == "{" and depth == 0:
                start = index
            depth += 1
        elif char in "}])":
            depth -= 1
            if char == "}" and depth == 0:
                yield section[start : index + 1]
        index += 1


def count_entries(text: str, key: str) -> int:
    section = array_section(text, key)
    return 0 if section is None else sum(1 for _ in iter_entries(section))


def config_metrics(text: Optional[str]) -> Dict[str, Any]:
    """Shape proxies for ``src/app-config.ts``: how much configuration exists."""
    record: Dict[str, Any] = {
        "config_present": text is not None,
        "config_lines": 0,
        "exported_consts": 0,
        "number_fields": 0,
        "number_fields_validated": 0,
        "numeric_validation": False,
    }
    for key in CONFIG_ARRAYS:
        record[key] = 0
    if text is None:
        return record
    record["config_lines"] = len(text.splitlines())
    record["exported_consts"] = len(re.findall(r"(?m)^export\s+const\s+\w+", text))
    for key in CONFIG_ARRAYS:
        record[key] = count_entries(text, key)
    fields = array_section(text, "fields")
    if fields is not None:
        for entry in iter_entries(fields):
            if re.search(r"kind\s*:\s*[\"']number[\"']", entry) is None:
                continue
            record["number_fields"] += 1
            if re.search(r"\bmin\s*:", entry) or re.search(r"\binteger\s*:", entry):
                record["number_fields_validated"] += 1
    record["numeric_validation"] = record["number_fields_validated"] > 0
    return record


# --------------------------------------------------------------------------
# journeys.test.tsx
# --------------------------------------------------------------------------


def tests_metrics(text: Optional[str]) -> Dict[str, Any]:
    """Test-file proxies: size, journey count and assertion density.

    Density is ``expect(`` per ``it(``. A journey that renders and asserts
    nothing still counts as a test to vitest; density is what separates the
    two.
    """
    record: Dict[str, Any] = {
        "test_file_present": text is not None,
        "test_lines": 0,
        "test_its": 0,
        "test_expects": 0,
        "assertion_density": 0.0,
    }
    if text is None:
        return record
    record["test_lines"] = len(text.splitlines())
    record["test_its"] = len(re.findall(r"\bit\s*\(", text))
    record["test_expects"] = len(re.findall(r"\bexpect\s*\(", text))
    if record["test_its"]:
        record["assertion_density"] = round(record["test_expects"] / record["test_its"], 2)
    return record


# --------------------------------------------------------------------------
# the generated src/ tree
# --------------------------------------------------------------------------


def _relative_files(root: pathlib.Path) -> List[str]:
    if not root.is_dir():
        return []
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file()
    )


def tree_metrics(src_dir: pathlib.Path, template_src: pathlib.Path) -> Dict[str, Any]:
    """What the agent changed, and whether the scaffold's guarantees survived.

    ``changed_files`` is the honest measure of the agent's footprint: a healthy
    run adds ``journeys.test.tsx`` and rewrites ``app-config.ts`` and nothing
    else. Anything longer is worth a human look.
    """
    record: Dict[str, Any] = {
        "src_files": 0,
        "changed_files": [],
        "changed_file_count": 0,
        "localstorage_outside_repository": False,
        "localstorage_files": [],
        "has_empty_state": False,
        "has_error_boundary": False,
        "has_aria_label": False,
    }
    names = _relative_files(src_dir)
    record["src_files"] = len(names)
    if not names:
        return record

    changed: List[str] = []
    offenders: List[str] = []
    for name in names:
        current = src_dir / name
        try:
            body = current.read_bytes()
        except OSError:
            continue
        original = template_src / name
        try:
            same = original.is_file() and original.read_bytes() == body
        except OSError:
            same = False
        if same:
            continue
        changed.append(name)
        # Only agent-authored files can violate the repository boundary; the
        # scaffold's own storage adapter uses localStorage by design.
        if name != REPOSITORY_RELATIVE and b"localStorage" in body:
            offenders.append(name)

    record["changed_files"] = changed
    record["changed_file_count"] = len(changed)
    record["localstorage_files"] = offenders
    record["localstorage_outside_repository"] = bool(offenders)

    haystack = []
    for name in names:
        text = read_text(src_dir / name)
        if text is not None:
            haystack.append(text)
    joined = "\n".join(haystack)
    record["has_empty_state"] = "EmptyState" in joined
    record["has_error_boundary"] = "ErrorBoundary" in joined
    record["has_aria_label"] = "aria-label" in joined
    return record


# --------------------------------------------------------------------------
# harness-owned artifacts inside the moved run directory
# --------------------------------------------------------------------------


def harness_metrics(harness_dir: pathlib.Path) -> Dict[str, Any]:
    """``spec.json`` / ``supervisor.json`` / ``missions.json``, when present.

    These say *how* the pipeline got there -- how many journeys the Analyst
    planned, how many repairs the Supervisor needed, how many Pi sessions ran.
    A run in single-session mode has none of them, hence every default.
    """
    record: Dict[str, Any] = {
        "spec_journeys": None,
        "spec_fields": None,
        "repairs": None,
        "final_action": None,
        "sessions": None,
    }
    spec = read_json(harness_dir / "spec.json")
    if isinstance(spec, dict):
        for key, target in (("journeys", "spec_journeys"), ("fields", "spec_fields")):
            if isinstance(spec.get(key), list):
                record[target] = len(spec[key])
    supervisor = read_json(harness_dir / "supervisor.json")
    if isinstance(supervisor, dict):
        if isinstance(supervisor.get("repairs"), int):
            record["repairs"] = supervisor["repairs"]
        if isinstance(supervisor.get("final_action"), str):
            record["final_action"] = supervisor["final_action"]
    missions = read_json(harness_dir / "missions.json")
    if isinstance(missions, dict) and isinstance(missions.get("sessions"), list):
        record["sessions"] = len(missions["sessions"])
    return record


# --------------------------------------------------------------------------
# the whole snapshot
# --------------------------------------------------------------------------


def snapshot_metrics(snapshot_dir: pathlib.Path, template_src: pathlib.Path) -> Dict[str, Any]:
    """Every metric for one snapshotted run, in one flat dictionary.

    Flat on purpose: the report's markdown table and the baseline comparison
    both want to name a metric with a single key.
    """
    snapshot_dir = pathlib.Path(snapshot_dir)
    result = read_json(snapshot_dir / "result.json")
    if result is None:
        # The runner mirrors result.json into the app directory; if the root
        # copy never landed, the mirror is still a faithful record of the run.
        result = read_json(snapshot_dir / "app.result.json")
    src_dir = snapshot_dir / "app-src"

    record: Dict[str, Any] = {}
    record.update(result_metrics(result))
    record.update(config_metrics(read_text(src_dir / CONFIG_RELATIVE)))
    record.update(tests_metrics(read_text(src_dir / TESTS_RELATIVE)))
    record.update(tree_metrics(src_dir, pathlib.Path(template_src)))
    record.update(harness_metrics(snapshot_dir / "run" / "harness"))
    record["report_partial_present"] = (snapshot_dir / "report.partial.json").is_file()
    return record

# --------------------------------------------------------------------------
# the markdown report
# --------------------------------------------------------------------------


def _row(cells) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _num(value: Any, digits: int = 0) -> str:
    try:
        return "{0:,.{1}f}".format(float(value), digits)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return "-"


def _table(headers: Sequence[str], rows) -> List[str]:
    """A markdown table (header, rule, body) plus the blank line after it."""
    return [_row(headers), _row(["---"] * len(headers))] + [_row(row) for row in rows] + [""]


def _yes(value: Any) -> str:
    return "yes" if value else "no"


#: The per-case table: a heading and how to read one aggregate.
CASE_COLUMNS = (
    ("Case", lambda name, row: name),
    ("Runs", lambda name, row: row["runs"]),
    ("Gate passed", lambda name, row: "{0} / {1} ({2:.0%})".format(
        row["gate_passed"], row["runs"], row["gate_pass_rate"])),
    ("Points (mean)", lambda name, row: _num(row["points_mean"])),
    ("Points (min-max)", lambda name, row: "{0}-{1}".format(
        _num(row["points_min"]), _num(row["points_max"]))),
    ("Wall (mean)", lambda name, row: "{0} s".format(_num(row["wall_s_mean"], 1))),
    ("Model calls", lambda name, row: _num(row["model_calls_mean"], 1)),
    ("Journeys tested", lambda name, row: _num(row["tests_run_mean"], 1)),
)

#: The per-run table -- the evidence behind every aggregate above.
RUN_COLUMNS = (
    ("Case", lambda run: run["case"]),
    ("Run", lambda run: run["repeat"]),
    ("Gate", lambda run: "pass" if run.get("gate") else "**FAIL**"),
    ("Status", lambda run: run.get("status") or "-"),
    ("Points", lambda run: _num(run.get("points"))),
    ("Wall", lambda run: "{0} s".format(_num(run.get("wall_s"), 1))),
    ("Calls", lambda run: run.get("model_calls", 0)),
    ("Cost (USD)", lambda run: _num(run.get("cost_total"), 4)),
    ("Journeys", lambda run: run.get("tests_run", 0)),
    ("Assertions per journey", lambda run: _num(run.get("assertion_density"), 1)),
    ("Config lines", lambda run: run.get("config_lines", 0)),
    ("Files changed", lambda run: run.get("changed_file_count", 0)),
    ("Why it failed", lambda run: run.get("gate_reason") or "-"),
)

#: The readiness proxies, in reading order. Every one should be "yes" except
#: the last, which reports a leak and should be "no".
PROXY_COLUMNS = (
    ("Config written", "config_present"),
    ("Numeric validation", "numeric_validation"),
    ("Empty state", "has_empty_state"),
    ("Error boundary", "has_error_boundary"),
    ("Accessible names", "has_aria_label"),
    ("Storage leak", "localstorage_outside_repository"),
)


def render_markdown(report: Dict[str, Any]) -> str:
    """The report as a page someone can read without opening the JSON."""
    runs = report["runs"]
    cases = report["cases"]
    failed = [run for run in runs if not run.get("gate")]
    lines: List[str] = [
        "# Blind evaluation - {0}".format(report.get("label") or "unlabelled"),
        "",
        "{0} - agent `{1}` - {2} case(s) x {3} repeat(s) = {4} run(s)".format(
            report["generated_at"], report["agent"], len(cases), report["repeats"], len(runs)),
        "",
    ]
    if not runs:
        return "\n".join(lines + ["**Verdict:** no runs were executed.", ""]) + "\n"
    lines.append("**Verdict: {0}.**".format(
        "{0} of {1} runs did NOT pass the gate".format(len(failed), len(runs)) if failed
        else "all {0} runs passed the gate".format(len(runs))))
    if report.get("baseline_path"):
        regressions = report.get("regressions") or []
        lines.append("")
        if regressions:
            lines.append("**Regressions against the baseline:**")
            lines.extend("- " + line for line in regressions)
        else:
            lines.append("No regression against the baseline.")
    lines.append("")

    lines += ["## Per case", ""]
    lines += _table(
        [heading for heading, _ in CASE_COLUMNS],
        [[cell(name, cases[name]) for _, cell in CASE_COLUMNS] for name in sorted(cases)],
    )
    lines += ["## Every run", ""]
    lines += _table(
        [heading for heading, _ in RUN_COLUMNS],
        [[cell(run) for _, cell in RUN_COLUMNS] for run in runs],
    )
    lines += [
        "## Readiness proxies",
        "",
        "Static checks on the generated app. Every column should read `yes` "
        "except *storage leak*, which should read `no`.",
        "",
    ]
    lines += _table(
        ["Case", "Run"] + [heading for heading, _ in PROXY_COLUMNS],
        [[run["case"], run["repeat"]] + [_yes(run.get(key)) for _, key in PROXY_COLUMNS]
         for run in runs],
    )
    lines.append("*Points* is `input + 3 x output + 0.1 x cache read` - the efficiency number "
                 "the challenge scores. *Gate* is the organizer's bar: the run reported success "
                 "and every harness check (tests, build, dev server) passed.")
    return "\n".join(lines) + "\n"
