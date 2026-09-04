"""``python3 -m harness.eval`` -- the blind evaluation runner (BUILD_PLAN §4).

The public idea is the one prompt this build has been tuned against, so it is
the one prompt that cannot tell us whether the pipeline generalises. This
module answers that question: it drives the *real* runner over a directory of
unseen idea files, several times each, and reduces every run to one row of
numbers that a person can read and a script can diff.

Four constraints shape the design.

**Holdout ideas never enter the working tree.** ``--cases`` is required, has no
default, must be absolute, and must resolve outside the repository root. The
same goes for ``--output-root`` and ``--report-dir``. A holdout that lives in
the repository is a holdout that leaks into a commit, a container image or a
prompt; refusing the path is the only reliable guard, so this is an exit-2
error rather than a warning.

**Runs are sequential, always.** The generated app binds port 3000 and the
runner audits that port before and after Pi. Two concurrent runs would fight
over it and both would report nonsense, so there is no parallel mode to
misuse -- the loop is one case, one repeat, at a time.

**Cleanup happens in a ``finally``.** A run leaves four kinds of debris behind:
``result.json`` at the repository root, the mirrored copies under
``output/app``, the generated ``src/`` tree, and a fresh ``artifacts/runs/<id>``
directory. The snapshot copies all of it into ``<output-root>``, then *moves*
the run directory out of ``artifacts/runs/`` and deletes the root
``result.json``. Moving rather than copying matters: ``npm run submission``
takes the newest ``artifacts/runs/<id>`` as *the* reference run, and twenty
evaluation runs left in place would bury the real one. Only the directory that
appeared during this run is touched -- never an older one, and never
``output/app`` itself, which the runner re-seeds from ``app-template`` on its
own.

**The report is the deliverable.** Two files per invocation: a JSON report that
a later run can pass back as ``--baseline``, and a markdown twin with one table
row per case and one per run, written for someone who will not read this file.

Exit codes: ``0`` every gate passed and nothing regressed, ``1`` a gate failed
or a case regressed against the baseline, ``2`` a usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import eval_metrics
from .log import error as log_error, log, warn

#: The repository this file lives in -- the default ``--repo-root``.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULT_CHALLENGE_COMMAND = "npm run challenge --"
DEFAULT_REPEATS = 2
DEFAULT_TIMEOUT_S = 1200
REPORT_SCHEMA = "agentcofounder.eval.v1"

#: A case may cost more points than the baseline before we call it a
#: regression. Run-to-run spread on an identical prompt is real; 10% is wide
#: enough to absorb it and narrow enough to catch a prompt that got fatter.
REGRESSION_TOLERANCE = 0.10

#: SIGTERM-to-SIGKILL grace when a run overruns ``--timeout-s``.
KILL_GRACE_S = 5.0


class UsageError(Exception):
    """A bad invocation. Always becomes exit code 2, never a traceback."""


@dataclass
class Config:
    repo_root: pathlib.Path
    cases: List[pathlib.Path]
    cases_path: pathlib.Path
    repeats: int
    output_root: pathlib.Path
    report_dir: pathlib.Path
    baseline: Optional[Dict[str, Any]]
    baseline_path: Optional[pathlib.Path]
    agent: str
    command: List[str]
    label: str
    timeout_s: float
    template_src: pathlib.Path = field(init=False)

    def __post_init__(self) -> None:
        self.template_src = self.repo_root / "app-template" / "src"


# ---------------------------------------------------------------------------
# argument parsing and validation
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m harness.eval",
        description="Run the challenge over a holdout case set and report the spread.",
    )
    parser.add_argument("--cases", required=True, help="absolute path to a .txt idea file or a directory of them, outside the repository")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="runs per case (default: %(default)s)")
    parser.add_argument("--output-root", required=True, help="absolute path outside the repository for per-run snapshots")
    parser.add_argument("--report-dir", required=True, help="absolute path outside the repository for the reports")
    parser.add_argument("--baseline", default=None, help="an earlier eval-*.json to compare against")
    parser.add_argument("--agent", default="python", choices=("python", "pi"), help="orchestrator under test (default: %(default)s)")
    parser.add_argument("--challenge-command", default=DEFAULT_CHALLENGE_COMMAND, help="command run from the repository root (default: %(default)r)")
    parser.add_argument("--label", default="", help="free-text tag stored in the report")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="per-run wall-clock limit (default: %(default)s)")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="repository to run in (default: this checkout)")
    return parser


def _resolve(value: str, flag: str) -> pathlib.Path:
    """An absolute, symlink-resolved path, or a usage error naming the flag."""
    if not value:
        raise UsageError("{0} needs a value".format(flag))
    if not pathlib.Path(value).is_absolute():
        raise UsageError(
            "{0} must be an absolute path (got {1!r}); relative paths are ambiguous "
            "because the runner executes from the repository root".format(flag, value)
        )
    return pathlib.Path(os.path.realpath(value))


def _require_outside(path: pathlib.Path, repo_root: pathlib.Path, flag: str) -> None:
    if path == repo_root or repo_root in path.parents:
        raise UsageError(
            "{0} must live outside the repository ({1}); {2} is inside it, and "
            "holdout material must never enter the working tree".format(flag, repo_root, path)
        )


def discover_cases(cases_path: pathlib.Path) -> List[pathlib.Path]:
    if cases_path.is_file():
        if cases_path.suffix != ".txt":
            raise UsageError("--cases file must be a .txt idea file, got {0}".format(cases_path))
        return [cases_path]
    if cases_path.is_dir():
        found = sorted(
            item for item in cases_path.iterdir() if item.is_file() and item.suffix == ".txt"
        )
        if not found:
            raise UsageError("--cases directory holds no *.txt idea files: {0}".format(cases_path))
        return found
    raise UsageError("--cases path does not exist: {0}".format(cases_path))


def load_baseline(path: pathlib.Path) -> Dict[str, Any]:
    report = eval_metrics.read_json(path)
    if not isinstance(report, dict) or not isinstance(report.get("cases"), dict):
        raise UsageError(
            "--baseline {0} is not an eval report (no \"cases\" object)".format(path)
        )
    return report


def build_config(args: argparse.Namespace) -> Config:
    repo_root = _resolve(args.repo_root, "--repo-root")
    if not repo_root.is_dir():
        raise UsageError("--repo-root does not exist: {0}".format(repo_root))
    cases_path = _resolve(args.cases, "--cases")
    _require_outside(cases_path, repo_root, "--cases")
    output_root = _resolve(args.output_root, "--output-root")
    _require_outside(output_root, repo_root, "--output-root")
    report_dir = _resolve(args.report_dir, "--report-dir")
    _require_outside(report_dir, repo_root, "--report-dir")

    if args.repeats < 1:
        raise UsageError("--repeats must be at least 1, got {0}".format(args.repeats))
    if args.timeout_s <= 0:
        raise UsageError("--timeout-s must be positive, got {0}".format(args.timeout_s))

    command = shlex.split(args.challenge_command)
    if not command:
        raise UsageError("--challenge-command is empty")

    baseline_path = None
    baseline = None
    if args.baseline:
        baseline_path = _resolve(args.baseline, "--baseline")
        baseline = load_baseline(baseline_path)

    return Config(
        repo_root=repo_root,
        cases=discover_cases(cases_path),
        cases_path=cases_path,
        repeats=int(args.repeats),
        output_root=output_root,
        report_dir=report_dir,
        baseline=baseline,
        baseline_path=baseline_path,
        agent=args.agent,
        command=command,
        label=args.label,
        timeout_s=float(args.timeout_s),
    )


# ---------------------------------------------------------------------------
# running one case
# ---------------------------------------------------------------------------


def case_key(case: pathlib.Path) -> str:
    """A filesystem-safe directory name for one case, derived from its stem."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", case.stem).strip("-") or "case"


