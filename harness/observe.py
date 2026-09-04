"""``observe()`` -- the harness's own, free look at the app (PHASE3_DESIGN.md §4).

Phase 2 measured ``npm test`` and ``npm run build`` at one model call each
(run ``python-k``: calls 6 and 7, 234 input + 49 output tokens for two lines
of output the harness can produce itself). Everything here therefore runs as
a plain subprocess: typecheck, tests, build, file facts. No model, no tokens,
no network.

The output is one :class:`Observation`, which is both what the Supervisor
decides on and what the Repairer's brief is rendered from:

- ``tsc`` first, because a type error makes every vitest failure a lie about
  the app -- when it is red, vitest is skipped outright (the fast path);
- ``signature`` is what "no progress" means: the same type errors, the same
  failing test names and the same over-long files after a repair round;
- ``coverage`` is informational -- a journey with no test is a real gap, but
  a title-matching heuristic must never be what stops a green run.

Never raises. Every subprocess is bounded by the observation's own budget,
and any failure degrades to a not-green observation with the reason in it,
because a flaky look at the app must never be what takes the run down.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from . import report as report_mod
from .log import log, warn
from .proc import run_bounded

PathLike = Union[str, "os.PathLike[str]"]

#: The two files a mission is allowed to write (plan.files); everything else
#: under ``src/`` is the frozen, measured scaffold.
CONFIG_FILE = "src/app-config.ts"
TESTS_FILE = "src/journeys.test.tsx"
TRACKED_FILES = (CONFIG_FILE, TESTS_FILE)

#: ``solution/system-prompt.md``: "Never write a file longer than 150 lines."
MAX_FILE_LINES = 150

#: A repair brief carries the type errors verbatim; 40 lines is already more
#: than a model needs to find the mistake and still fits the brief's budget.
MAX_TSC_ERRORS = 40
BUILD_TAIL_LINES = 30

DEFAULT_TIMEOUT_S = 90.0
TSC_TIMEOUT_S = 60.0
VITEST_TIMEOUT_S = 60.0
BUILD_TIMEOUT_S = 90.0

#: Token-overlap threshold for the coverage fallback (over stemmed tokens, so
#: "Add a book" and "adds a book" are 1.0). A test title is written by the
#: Tester from the journey title, so an exact match is the normal case; 0.6
#: catches the rest ("Reject empty title" vs "rejects an empty title", 0.75)
#: without collapsing two genuinely different journeys onto one test.
FUZZY_MATCH_MIN = 0.6

#: ``src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable``.
#: Anchored at the start of the line so a message that merely quotes that
#: shape inside a longer explanation is not counted twice.
_TSC_ERROR_LINE = re.compile(r"^(?P<path>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\): error TS\d+:")

_OBSERVE_FILE = re.compile(r"^observe-(\d+)\.json$")

#: ``observe-<n>.json`` numbering. The Supervisor loop is sequential, but the
#: counter is shared state either way, so it is taken under a lock and cached
#: per harness directory rather than re-derived from a directory listing that
#: a concurrent writer could be halfway through.
_INDEX_LOCK = threading.Lock()
_INDEXES: Dict[str, int] = {}


@dataclass
class Observation:
    """One complete, free look at the app. ``as_dict`` is what is written out."""

    tsc_ran: bool = False
    tsc_ok: bool = False
    tsc_errors: List[str] = field(default_factory=list)
    vitest: Dict[str, Any] = field(default_factory=report_mod.empty_observation)
    build_ran: bool = False
    build_ok: Optional[bool] = None
    build_tail: str = ""
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    over_limit: List[str] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=lambda: {"missing": [], "matched": 0, "total": 0})
    signature: str = ""
    green: bool = False
    elapsed_s: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tsc_ran": self.tsc_ran,
            "tsc_ok": self.tsc_ok,
            "tsc_errors": list(self.tsc_errors),
            "vitest": dict(self.vitest),
            "build_ran": self.build_ran,
            "build_ok": self.build_ok,
            "build_tail": self.build_tail,
            "files": {name: dict(facts) for name, facts in self.files.items()},
            "over_limit": list(self.over_limit),
            "coverage": dict(self.coverage),
            "signature": self.signature,
            "green": self.green,
            "elapsed_s": self.elapsed_s,
        }

    def failing_names(self) -> List[str]:
        return [
            str(failure.get("name", ""))
            for failure in (self.vitest.get("failures") or [])
            if isinstance(failure, dict) and failure.get("name")
        ]


# -- binaries ---------------------------------------------------------------


def tsc_binary(app_dir: PathLike) -> pathlib.Path:
    """``HARNESS_TSC_BIN`` overrides for tests; otherwise the app's own binary."""
    override = os.environ.get("HARNESS_TSC_BIN")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(app_dir) / "node_modules" / ".bin" / "tsc"


