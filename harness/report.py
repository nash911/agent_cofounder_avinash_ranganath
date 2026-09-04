"""Harness-authored ``report.partial.json`` (BUILD_PLAN.md rev 6, §1, C7).

The model's own report, if any, is authoritative -- this module exists so a
run that never gets there (killed on the deadline, or one whose tests never
went green) still leaves a valid ``partial`` report describing whatever
state the app is actually in. It observes reality directly (running vitest
itself) rather than trusting anything Pi said about its own progress.

Two write paths, both funnelled through :func:`write_report`, which never
clobbers a report the model wrote more recently than the observation started:

- live: :class:`ReportWatcher` watches ``tool_execution_end`` events for a
  bash call whose output looks like a green ``vitest`` summary, and kicks off
  at most one background observation per rolling window.
- shutdown: the harness calls :meth:`ReportWatcher.final_observe` once, capped
  at a short timeout, when the session ended abnormally or no report exists.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
from typing import Any, Dict, List, Optional, Union

from .log import warn
from .proc import run_bounded

PathLike = Union[str, "os.PathLike[str]"]

DEFAULT_OBSERVE_TIMEOUT_S = 60.0
DEFAULT_MIN_INTERVAL_S = 60.0
DEFAULT_FINAL_TIMEOUT_S = 15.0

#: How much of a failing test's first assertion message survives into an
#: observation. Long enough for the "Unable to find an element with the text:
#: ..." messages that drive a repair (measured: ~300 chars including the
#: printed DOM head), short enough that ten of them still fit a repair brief.
MAX_FAILURE_MESSAGE_CHARS = 600

_EMPTY_OBSERVATION: Dict[str, Any] = {
    "green": False,
    "total": 0,
    "passed": 0,
    "failed": 0,
    "names": [],
    "failures": [],
}


def empty_observation() -> Dict[str, Any]:
    """A fresh "nothing observed" vitest summary.

    Public because :mod:`harness.observe` needs exactly this shape for the
    runs where vitest is deliberately never spawned (tsc red, no time left).
    """
    return {"green": False, "total": 0, "passed": 0, "failed": 0, "names": [], "failures": []}


#: Vitest's default reporter line: "      Tests  3 passed (3)" (green) or
#: "Tests  1 failed | 2 passed (3)" (red). Matching "N passed" and then
#: separately rejecting any "failed" substring covers both shapes without
#: depending on exact column spacing.
_GREEN_PATTERN = re.compile(r"Tests\s+\d+\s+passed", re.IGNORECASE)
_FAILED_PATTERN = re.compile(r"\bfailed\b", re.IGNORECASE)


def is_green_vitest_summary(text: str) -> bool:
    """True for bash output whose vitest summary reports only passing tests."""
    if not text:
        return False
    if not _GREEN_PATTERN.search(text):
        return False
    return not _FAILED_PATTERN.search(text)


def _extract_bash_result_text(event: Dict[str, Any]) -> str:
    """The concatenated text blocks of a ``tool_execution_end`` bash result."""
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def vitest_binary(app_dir: PathLike) -> pathlib.Path:
    """``HARNESS_VITEST_BIN`` overrides for tests; otherwise the app's own binary."""
    override = os.environ.get("HARNESS_VITEST_BIN")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(app_dir) / "node_modules" / ".bin" / "vitest"