def _run_directory_names(repo_root: pathlib.Path) -> List[str]:
    runs = repo_root / "artifacts" / "runs"
    try:
        return sorted(item.name for item in runs.iterdir() if item.is_dir())
    except OSError:
        return []


def _kill_group(process: "subprocess.Popen[bytes]") -> Optional[int]:
    """SIGTERM then SIGKILL the whole process group.

    ``npm run challenge`` is npm → node → pi → the app's dev server; signalling
    the direct child alone would leave the grandchildren holding port 3000 and
    poison every case after this one.
    """
    try:
        pgid: Optional[int] = os.getpgid(process.pid)
    except OSError:
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:  # pragma: no cover - the child is already unreachable
                process.send_signal(sig)
        except OSError:  # pragma: no cover - already gone
            pass
        try:
            return process.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            continue
    return process.poll()


def _newest_new_run(repo_root: pathlib.Path, before: Sequence[str]) -> Optional[pathlib.Path]:
    """The run directory that appeared during this run, if exactly one did."""
    appeared = sorted(set(_run_directory_names(repo_root)) - set(before))
    if not appeared:
        return None
    if len(appeared) > 1:
        warn("more than one new run directory: {0}; keeping only the newest".format(", ".join(appeared)))
    return repo_root / "artifacts" / "runs" / appeared[-1]