def vite_binary(app_dir: PathLike) -> pathlib.Path:
    """``HARNESS_VITE_BIN`` overrides for tests; otherwise the app's own binary."""
    override = os.environ.get("HARNESS_VITE_BIN")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(app_dir) / "node_modules" / ".bin" / "vite"


# -- typecheck --------------------------------------------------------------


def parse_tsc_errors(text: str) -> List[str]:
    """The ``path(line,col): error TSxxxx: message`` lines, verbatim and capped.

    Verbatim because the repair brief quotes them: the path and position are
    most of the information, and paraphrasing them costs the Repairer a read.
    """
    errors: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _TSC_ERROR_LINE.match(line.lstrip()):
            errors.append(line.strip())
            if len(errors) >= MAX_TSC_ERRORS:
                break
    return errors


def _run_tsc(
    app_dir: pathlib.Path, timeout_s: float, stop_event: Optional[threading.Event] = None
) -> Tuple[bool, bool, List[str]]:
    """``(tsc_ran, tsc_ok, errors)``. A spawn failure or timeout is not a red
    typecheck -- it is *no* typecheck, and the Supervisor must not repair
    against errors nobody produced."""
    if timeout_s <= 0:
        return False, False, ["tsc skipped: no time left in the observation budget"]
    binary = tsc_binary(app_dir)
    completed = run_bounded(
        [str(binary), "--noEmit", "-p", "."],
        cwd=str(app_dir),
        timeout_s=timeout_s,
        stop_event=stop_event,
    )
    if completed.status == "timeout":
        warn("observe · tsc did not finish within {0:.0f}s".format(timeout_s))
        return False, False, ["tsc did not finish within {0:.0f}s".format(timeout_s)]
    if completed.status == "interrupted":
        return False, False, ["tsc was stopped: the harness is shutting down"]
    if completed.status == "error":
        warn("observe · could not run {0}: {1}".format(binary, completed.error))
        return False, False, ["could not run {0}: {1}".format(binary, completed.error)]

    output = completed.output
    errors = parse_tsc_errors(output)
    if completed.returncode != 0 and not errors:
        # tsc failed on something that is not a per-file diagnostic (a missing
        # tsconfig, TS5058). The tail is all the Repairer has to go on.
        errors = _tail_lines(output, 5) or [
            "tsc exited {0} with no parsable error lines".format(completed.returncode)
        ]
    return True, completed.returncode == 0 and not errors, errors


def _skip_vitest(tsc_ran: bool, tsc_ok: bool, skip_on_tsc_error: bool) -> bool:
    """Only a *red* typecheck suppresses the test run.

    Keyed on ``tsc_ran`` as well as ``tsc_ok`` because a tsc that never
    finished has produced no type errors to be a consequence of: skipping
    vitest there would leave the observation with no evidence at all, which is
    what emptied ``tests_run`` in a measured run whose ten tests all passed.
    """
    return bool(skip_on_tsc_error and tsc_ran and not tsc_ok)


# -- tests ------------------------------------------------------------------