def observe(
    app_dir: PathLike,
    harness_dir: PathLike,
    timeout_s: float = DEFAULT_OBSERVE_TIMEOUT_S,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Run vitest once against ``app_dir`` and summarize the JSON reporter output.

    Never raises: a spawn failure, a timeout, or an unparsable report all
    fold into the same "not green, nothing observed" shape so a flaky
    observation never takes the harness down.

    ``stop_event``, when given, kills the vitest child as soon as it is set:
    a test run is the longest thing the harness does with no model in the
    loop, and it must not hold a shutdown past the runner's 5 s grace.
    """
    app_dir = pathlib.Path(app_dir)
    harness_dir = pathlib.Path(harness_dir)
    try:
        harness_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn("report observe: cannot create {0}: {1}".format(harness_dir, exc))
        return empty_observation()

    output_file = harness_dir / "vitest.json"
    binary = vitest_binary(app_dir)
    argv = [
        str(binary),
        "run",
        "--reporter=json",
        "--outputFile={0}".format(output_file),
        "--passWithNoTests=false",
    ]
    completed = run_bounded(
        argv,
        cwd=str(app_dir),
        timeout_s=max(0.1, float(timeout_s)),
        stop_event=stop_event,
        capture=False,
    )
    if completed.status == "timeout":
        warn("report observe: vitest did not finish within {0:.0f}s".format(timeout_s))
        return empty_observation()
    if completed.status == "interrupted":
        warn("report observe: vitest stopped; the harness is shutting down")
        return empty_observation()
    if completed.status == "error":
        warn("report observe: could not run {0}: {1}".format(binary, completed.error))
        return empty_observation()

    try:
        data = json.loads(output_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn("report observe: could not read {0}: {1}".format(output_file, exc))
        return empty_observation()
    if not isinstance(data, dict):
        return empty_observation()

    total = int(data.get("numTotalTests") or 0)
    passed = int(data.get("numPassedTests") or 0)
    failed = int(data.get("numFailedTests") or 0)
    names: List[str] = []
    failures: List[Dict[str, str]] = []
    for suite in data.get("testResults") or []:
        if not isinstance(suite, dict):
            continue
        for assertion in suite.get("assertionResults") or []:
            if not isinstance(assertion, dict):
                continue
            name = assertion.get("fullName") or assertion.get("title") or ""
            if not name:
                continue
            if assertion.get("status") == "passed":
                names.append(str(name))
            else:
                # Everything that is not a pass -- failed, but also skipped and
                # todo, which the runner rejects just as hard -- is something a
                # Repairer has to know about, so all of it lands here.
                failures.append({"name": str(name), "message": _failure_message(assertion)})

    green = failed == 0 and passed > 0
    return {
        "green": green,
        "total": total,
        "passed": passed,
        "failed": failed,
        "names": names,
        "failures": failures,
    }


def _failure_message(assertion: Dict[str, Any]) -> str:
    """The first assertion message of a non-passing test, capped.

    Vitest repeats the same expectation across ``failureMessages``; the first
    entry is the one that names the missing element or the mismatched value,
    and the rest is stack.
    """
    messages = assertion.get("failureMessages")
    if isinstance(messages, str):
        first = messages
    elif isinstance(messages, list) and messages:
        first = messages[0] if isinstance(messages[0], str) else str(messages[0])
    else:
        first = ""
    return first[:MAX_FAILURE_MESSAGE_CHARS]


def _first_sentence(text: str) -> str:
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return ""
    match = re.search(r"[.!?](\s|$)", collapsed)
    if match:
        return collapsed[: match.start() + 1].strip()
    return collapsed[:240].strip()


def compose_report(
    spec: Optional[Dict[str, Any]],
    observation: Dict[str, Any],
    idea_text: str,
    *,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``report.partial.json`` payload from an observation.

    ``spec`` (``harness/spec.json``'s dict, when the analyst produced one) is
    consulted field by field -- a spec with only ``summary`` still improves
    the summary while ``implemented_features``/``assumptions`` fall back to
    empty, rather than an all-or-nothing choice.

    ``status`` defaults to ``partial``, which is what the Phase-2 single
    session path has always written: the harness only ever *observed* a run
    it did not drive. Missions mode drives the whole run and does know when
    the app is finished, so it passes ``success`` (or ``failed``); anything
    the runner would not accept (``src/result.ts``) falls back to ``partial``.
    """
    if spec:
        summary = spec.get("summary") or spec.get("tagline") or _first_sentence(idea_text)
        implemented_features = list(spec.get("implemented_features") or [])
        assumptions = list(spec.get("assumptions") or [])
    else:
        summary = _first_sentence(idea_text)
        implemented_features = []
        assumptions = []

    return {
        "status": status if status in VALID_STATUSES else "partial",
        "summary": summary,
        "implemented_features": implemented_features,
        "assumptions": assumptions,
        # Passed journeys first, then the failed ones: a report that admits a
        # failed journey is worth far more than one that silently drops it
        # (AGENTS.md: "If a journey failed ... record it as failed").
        "tests_run": tests_run_from_observation(observation) + failed_tests_run(observation),
    }


_MISSING = object()


def _mtime_or_none(path: pathlib.Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


#: sha256 of the bytes the harness last wrote to each report path, in-process.
#: Consulted (with the sidecar below) before any overwrite: a report whose
#: bytes the harness did not produce is the model's, and the model's report is
#: authoritative -- it carries ``status: success`` and the real feature and
#: assumption lists, which a harness-authored ``partial`` must never replace.
_LAST_WRITTEN: Dict[str, str] = {}
_SIDECAR_NAME = "report.harness.sha256"


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: pathlib.Path) -> Optional[str]:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _recorded_hash(report_path: pathlib.Path, harness_dir: Optional[PathLike]) -> Optional[str]:
    recorded = _LAST_WRITTEN.get(str(report_path))
    if recorded is not None or harness_dir is None:
        return recorded
    try:
        return (pathlib.Path(harness_dir) / _SIDECAR_NAME).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def harness_authored(report_path: PathLike, harness_dir: Optional[PathLike] = None) -> bool:
    """True when the report on disk is byte-identical to the harness's last write."""
    path = pathlib.Path(report_path)
    current = _sha256_file(path)
    if current is None:
        return False
    recorded = _recorded_hash(path, harness_dir)
    return recorded is not None and recorded == current


def write_report(
    app_dir: PathLike,
    spec: Optional[Dict[str, Any]],
    observation: Dict[str, Any],
    idea_text: str,
    *,
    expected_mtime: Any = _MISSING,
    harness_dir: Optional[PathLike] = None,
    status: Optional[str] = None,
) -> bool:
    """Write ``report.partial.json`` unless the model's own report is on disk.

    Two guards, both of which make the call a no-op:

    - authorship: an existing report whose bytes the harness did not write is
      the model's, and is never replaced (measured 2026-09-03: the model wrote
      its ``success`` report, then re-ran its tests; the green re-run
      triggered an observation that clobbered the model's report with a
      harness ``partial`` and degraded the run's status);
    - race: ``expected_mtime`` is the report's mtime captured *before* the
      (possibly slow) observation ran; if it moved since, the model wrote
      during the observation and that write wins.

    ``harness_dir`` lets the authorship record survive in a sidecar file next
    to the harness's other artifacts, in addition to the in-process record.
    ``status`` is passed straight to :func:`compose_report` (default
    ``partial``); missions mode is the only caller that knows better.
    """
    report_path = pathlib.Path(app_dir) / "report.partial.json"
    if expected_mtime is _MISSING:
        expected_mtime = _mtime_or_none(report_path)
    if _mtime_or_none(report_path) != expected_mtime:
        warn("report.partial.json changed during observe(); not overwriting (model's write wins)")
        return False
    if report_path.exists() and not harness_authored(report_path, harness_dir):
        warn("report.partial.json was written by the model; leaving it untouched")
        return False

    payload = compose_report(spec, observation, idea_text, status=status)
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    tmp_path = report_path.with_name(report_path.name + ".harness-tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(str(tmp_path), str(report_path))
    except OSError as exc:
        warn("could not write report.partial.json: {0}".format(exc))
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False
    digest = _sha256_bytes(data)
    _LAST_WRITTEN[str(report_path)] = digest
    if harness_dir is not None:
        try:
            (pathlib.Path(harness_dir) / _SIDECAR_NAME).write_text(digest + "\n", encoding="utf-8")
        except OSError:
            pass
    return True


VALID_RESULTS = ("passed", "failed")

#: The three statuses ``normalizeResult`` in ``src/result.ts`` accepts;
#: anything else is coerced to ``failed`` there, so the harness never emits one.
VALID_STATUSES = ("success", "partial", "failed")


def valid_tests_run(value: Any) -> List[Dict[str, str]]:
    """The entries the runner will keep: ``{command, journey, result}`` with a
    string command and journey and a ``result`` of ``passed``/``failed`` --
    mirroring ``normalizeTestRun`` in ``src/result.ts``. Anything else (the
    measured slip was ``{name, status}``) is dropped by the runner, which then
    degrades a fully green run to ``partial`` for want of a reported journey."""
    kept: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return kept
    for item in value:
        if not isinstance(item, dict):
            continue
        command, journey, result = item.get("command"), item.get("journey"), item.get("result")
        if isinstance(command, str) and isinstance(journey, str) and result in VALID_RESULTS:
            kept.append({"command": command, "journey": journey, "result": str(result)})
    return kept


def tests_run_from_observation(observation: Dict[str, Any]) -> List[Dict[str, str]]:
    """The ``passed`` entries only -- :func:`repair_tests_run`'s whole input,
    since it never fires on anything but a green observation."""
    return [
        {"command": "npm test", "journey": str(name), "result": "passed"}
        for name in (observation.get("names") or [])
    ]


def failed_tests_run(observation: Dict[str, Any]) -> List[Dict[str, str]]:
    """The ``failed`` entries, in the runner's own shape.

    The failure message is deliberately *not* carried into ``journey``: the
    runner shows that string as the journey's name, and a stack trace there
    reads as a broken report. The message stays in the observation, where the
    Repairer's brief picks it up.
    """
    entries: List[Dict[str, str]] = []
    for failure in observation.get("failures") or []:
        if not isinstance(failure, dict):
            continue
        name = failure.get("name")
        if isinstance(name, str) and name:
            entries.append({"command": "npm test", "journey": name, "result": "failed"})
    return entries


def repair_tests_run(
    app_dir: PathLike,
    harness_dir: Optional[PathLike],
    observation: Dict[str, Any],
) -> bool:
    """Fill a model-authored report's ``tests_run`` from a green observation.

    Only fires when the report on disk carries **no** runner-valid
    ``tests_run`` entry and vitest just passed with at least one test. Every
    other field the model wrote (status, summary, features, assumptions) is
    preserved byte-for-byte in value; only ``tests_run`` changes.
    """
    report_path = pathlib.Path(app_dir) / "report.partial.json"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if valid_tests_run(payload.get("tests_run")):
        return False
    if not observation.get("green") or not observation.get("names"):
        return False
    payload["tests_run"] = tests_run_from_observation(observation)
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    tmp_path = report_path.with_name(report_path.name + ".harness-tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(str(tmp_path), str(report_path))
    except OSError as exc:
        warn("could not repair report.partial.json: {0}".format(exc))
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False
    digest = _sha256_bytes(data)
    _LAST_WRITTEN[str(report_path)] = digest
    if harness_dir is not None:
        try:
            (pathlib.Path(harness_dir) / _SIDECAR_NAME).write_text(digest + "\n", encoding="utf-8")
        except OSError:
            pass
    return True


class ReportWatcher:
    """Live vitest-green detector plus the single-flight observe/write it drives.

    ``on_event`` is meant to be passed straight through as :meth:`PiRpc.prompt`'s
    ``on_event`` callback; every event the session emits passes through it, and
    everything but a green bash ``tool_execution_end`` is ignored.
    """

    def __init__(
        self,
        app_dir: PathLike,
        harness_dir: PathLike,
        idea_text: str,
        spec: Optional[Dict[str, Any]] = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        observe_timeout_s: float = DEFAULT_OBSERVE_TIMEOUT_S,
        final_timeout_s: float = DEFAULT_FINAL_TIMEOUT_S,
    ) -> None:
        self.app_dir = pathlib.Path(app_dir)
        self.harness_dir = pathlib.Path(harness_dir)
        self.idea_text = idea_text
        self.spec = spec
        self.min_interval_s = min_interval_s
        self.observe_timeout_s = observe_timeout_s
        self.final_timeout_s = final_timeout_s

        self._lock = threading.Lock()
        self._last_started = 0.0
        self._thread: Optional[threading.Thread] = None
        self.observations: List[Dict[str, Any]] = []

    # -- live path ----------------------------------------------------------

    def on_event(self, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict) or event.get("type") != "tool_execution_end":
            return
        if event.get("toolName") != "bash":
            return
        if not is_green_vitest_summary(_extract_bash_result_text(event)):
            return
        self._maybe_start()

    def _maybe_start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            now = time.monotonic()
            if now - self._last_started < self.min_interval_s:
                return False
            self._last_started = now
            thread = threading.Thread(
                target=self._run_observe, name="report-observe", daemon=True
            )
            self._thread = thread
        thread.start()
        return True

    def _run_observe(self) -> None:
        report_path = self.app_dir / "report.partial.json"
        before = _mtime_or_none(report_path)
        try:
            observation = observe(self.app_dir, self.harness_dir, timeout_s=self.observe_timeout_s)
        except Exception as exc:  # noqa: BLE001 - a background observer must never crash the run
            warn("report observe failed: {0}".format(exc))
            return
        self.observations.append(observation)
        if observation.get("green"):
            write_report(
                self.app_dir, self.spec, observation, self.idea_text,
                expected_mtime=before, harness_dir=self.harness_dir,
            )

    # -- shutdown path --------------------------------------------------

    def join(self, timeout_s: float) -> None:
        """Wait, in <=0.25s slices, for an in-flight observation to finish."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + max(0.0, timeout_s)
        while thread.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(timeout=min(0.25, remaining))

    def repair_model_report(self) -> Optional[int]:
        """After a normal settle: if the model's report has no runner-valid
        ``tests_run`` entry, observe once (capped) and fill it from vitest.
        Returns the number of entries written, or ``None`` when nothing needed
        repair (harness-authored report, valid entries, no report, or red)."""
        report_path = self.app_dir / "report.partial.json"
        if not report_path.is_file() or harness_authored(report_path, self.harness_dir):
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or valid_tests_run(payload.get("tests_run")):
            return None
        observation = self.final_observe()
        if repair_tests_run(self.app_dir, self.harness_dir, observation):
            return len(observation.get("names") or [])
        return None

    def final_observe(self) -> Dict[str, Any]:
        """One last, capped, synchronous observation at shutdown.

        Writes only when this last check is green, exactly like the live
        path: it exists to catch a pass that happened too close to shutdown
        for the live watcher's rolling window, not to fabricate a "partial"
        report over an app whose tests never passed.
        """
        self.join(2.0)
        report_path = self.app_dir / "report.partial.json"
        before = _mtime_or_none(report_path)
        observation = observe(self.app_dir, self.harness_dir, timeout_s=self.final_timeout_s)
        self.observations.append(observation)
        if observation.get("green"):
            write_report(
                self.app_dir, self.spec, observation, self.idea_text,
                expected_mtime=before, harness_dir=self.harness_dir,
            )
        return observation