def snapshot_run(config: Config, snapshot_dir: pathlib.Path, before: Sequence[str]) -> Dict[str, Any]:
    """Copy everything the run produced, then leave the repository as we found it.

    Never raises: this is called from a ``finally``, and losing the cleanup
    because a copy failed would leave the next case running against a dirty
    tree.
    """
    audit: Dict[str, Any] = {"snapshot_files": [], "run_dir": None, "cleanup_errors": []}
    app = config.repo_root / "output" / "app"
    root_result = config.repo_root / "result.json"
    for source, target in (
        (root_result, snapshot_dir / "result.json"),
        (app / "result.json", snapshot_dir / "app.result.json"),
        (app / "report.partial.json", snapshot_dir / "report.partial.json"),
        (app / "AGENTS.md", snapshot_dir / "AGENTS.md"),
    ):
        try:
            if source.is_file():
                shutil.copy2(str(source), str(target))
                audit["snapshot_files"].append(target.name)
        except OSError as exc:
            audit["cleanup_errors"].append("copy {0}: {1}".format(source.name, exc))

    try:
        if (app / "src").is_dir():
            shutil.copytree(str(app / "src"), str(snapshot_dir / "app-src"), dirs_exist_ok=True)
            audit["snapshot_files"].append("app-src")
    except OSError as exc:
        audit["cleanup_errors"].append("copy src: {0}".format(exc))

    run_dir = _newest_new_run(config.repo_root, before)
    if run_dir is None:
        warn("no new artifacts/runs directory appeared for {0}".format(snapshot_dir.name))
    else:
        target = snapshot_dir / "run"
        try:
            if target.exists():
                shutil.rmtree(str(target))
            shutil.move(str(run_dir), str(target))
            audit["run_dir"] = run_dir.name
            audit["snapshot_files"].append("run")
        except OSError as exc:
            audit["cleanup_errors"].append("move {0}: {1}".format(run_dir.name, exc))

    try:
        if root_result.is_file():
            root_result.unlink()
    except OSError as exc:  # pragma: no cover - permissions
        audit["cleanup_errors"].append("unlink result.json: {0}".format(exc))
    return audit