def _run_vitest(
    app_dir: pathlib.Path,
    harness_dir: pathlib.Path,
    timeout_s: float,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """The vitest half of the observation, delegated to :func:`report.observe`.

    One vitest runner for the whole harness: the single-session path's
    ReportWatcher and this both need the same JSON summary, and a second
    implementation would be a second thing to keep in step with the reporter.
    """
    if timeout_s <= 0:
        warn("observe · vitest skipped: no time left in the observation budget")
        return report_mod.empty_observation()
    try:
        return report_mod.observe(
            app_dir, harness_dir, timeout_s=timeout_s, stop_event=stop_event
        )
    except Exception as exc:  # noqa: BLE001 - report.observe is documented never to raise
        warn("observe · vitest failed: {0}: {1}".format(type(exc).__name__, exc))
        return report_mod.empty_observation()


# -- build ------------------------------------------------------------------


def _run_build(
    app_dir: pathlib.Path, timeout_s: float, stop_event: Optional[threading.Event] = None
) -> Tuple[bool, Optional[bool], str]:
    """``(build_ran, build_ok, tail)`` for ``vite build``; tsc is never repeated."""
    if timeout_s <= 0:
        return False, None, "build skipped: no time left in the observation budget"
    binary = vite_binary(app_dir)
    completed = run_bounded(
        [str(binary), "build"], cwd=str(app_dir), timeout_s=timeout_s, stop_event=stop_event
    )
    if completed.status == "timeout":
        warn("observe · vite build did not finish within {0:.0f}s".format(timeout_s))
        return True, False, "vite build did not finish within {0:.0f}s".format(timeout_s)
    if completed.status == "interrupted":
        return False, None, "vite build was stopped: the harness is shutting down"
    if completed.status == "error":
        warn("observe · could not run {0}: {1}".format(binary, completed.error))
        return False, None, "could not run {0}: {1}".format(binary, completed.error)
    return (
        True,
        completed.returncode == 0,
        "\n".join(_tail_lines(completed.output, BUILD_TAIL_LINES)),
    )


def _tail_lines(text: str, count: int) -> List[str]:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    return lines[-count:]


# -- files ------------------------------------------------------------------


def _line_count(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _read_bytes(path: pathlib.Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _file_facts(
    app_dir: pathlib.Path, seed_dir: Optional[pathlib.Path]
) -> Dict[str, Dict[str, Any]]:
    """``exists``/``lines``/``changed_from_seed`` for the two files a mission writes.

    A file the seed does not have at all (``src/journeys.test.tsx``) counts as
    changed as soon as it exists -- that is exactly the "did the Tester
    actually write anything?" question the Supervisor asks.
    """
    facts: Dict[str, Dict[str, Any]] = {}
    for relative in TRACKED_FILES:
        data = _read_bytes(app_dir / relative)
        seed_data = _read_bytes(seed_dir / relative) if seed_dir is not None else None
        facts[relative] = {
            "exists": data is not None,
            "lines": _line_count(data) if data is not None else 0,
            "changed_from_seed": data is not None and data != seed_data,
        }
    return facts


def _source_files(app_dir: pathlib.Path) -> List[Tuple[str, pathlib.Path]]:
    """Every ``src/**/*.ts``/``*.tsx``, as ``(posix relative path, path)``, sorted."""
    root = app_dir / "src"
    found: List[Tuple[str, pathlib.Path]] = []
    try:
        for parent, directories, names in os.walk(str(root)):
            directories[:] = sorted(d for d in directories if d != "node_modules" and not d.startswith("."))
            for name in sorted(names):
                if not name.endswith((".ts", ".tsx")):
                    continue
                path = pathlib.Path(parent) / name
                try:
                    relative = path.relative_to(app_dir).as_posix()
                except ValueError:  # pragma: no cover - os.walk stays under app_dir
                    continue
                found.append((relative, path))
    except OSError as exc:
        warn("observe · could not walk {0}: {1}".format(root, exc))
    return sorted(found)


def _over_limit(app_dir: pathlib.Path, seed_dir: Optional[pathlib.Path]) -> List[str]:
    """Files over :data:`MAX_FILE_LINES` that a mission is responsible for.

    A file byte-identical to its seed copy is excluded: the measured scaffold
    ships four files over the limit already (``lib/collection.ts`` 169,
    ``lib/config-types.ts`` 162, ``lib/repository.ts`` 156,
    ``test/helpers.tsx`` 169), and repairing what the harness itself shipped
    would burn the whole repair cap on the first observation.
    """
    over: List[str] = []
    for relative, path in _source_files(app_dir):
        data = _read_bytes(path)
        if data is None:
            continue
        if _line_count(data) <= MAX_FILE_LINES:
            continue
        if seed_dir is not None and _read_bytes(seed_dir / relative) == data:
            continue
        over.append(relative)
    return over


# -- coverage ---------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase, alphanumerics and spaces only, whitespace collapsed."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return " ".join(cleaned.split())


def _variants(name: str) -> List[str]:
    """The normalized forms a vitest name can be matched by.

    A full name is ``"journeys > adds a book"``: the suite prefix is the test
    file's ``describe`` and is not part of any journey title, so the last
    segment is what an exact match is against, with the whole string kept as a
    fallback for a test written without a ``describe``.
    """
    whole = _normalize(name)
    forms = [whole]
    if ">" in name:
        tail = _normalize(name.rsplit(">", 1)[-1])
        if tail and tail != whole:
            forms.append(tail)
    return [form for form in forms if form]


def _stem(token: str) -> str:
    """Fold the one inflection that actually shows up here: the plural/3rd
    person ``s``. The Tester writes ``it("adds a book")`` for the journey
    "Add a book" often enough that without this the fuzzy pass reports six of
    the public idea's ten journeys as uncovered (measured against
    ``scratchpad/p3/probe1/spec.json``)."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> Set[str]:
    """Stemmed word set of a normalized string -- the fuzzy pass's only input."""
    return set(_stem(word) for word in text.split())


def _jaccard(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / float(len(union))


def coverage(expected_titles: Sequence[str], observed_names: Sequence[str]) -> Dict[str, Any]:
    """``{"missing": [titles], "matched": n, "total": n}`` -- journeys with a test.

    Exact (normalized) matches are claimed first so a fuzzy near-match can
    never steal the test that belongs to another journey, and every observed
    name is claimed at most once: two journeys sharing one test is a gap, not
    two hits.
    """
    expected = [str(title).strip() for title in (expected_titles or []) if str(title).strip()]
    candidates = [_variants(str(name)) for name in (observed_names or []) if str(name).strip()]
    claimed = [False] * len(candidates)

    pending: List[str] = []
    matched = 0
    for title in expected:
        wanted = _normalize(title)
        index = _claim(candidates, claimed, lambda forms: wanted in forms)
        if index < 0:
            pending.append(title)
        else:
            matched += 1

    missing: List[str] = []
    for title in pending:
        wanted = _tokens(_normalize(title))
        index = _claim(
            candidates,
            claimed,
            lambda forms: any(_jaccard(wanted, _tokens(form)) >= FUZZY_MATCH_MIN for form in forms),
        )
        if index < 0:
            missing.append(title)
        else:
            matched += 1

    return {"missing": missing, "matched": matched, "total": len(expected)}


def _claim(
    candidates: List[List[str]], claimed: List[bool], predicate: Callable[[List[str]], bool]
) -> int:
    """The first unclaimed candidate satisfying ``predicate``; ``-1`` for none."""
    for index, forms in enumerate(candidates):
        if claimed[index]:
            continue
        if predicate(forms):
            claimed[index] = True
            return index
    return -1


def expected_titles(spec_or_plan: Optional[Dict[str, Any]]) -> List[str]:
    """Journey titles the test file is expected to cover.

    Accepts either the plan (``tests[].title``, what §4 names) or the raw spec
    (``journeys[].title``) so the caller can hand over whichever it has --
    ``derive_plan`` copies the titles across unchanged.
    """
    if not isinstance(spec_or_plan, dict):
        return []
    for key in ("tests", "journeys"):
        entries = spec_or_plan.get(key)
        if not isinstance(entries, list):
            continue
        titles = [
            str(entry.get("title")).strip()
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("title") or "").strip()
        ]
        if titles:
            return titles
    return []


# -- signature --------------------------------------------------------------


def signature(
    tsc_errors: Sequence[str], failing_names: Sequence[str], over_limit: Sequence[str]
) -> str:
    """A stable digest of *what is wrong*, for no-progress detection.

    Test names are sorted (vitest's file order is not stable across runs) but
    failure *messages* are deliberately left out: a repair that changes the
    wording of the same failure on the same test has not made progress.
    """
    payload = json.dumps(
        {
            "tsc": [str(line) for line in (tsc_errors or [])],
            "tests": sorted(str(name) for name in (failing_names or [])),
            "over_limit": sorted(str(path) for path in (over_limit or [])),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -- the observation itself -------------------------------------------------


def _next_index(harness_dir: pathlib.Path) -> int:
    key = str(harness_dir)
    with _INDEX_LOCK:
        current = _INDEXES.get(key)
        if current is None:
            current = _highest_on_disk(harness_dir)
        current += 1
        _INDEXES[key] = current
        return current


def _highest_on_disk(harness_dir: pathlib.Path) -> int:
    highest = 0
    try:
        for entry in harness_dir.iterdir():
            match = _OBSERVE_FILE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    except OSError:
        pass
    return highest


def _write_observation(observation: Observation, harness_dir: pathlib.Path) -> None:
    """Persist ``observe-<n>.json``; a write failure is logged, never raised."""
    try:
        harness_dir.mkdir(parents=True, exist_ok=True)
        path = harness_dir / "observe-{0}.json".format(_next_index(harness_dir))
        path.write_text(json.dumps(observation.as_dict(), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        warn("observe · could not write observe-<n>.json: {0}".format(exc))


def _remaining(deadline: float, cap: float) -> float:
    return min(cap, deadline - time.monotonic())


def _stopped(stop_event: Optional[threading.Event]) -> bool:
    return stop_event is not None and stop_event.is_set()


def observe(
    app_dir: PathLike,
    harness_dir: PathLike,
    *,
    seed_dir: Optional[PathLike],
    spec: Optional[Dict[str, Any]] = None,
    run_build: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    skip_vitest_on_tsc_error: bool = True,
    stop_event: Optional[threading.Event] = None,
) -> Observation:
    """Typecheck, test, (optionally) build and measure the app. Never raises.

    ``timeout_s`` bounds the *whole* observation: each step gets what is left
    of it, capped by its own limit, so a slow tsc can eat into vitest's time
    but the call as a whole still returns inside the caller's budget.

    ``stop_event`` makes the observation interruptible: each child is polled
    and killed the moment the event fires, and no further step is started
    after it. Without it a SIGTERM landing inside a 60 s typecheck was
    measured holding the shutdown for 11 s against the runner's 5 s grace.

    ``spec`` may be the plan or the spec; only journey titles are read from it.
    """
    started = time.monotonic()
    deadline = started + max(1.0, float(timeout_s or DEFAULT_TIMEOUT_S))
    app_path = pathlib.Path(app_dir)
    harness_path = pathlib.Path(harness_dir)
    seed_path = pathlib.Path(seed_dir) if seed_dir else None

    observation = Observation()
    try:
        observation.tsc_ran, observation.tsc_ok, observation.tsc_errors = _run_tsc(
            app_path, _remaining(deadline, TSC_TIMEOUT_S), stop_event
        )
        if _stopped(stop_event):
            warn("observe · stopping after the typecheck: the harness is shutting down")
        elif _skip_vitest(observation.tsc_ran, observation.tsc_ok, skip_vitest_on_tsc_error):
            # Red tsc: every vitest failure would be a consequence of a type
            # error the Repairer has to fix first, so the run is skipped
            # outright (it is also the slowest step by far).
            log("observe", "vitest skipped: {0} type error(s)".format(len(observation.tsc_errors)))
        else:
            if not observation.tsc_ran:
                log("observe", "vitest runs anyway: the typecheck never ran")
            observation.vitest = _run_vitest(
                app_path, harness_path, _remaining(deadline, VITEST_TIMEOUT_S), stop_event
            )

        if run_build and not _stopped(stop_event):
            observation.build_ran, observation.build_ok, observation.build_tail = _run_build(
                app_path, _remaining(deadline, BUILD_TIMEOUT_S), stop_event
            )

        observation.files = _file_facts(app_path, seed_path)
        observation.over_limit = _over_limit(app_path, seed_path)
        observation.coverage = coverage(
            expected_titles(spec),
            list(observation.vitest.get("names") or []) + observation.failing_names(),
        )
    except Exception as exc:  # noqa: BLE001 - an observation must never sink the run
        warn("observe · unexpected failure: {0}: {1}".format(type(exc).__name__, exc))

    observation.signature = signature(
        observation.tsc_errors, observation.failing_names(), observation.over_limit
    )
    observation.green = bool(
        observation.tsc_ok and observation.vitest.get("green") and not observation.over_limit
    )
    observation.elapsed_s = round(time.monotonic() - started, 3)
    _write_observation(observation, harness_path)
    log(
        "observe",
        "green={0} tsc={1} tests={2}/{3} build={4} over_limit={5} coverage={6}/{7} {8:.1f}s".format(
            observation.green,
            "ok" if observation.tsc_ok else "{0} error(s)".format(len(observation.tsc_errors)),
            observation.vitest.get("passed", 0),
            observation.vitest.get("total", 0),
            observation.build_ok if observation.build_ran else "skipped",
            len(observation.over_limit),
            observation.coverage.get("matched", 0),
            observation.coverage.get("total", 0),
            observation.elapsed_s,
        ),
    )
    return observation

