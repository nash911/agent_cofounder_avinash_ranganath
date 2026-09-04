"""Bounded, stop-aware subprocess execution for the harness's own checks.

``subprocess.run(..., timeout=…)`` is bounded but not interruptible: it blocks
until the child exits or its own timeout fires. Measured consequence -- a
SIGTERM that lands one second into an observation whose ``tsc`` sleeps 12 s
took 11.05 s to shut the harness down, against the runner's 5 s
SIGTERM-to-SIGKILL grace, and the run lost ``supervisor.json``,
``missions.json``, ``budget.json`` and the final report.

Everything here polls in :data:`POLL_SLICE_S` slices and reaps the child as
soon as the caller's stop event fires, so an in-flight typecheck, test run or
build can never hold the shutdown past that grace.

Output is captured through a temporary file rather than a pipe: the callers
run node tool-chains that emit more than a pipe buffer holds, and a pipe with
no reader deadlocks exactly when the timeout is supposed to save us.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

#: Longest any wait in this module blocks at a stretch. Small enough that a
#: stop event costs at most this much of the 5 s shutdown grace.
POLL_SLICE_S = 0.1

#: Wind-down for a child that has to go (timeout or shutdown): SIGTERM, then
#: SIGKILL. Both graces are inside the shutdown budget on purpose.
TERM_GRACE_S = 0.5
KILL_GRACE_S = 0.5

OK = "ok"
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"
ERROR = "error"


@dataclass
class Completed:
    """One finished (or killed) child process. Never carries an exception."""

    status: str  # "ok" | "timeout" | "interrupted" | "error"
    returncode: Optional[int] = None
    output: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """The child ran to completion -- say nothing about its exit code."""
        return self.status == OK


def _sleep(stop_event: Optional[threading.Event], seconds: float) -> None:
    """Wait ``seconds``, returning early once ``stop_event`` is set."""
    if seconds <= 0:
        return
    if stop_event is not None:
        stop_event.wait(seconds)
        return
    time.sleep(seconds)


def _reap(proc: "subprocess.Popen") -> Optional[int]:
    """SIGTERM, then SIGKILL, both bounded. Returns the exit code, if any."""
    for signal_name, grace in (("terminate", TERM_GRACE_S), ("kill", KILL_GRACE_S)):
        try:
            getattr(proc, signal_name)()
        except (OSError, ValueError):  # pragma: no cover - already gone
            pass
        try:
            return proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        except OSError:  # pragma: no cover - the child is unreachable
            return None
    return proc.poll()


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: str,
    timeout_s: float,
    stop_event: Optional[threading.Event] = None,
    capture: bool = True,
) -> Completed:
    """Run ``argv`` with a wall-clock bound and a stop event. Never raises.

    ``capture`` false discards the child's output (the vitest run writes its
    report to a file and its stdout is noise worth neither the memory nor the
    temporary file).
    """
    timeout_s = max(0.0, float(timeout_s or 0.0))
    if timeout_s <= 0:
        return Completed(TIMEOUT, None, "", "no time left in the budget")
    if stop_event is not None and stop_event.is_set():
        return Completed(INTERRUPTED, None, "", "shutting down")

    sink = tempfile.TemporaryFile() if capture else None
    try:
        try:
            proc = subprocess.Popen(
                [str(item) for item in argv],
                cwd=str(cwd),
                stdout=sink if sink is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            return Completed(ERROR, None, "", str(exc))

        status = OK
        deadline = time.monotonic() + timeout_s
        while True:
            code = proc.poll()
            if code is not None:
                break
            if stop_event is not None and stop_event.is_set():
                status = INTERRUPTED
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = TIMEOUT
                break
            _sleep(stop_event, min(POLL_SLICE_S, remaining))
        if status != OK:
            code = _reap(proc)

        output = ""
        if sink is not None:
            try:
                sink.seek(0)
                output = sink.read().decode("utf-8", "replace")
            except (OSError, ValueError):  # pragma: no cover - the file is ours
                output = ""
        return Completed(status, code, output, "" if status == OK else status)
    finally:
        if sink is not None:
            try:
                sink.close()
            except OSError:  # pragma: no cover
                pass