def run_case(config: Config, case: pathlib.Path, repeat: int) -> Dict[str, Any]:
    """One challenge invocation, snapshotted and measured. Cleanup is guaranteed."""
    snapshot_dir = config.output_root / case_key(case) / str(repeat)
    if snapshot_dir.exists():
        # Re-running into the same --output-root replaces the slot rather than
        # merging: a leftover app-src from a previous evaluation would be read
        # back as this run's generated tree.
        warn("replacing an existing snapshot at {0}".format(snapshot_dir))
        shutil.rmtree(str(snapshot_dir), ignore_errors=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    argv = list(config.command) + ["--agent", config.agent, "--idea-file", str(case)]

    before = _run_directory_names(config.repo_root)
    stale = config.repo_root / "result.json"
    try:
        if stale.is_file():
            # A leftover from an earlier run would be snapshotted as this run's.
            stale.unlink()
    except OSError as exc:  # pragma: no cover - permissions
        warn("could not remove a stale result.json: {0}".format(exc))

    record: Dict[str, Any] = {
        "case": case.stem,
        "case_key": case_key(case),
        "case_file": str(case),
        "repeat": repeat,
        "command": " ".join(shlex.quote(item) for item in argv),
        "snapshot": str(snapshot_dir),
        "exit_code": None,
        "timed_out": False,
        "wall_s": 0.0,
    }
    started = time.monotonic()
    try:
        with (snapshot_dir / "challenge.stdout.log").open("wb") as out, (
            snapshot_dir / "challenge.stderr.log"
        ).open("wb") as err:
            process = subprocess.Popen(
                argv,
                cwd=str(config.repo_root),
                # env=None inherits this process's environment untouched: the
                # credentials and PI_CODING_AGENT_DIR are the caller's business.
                env=None,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                record["exit_code"] = process.wait(timeout=config.timeout_s)
            except subprocess.TimeoutExpired:
                warn("run exceeded {0}s; killing the process group".format(config.timeout_s))
                record["exit_code"] = _kill_group(process)
                record["timed_out"] = True
            except BaseException:
                # Ctrl-C, most likely. Take the whole tree down with us before
                # the finally block cleans up.
                _kill_group(process)
                raise
    except OSError as exc:
        record["error"] = "could not start {0!r}: {1}".format(argv[0], exc)
        log_error(record["error"])
    finally:
        record["wall_s"] = round(time.monotonic() - started, 2)
        record.update(snapshot_run(config, snapshot_dir, before))

    record.update(eval_metrics.snapshot_metrics(snapshot_dir, config.template_src))
    return record


# ---------------------------------------------------------------------------
# aggregation, baseline comparison
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def aggregate(runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    points = [float(run.get("points") or 0.0) for run in runs]
    passed = sum(1 for run in runs if run.get("gate"))
    return {
        "runs": len(runs),
        "gate_passed": passed,
        "gate_pass_rate": round(passed / len(runs), 3) if runs else 0.0,
        "points_mean": _mean(points),
        "points_min": round(min(points), 1) if points else 0.0,
        "points_max": round(max(points), 1) if points else 0.0,
        "wall_s_mean": _mean([float(run.get("wall_s") or 0.0) for run in runs]),
        "model_calls_mean": _mean([float(run.get("model_calls") or 0) for run in runs]),
        "tests_run_mean": _mean([float(run.get("tests_run") or 0) for run in runs]),
        "cost_total_mean": round(_mean([float(run.get("cost_total") or 0.0) for run in runs]), 4),
    }


def compare_baseline(baseline: Dict[str, Any], cases: Dict[str, Any]) -> List[str]:
    """Human-readable regression lines; an empty list means "no regression"."""
    previous = baseline.get("cases") or {}
    regressions: List[str] = []
    for name in sorted(cases):
        old = previous.get(name)
        if not isinstance(old, dict):
            continue
        new = cases[name]
        old_rate = float(old.get("gate_pass_rate") or 0.0)
        new_rate = float(new.get("gate_pass_rate") or 0.0)
        if new_rate < old_rate - 1e-9:
            regressions.append(
                "{0}: gate pass rate fell from {1:.0%} to {2:.0%}".format(name, old_rate, new_rate)
            )
        old_points = float(old.get("points_mean") or 0.0)
        new_points = float(new.get("points_mean") or 0.0)
        if old_points > 0 and new_points > old_points * (1.0 + REGRESSION_TOLERANCE):
            regressions.append(
                "{0}: mean points rose from {1:,.0f} to {2:,.0f} (+{3:.0%}, tolerance {4:.0%})".format(
                    name, old_points, new_points,
                    (new_points - old_points) / old_points, REGRESSION_TOLERANCE,
                )
            )
    return regressions


# ---------------------------------------------------------------------------
# the report file
# ---------------------------------------------------------------------------


def write_report(config: Config, runs: List[Dict[str, Any]]) -> Tuple[pathlib.Path, Dict[str, Any]]:
    cases: Dict[str, Any] = {}
    for run in runs:
        cases.setdefault(run["case"], []).append(run)
    aggregates = {name: aggregate(entries) for name, entries in cases.items()}
    report: Dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": config.label,
        "agent": config.agent,
        "repeats": config.repeats,
        "timeout_s": config.timeout_s,
        "cases_path": str(config.cases_path),
        "repo_root": str(config.repo_root),
        "output_root": str(config.output_root),
        "challenge_command": " ".join(shlex.quote(item) for item in config.command),
        "baseline_path": str(config.baseline_path) if config.baseline_path else None,
        "runs": runs,
        "cases": aggregates,
    }
    report["regressions"] = (
        compare_baseline(config.baseline, aggregates) if config.baseline is not None else []
    )
    report["gate_failures"] = [
        "{0} run {1}: {2}".format(run["case"], run["repeat"], run.get("gate_reason") or "unknown")
        for run in runs
        if not run.get("gate")
    ]

    config.report_dir.mkdir(parents=True, exist_ok=True)
    stem = "eval-{0}".format(time.strftime("%Y%m%d-%H%M%S"))
    json_path = config.report_dir / (stem + ".json")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    markdown = eval_metrics.render_markdown(report)
    (config.report_dir / (stem + ".md")).write_text(markdown, encoding="utf-8")
    report["_markdown"] = markdown
    return json_path, report


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = build_config(args)
    except UsageError as exc:
        log_error(str(exc))
        return 2

    total = len(config.cases) * config.repeats
    log("eval", "{0} case(s) x {1} repeat(s) = {2} run(s), sequential, agent={3}".format(
        len(config.cases), config.repeats, total, config.agent))
    log("eval", "cases from {0}".format(config.cases_path))

    runs: List[Dict[str, Any]] = []
    interrupted = False
    index = 0
    try:
        for case in config.cases:
            for repeat in range(1, config.repeats + 1):
                index += 1
                log("eval", "run {0}/{1} start · case={2} repeat={3}".format(
                    index, total, case.stem, repeat))
                run = run_case(config, case, repeat)
                runs.append(run)
                log("eval", "run {0}/{1} end · case={2} repeat={3} · gate={4} "
                            "points={5:,.0f} wall={6}s exit={7}".format(
                                index, total, case.stem, repeat,
                                "pass" if run.get("gate") else "FAIL",
                                float(run.get("points") or 0.0), run.get("wall_s"),
                                run.get("exit_code")))
    except KeyboardInterrupt:
        interrupted = True
        warn("interrupted after {0} of {1} runs; writing a partial report".format(len(runs), total))

    json_path, report = write_report(config, runs)
    sys.stdout.write(report["_markdown"])
    sys.stdout.flush()
    log("eval", "report written to {0}".format(json_path))

    for line in report["gate_failures"]:
        log_error("gate failure · " + line)
    for line in report["regressions"]:
        log_error("regression · " + line)

    if interrupted or report["gate_failures"] or report["regressions"]:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
