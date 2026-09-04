"""Mission sessions: one Pi RPC session per mission, or one session for all.

Phase 3 (``docs/PHASE3_DESIGN.md`` §5) splits the single all-in-one Pi session
into small, single-purpose *missions*: a Builder that writes
``src/app-config.ts``, a Tester that writes ``src/journeys.test.tsx``, and any
number of Repairers. Each mission is one user message (its *brief*) sent into a
session whose system prompt is byte-identical to every other mission's, so the
provider's prefix cache is hit from the second request onwards.

Two session strategies, both measured (§8), selected by the caller:

- ``per-mission`` (default): a fresh :class:`~harness.pirpc.PiRpc` per mission,
  Builder ∥ Tester started :data:`STAGGER_S` apart so the second request finds
  the first's prefix already cached. Each session is closed as soon as its
  mission settles, which keeps the context small and the cost flat.
- ``single``: one session for the whole run, missions sent as consecutive
  prompts. Sequential by construction (one agent, one turn at a time).

Rules this module inherits from the rest of the harness and must not break:

- nothing here writes to stdout; Pi's own lines are forwarded by
  :func:`harness.pirpc.forward_record` and everything else goes through
  :mod:`harness.log`;
- every blocking wait is bounded and stop-event aware, so a SIGTERM aimed at
  the harness is observed inside the runner's 5 s grace;
- ``on_event`` observers and the :class:`~harness.budget.BudgetController` are
  driven from worker threads in parallel mode, so every call into them is
  serialised here under one lock (:attr:`MissionRunner._budget_lock`). That is
  what makes ``__main__``'s existing ``_UsageObserver`` and the report watcher
  safe without changing them.

:func:`resume_after_transient_errors` is the resume loop that used to live in
``harness/__main__.py`` as ``_resume_after_transient_errors``; it is kept here
unchanged apart from two additions -- an optional lock around the controller
calls, and the session label in its log line -- so the single session and every
mission session behave identically. ``__main__`` keeps a thin alias to it,
which is why its constants are duplicated here rather than imported: this
module must never import ``__main__``.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .budget import BudgetController
from .log import error as log_error, log, warn
from .pirpc import PiRpc, PiRpcError, PiRpcInterrupted, base_args, pi_env

#: Predicted output tokens per role (PHASE3_DESIGN.md §5). Measured shapes: the
#: seed ``app-config.ts`` is ~60 lines and the journey test file ~150, so the
#: Builder's single write is the smaller of the two; a Repairer applies the
#: smallest edit that fixes one failure and is smaller again.
BUILDER_PREDICTED_OUTPUT_TOKENS = 1500
TESTER_PREDICTED_OUTPUT_TOKENS = 1800
REPAIRER_PREDICTED_OUTPUT_TOKENS = 1200

#: Lookup for the callers that build a :class:`MissionSpec` from a role name
#: (``PREDICTED_OUTPUT_TOKENS.get(role, BUILDER_PREDICTED_OUTPUT_TOKENS)``).
PREDICTED_OUTPUT_TOKENS: Dict[str, int] = {
    "builder": BUILDER_PREDICTED_OUTPUT_TOKENS,
    "tester": TESTER_PREDICTED_OUTPUT_TOKENS,
    "repairer": REPAIRER_PREDICTED_OUTPUT_TOKENS,
    # Both files in one prompt (HARNESS_SESSION_MODE=combined): the two
    # single-file predictions, since the output is the same two files.
    "combined": BUILDER_PREDICTED_OUTPUT_TOKENS + TESTER_PREDICTED_OUTPUT_TOKENS,
}

#: A mission session gets exactly the tools its brief needs. ``read`` is kept
#: for the Repairer (it has to see the file it edits); the Builder and Tester
#: are told in their brief not to read anything.
DEFAULT_TOOLS = "read,write,edit"

#: Reserved out of the run deadline for abort + shutdown. Same value (and the
#: same reason) as ``__main__.SHUTDOWN_RESERVE_S``.
SHUTDOWN_RESERVE_S = 30.0

#: Reserved on top of that for the observe() that follows every mission: tsc +
#: vitest + (once) a vite build, all of which run after the last session ends.
OBSERVE_RESERVE_S = 30.0

#: Grace for the ``abort`` acknowledgement (``agent_settled`` or the response).
ABORT_GRACE_S = 5.0

#: Shutdown budget when the harness itself was signalled: the runner escalates
#: SIGTERM to SIGKILL after 5 s, so everything here must fit inside that.
FAST_CLOSE = {"stdin_grace": 0.5, "term_grace": 1.5, "kill_grace": 1.0}

#: Gap between two parallel missions' first requests. The provider only caches a
#: prefix once it has *answered* a request carrying it, so starting the second
#: session immediately would pay the full prefix twice.
STAGGER_S = 1.5

#: No join or sleep here blocks for longer than this at a stretch.
JOIN_SLICE_S = 0.25

#: Once the stop event has fired, the join polls this finely instead: the whole
#: wind-down (0.5 s stdin grace, then SIGTERM) is under a second, and a quarter
#: of that spent in one last join slice is a quarter the runner's 5 s grace
#: does not get back.
STOP_POLL_S = 0.05

#: After the stop event fires, worker threads are given this long to finish
#: their own fast close (they reap their child) before the caller gives up.
STOP_JOIN_S = 4.0

#: Whole budget :meth:`MissionRunner.close` may spend on *all* the sessions a
#: worker thread left behind, shared between them: by the time one exists,
#: STOP_JOIN_S of the runner's 5 s SIGTERM grace is already gone.
LEFTOVER_CLOSE_S = 1.5

#: The one session's role name in ``single`` mode -- it is not a Builder, it is
#: the Phase 2 all-in-one agent.
SINGLE_SESSION_ROLE = "agent"

#: Pi's agent-level auto-retry stays ON by default (it carried the measured
#: baseline through transient 5xx responses). ``HARNESS_PI_AUTO_RETRY=0`` turns
#: it off for experiments.
PI_AUTO_RETRY_ENV = "HARNESS_PI_AUTO_RETRY"

#: Resume policy: identical to the one ``__main__`` used for the single session.
RESUME_MAX_ATTEMPTS = 3
RESUME_BACKOFF_S = (5.0, 10.0, 20.0)
RESUME_MIN_BUDGET_S = 60.0
RESUME_PREDICTED_OUTPUT_TOKENS = 3000
RESUME_PROMPT = (
    "The previous model call failed with a transient provider error and the run was "
    "interrupted. Continue the task from exactly where you left off. Do not start over "
    "and do not repeat work that is already done."
)
TRANSIENT_ERROR = re.compile(
    r"(?<!\d)(5\d\d|429|408)(?!\d)|overload|rate.?limit|unavailable|time.?out|"
    r"econn|epipe|socket hang up|terminated|network|fetch failed",
    re.IGNORECASE,
)

_LABEL_SAFE = re.compile(r"[^a-z0-9]+")

PathLike = Union[str, "os.PathLike[str]"]
EventCallback = Callable[[Dict[str, Any]], None]


def pi_auto_retry_enabled() -> bool:
    return os.environ.get(PI_AUTO_RETRY_ENV, "1").strip() != "0"


def is_transient_error(text: str) -> bool:
    return bool(TRANSIENT_ERROR.search(text or ""))


def resume_policy() -> Dict[str, Any]:
    """Resume limits; the ``HARNESS_RESUME_*`` overrides exist for the fake-Pi tests."""
    try:
        attempts = int(os.environ.get("HARNESS_RESUME_ATTEMPTS", str(RESUME_MAX_ATTEMPTS)))
    except ValueError:
        attempts = RESUME_MAX_ATTEMPTS
    raw_backoff = os.environ.get("HARNESS_RESUME_BACKOFF_S", "")
    backoff: List[float] = []
    for piece in raw_backoff.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            backoff.append(max(0.0, float(piece)))
        except ValueError:
            backoff = []
            break
    try:
        min_budget = float(os.environ.get("HARNESS_RESUME_MIN_BUDGET_S", str(RESUME_MIN_BUDGET_S)))
    except ValueError:
        min_budget = RESUME_MIN_BUDGET_S
    return {
        "attempts": max(0, attempts),
        "backoff": tuple(backoff) or RESUME_BACKOFF_S,
        "min_budget": max(0.0, min_budget),
    }


def budget_gate_reason(
    controller: BudgetController, predicted_output_tokens: int, accept_partial: bool = False
) -> Optional[str]:
    """``None`` when the mission may start, else the refusal reason."""
    ok, reason = controller.can_start(predicted_output_tokens, accept_partial=accept_partial)
    return None if ok else reason


def resume_after_transient_errors(
    client: PiRpc,
    result: Dict[str, Any],
    deadline: float,
    stop_event: threading.Event,
    *,
    controller: Optional[BudgetController] = None,
    gate_active: bool = False,
    on_event: Optional[EventCallback] = None,
    budget_lock: Optional[threading.Lock] = None,
) -> Dict[str, Any]:
    """Follow up a run that settled on a transient provider error, within budget.

    Each attempt waits a backoff (polled in <=0.25 s slices so SIGTERM is still
    observed), then sends :data:`RESUME_PROMPT` into the same session. The
    returned result is the latest prompt's, with ``success`` carried forward if
    any earlier prompt already produced a usable assistant turn.

    When ``controller`` is given and ``gate_active``, each attempt also asks
    ``BudgetController.can_start(RESUME_PREDICTED_OUTPUT_TOKENS,
    accept_partial=False)`` (C6) before sending; a refusal stops resuming (it
    does not fail the run -- whatever the session already produced stands).

    ``budget_lock`` is the only addition to the version this was moved from: in
    parallel mode two mission threads share one controller, so the caller passes
    the lock that serialises every other call into it.
    """
    policy = resume_policy()
    attempts = 0
    while (
        not stop_event.is_set()
        and result.get("settled")
        and result.get("stop_reason") == "error"
        and is_transient_error(str(result.get("error") or ""))
        and attempts < policy["attempts"]
    ):
        backoff = policy["backoff"][min(attempts, len(policy["backoff"]) - 1)]
        remaining = deadline - time.monotonic() - SHUTDOWN_RESERVE_S - backoff
        if remaining < policy["min_budget"]:
            log(
                "harness",
                "not resuming after '{0}': {1:.0f}s of budget left".format(
                    result.get("error"), max(0.0, remaining)
                ),
            )
            break
        if controller is not None and gate_active:
            with _optional_lock(budget_lock):
                refusal = budget_gate_reason(
                    controller, RESUME_PREDICTED_OUTPUT_TOKENS, accept_partial=False
                )
            if refusal is not None:
                log("budget", "not resuming: {0}".format(refusal))
                break
        attempts += 1
        log(
            "harness",
            "{0} · transient provider error '{1}' · resuming in {2:.0f}s (attempt {3}/{4})".format(
                client.label, result.get("error"), backoff, attempts, policy["attempts"]
            ),
        )
        waited = 0.0
        while waited < backoff and not stop_event.is_set():
            slice_s = min(JOIN_SLICE_S, backoff - waited)
            stop_event.wait(slice_s)
            waited += slice_s
        if stop_event.is_set():
            result["interrupted"] = True
            break
        previous_success = bool(result.get("success"))
        mission: Optional[int] = None
        if controller is not None:
            with _optional_lock(budget_lock):
                mission = controller.begin_mission(
                    "resume-{0}".format(attempts), RESUME_PREDICTED_OUTPUT_TOKENS
                )
        try:
            follow = client.prompt(RESUME_PROMPT, timeout=max(1.0, remaining), on_event=on_event)
        except PiRpcInterrupted:
            result["interrupted"] = True
            break
        except PiRpcError as exc:
            result["error"] = str(exc)
            break
        if controller is not None and mission is not None:
            follow_usage = follow.get("usage")
            with _optional_lock(budget_lock):
                controller.end_mission(
                    mission,
                    getattr(follow_usage, "output", 0) or 0,
                    follow.get("wall_s", 0.0) or 0.0,
                )
        follow["success"] = bool(follow.get("success")) or previous_success
        result = follow
    result["resume_attempts"] = attempts
    return result


#: ``__main__`` aliases its private name to this one; keep both spellings.
_resume_after_transient_errors = resume_after_transient_errors


def _optional_lock(lock: Optional[threading.Lock]) -> Any:
    """The lock, or a no-op context manager when the caller has no lock."""
    return lock if lock is not None else contextlib.nullcontext()


def close_session(client: PiRpc, settled: bool, stop_event: threading.Event) -> None:
    """Close one session: abort if it never settled, then stdin EOF. Never raises."""
    try:
        if stop_event.is_set():
            # The runner escalates to SIGKILL after 5 s -- no time for an abort
            # handshake. Closing stdin still lets Pi flush its stdout.
            client.close(**FAST_CLOSE)
            return
        if not settled:
            if not client.abort(grace=ABORT_GRACE_S):
                warn(
                    "{0}: abort was not acknowledged within {1:.0f}s".format(
                        client.label, ABORT_GRACE_S
                    )
                )
        client.close()
    except Exception as exc:  # noqa: BLE001 - shutdown must not mask the result
        log_error("{0}: shutdown problem: {1}".format(client.label, exc))
        try:
            client.close(**FAST_CLOSE)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class MissionSpec:
    """One mission: a role, the user message that is its whole instruction."""

    role: str
    brief: str
    predicted_output: int
    tools: Optional[str] = DEFAULT_TOOLS
    accept_partial: bool = False


@dataclass
class MissionResult:
    """What one mission produced. ``skipped_reason`` means it never started."""

    role: str
    label: str
    session_dir: pathlib.Path
    settled: bool = False
    success: bool = False
    interrupted: bool = False
    timed_out: bool = False
    error: Optional[str] = None
    stop_reason: Optional[str] = None
    output_tokens: int = 0
    wall_s: float = 0.0
    resume_attempts: int = 0
    skipped_reason: Optional[str] = None
    text: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe shape for ``harness/missions.json``.

        ``text`` is dropped: the assistant's last message is already in
        ``events.jsonl`` and the session file, and a mission's whole point is
        that its answer is the file it wrote, not its prose.
        """
        data = asdict(self)
        data.pop("text", None)
        data["session_dir"] = str(self.session_dir)
        data["wall_s"] = round(self.wall_s, 3)
        return data


def _empty_outcome() -> Dict[str, Any]:
    return {
        "settled": False,
        "success": False,
        "interrupted": False,
        "timed_out": False,
        "error": None,
        "stop_reason": None,
        "wall_s": 0.0,
        "usage": None,
        "text": "",
    }


def _scaled_close(deadline: float) -> Dict[str, float]:
    """:data:`FAST_CLOSE`, scaled to what is left of a shared close deadline.

    Past the deadline every grace is zero, which is not "wait forever": each
    step still sends its signal and polls once, so the sequence degrades to
    stdin EOF, SIGTERM, SIGKILL back to back.
    """
    left = max(0.0, deadline - time.monotonic())
    budget = sum(FAST_CLOSE.values())
    factor = 1.0 if left >= budget else (left / budget if budget > 0 else 0.0)
    return {name: grace * factor for name, grace in FAST_CLOSE.items()}


def _slug(role: str) -> str:
    """A session-directory-safe role name (``1-builder``)."""
    cleaned = _LABEL_SAFE.sub("-", str(role or "").strip().lower()).strip("-")
    return cleaned or "mission"


class MissionRunner:
    """Runs missions as Pi sessions, one per mission or all in one session.

    Thread-safe: :meth:`run` may be called concurrently (that is what
    :meth:`run_parallel` does). Labels, the results list, the live-session
    registry, and every call into the budget controller or an ``on_event``
    observer are serialised.
    """

    def __init__(
        self,
        *,
        pi_binary: PathLike,
        app_directory: PathLike,
        harness_directory: PathLike,
        session_root: PathLike,
        append_system: Optional[str],
        extensions: Optional[List[PathLike]] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        thinking: str = "off",
        env: Optional[Dict[str, str]] = None,
        stop_event: Optional[threading.Event] = None,
        controller: Optional[BudgetController] = None,
        gate_active: bool = False,
        on_event: Optional[EventCallback] = None,
        deadline: Optional[float] = None,
        session_mode: str = "per-mission",
        first_session_index: int = 1,
    ) -> None:
        self.pi_binary = pathlib.Path(pi_binary)
        self.app_directory = pathlib.Path(app_directory)
        self.harness_directory = pathlib.Path(harness_directory)
        self.session_root = pathlib.Path(session_root)
        self.append_system = append_system
        self.extensions: List[PathLike] = list(extensions or [])
        self.provider = provider
        self.model = model
        # PHASE3_DESIGN §9: never a Pi session with thinking on in missions
        # mode. The run-level level comes from CHALLENGE_THINKING, which the
        # organizers set, so it is forced here rather than trusted -- a mission
        # is a transcription task and thinking tokens on it are pure cost.
        self.requested_thinking = str(thinking or "off").strip().lower() or "off"
        self.thinking = "off"
        if self.requested_thinking != "off":
            warn(
                "missions run with thinking off (requested {0}): "
                "PHASE3_DESIGN §9".format(self.requested_thinking)
            )
        self.env: Dict[str, str] = dict(env) if env is not None else pi_env()
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.controller = controller
        self.gate_active = bool(gate_active)
        self.deadline = float(deadline) if deadline is not None else time.monotonic() + 900.0
        self.session_mode = "single" if str(session_mode).strip() == "single" else "per-mission"
        self.results: List[MissionResult] = []

        self._index = int(first_session_index)
        self._lock = threading.Lock()  # labels, results, the live registry
        self._budget_lock = threading.Lock()  # the controller and every observer
        self._raw_on_event = on_event
        self._live: Dict[str, PiRpc] = {}
        self._shared: Optional[PiRpc] = None
        self._shared_label = ""
        self._shared_settled = True
        self._shared_lock = threading.RLock()  # one prompt at a time in single mode

    # -- labels --------------------------------------------------------------

    def next_label(self, role: str) -> str:
        """The next session directory name, and consume it: ``1-builder``.

        One label per *Pi session*, not per mission: in ``single`` mode it is
        called exactly once, for ``1-agent``.
        """
        with self._lock:
            label = "{0}-{1}".format(self._index, _slug(role))
            self._index += 1
            return label

    def peek_label(self, role: str) -> str:
        """What :meth:`next_label` would return, without consuming it.

        A mission the budget refuses never spawns a session, so it must not
        burn a session number either -- but its result still names the
        directory that would have held it.
        """
        with self._lock:
            return "{0}-{1}".format(self._index, _slug(role))

    # -- budget and observers (worker threads call these) -------------------

    def _dispatch(self, event: Dict[str, Any]) -> None:
        callback = self._raw_on_event
        if callback is None:
            return
        with self._budget_lock:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 - an observer never sinks a session
                warn("mission on_event observer failed: {0}".format(exc))

    def _on_event(self) -> Optional[EventCallback]:
        return self._dispatch if self._raw_on_event is not None else None

    def _gate(self, mission: MissionSpec) -> Optional[str]:
        if not self.gate_active or self.controller is None:
            return None
        with self._budget_lock:
            return budget_gate_reason(
                self.controller, mission.predicted_output, mission.accept_partial
            )

    def _begin(self, label: str, predicted: int) -> Optional[int]:
        if self.controller is None:
            return None
        with self._budget_lock:
            return self.controller.begin_mission(label, predicted)

    def _end(self, index: Optional[int], output: int, wall: float) -> None:
        if self.controller is None or index is None:
            return
        with self._budget_lock:
            self.controller.end_mission(index, output, wall)

    # -- session plumbing ----------------------------------------------------

    def prompt_budget(self) -> float:
        """Per-mission timeout: what is left after shutdown and observe reserves."""
        remaining = self.deadline - time.monotonic() - SHUTDOWN_RESERVE_S - OBSERVE_RESERVE_S
        return max(1.0, remaining)

    def _spawn(self, label: str, session_dir: pathlib.Path, tools: Optional[str]) -> PiRpc:
        arguments = base_args(
            append_system=self.append_system,
            session_dir=session_dir,
            extensions=list(self.extensions),
            skill=None,  # never a --skill in missions mode (PHASE3_DESIGN §9)
            tools=tools,
            provider=self.provider,
            model=self.model,
            thinking=self.thinking,
        )
        client = PiRpc(
            pi_bin=self.pi_binary,
            args=arguments,
            cwd=self.app_directory,
            env=self.env,
            session_dir=session_dir,
            label=label,
            stderr_path=self.harness_directory / "{0}.stderr.log".format(label),
            stop_event=self.stop_event,
        )
        with self._lock:
            self._live[label] = client
        log(
            "session",
            "{0} · tools={1} · thinking={2} · budget={3:.0f}s".format(
                label, tools or "(default)", self.thinking, self.prompt_budget()
            ),
        )
        return client

    def _release(self, label: str) -> None:
        with self._lock:
            self._live.pop(label, None)

    def _configure(self, client: PiRpc) -> None:
        auto_retry = pi_auto_retry_enabled()
        try:
            client.set_auto_retry(
                auto_retry, timeout=min(30.0, max(1.0, self.deadline - time.monotonic()))
            )
            log("session", "{0} · pi auto-retry {1}".format(client.label, "on" if auto_retry else "off"))
        except PiRpcInterrupted:
            self.stop_event.set()
        except PiRpcError as exc:
            warn("set_auto_retry failed ({0}); Pi keeps its own retry policy".format(exc))

    def _prompt(self, client: PiRpc, label: str, mission: MissionSpec) -> Dict[str, Any]:
        """One brief into one session, plus the transient-error resume loop."""
        outcome = _empty_outcome()
        index = self._begin(label, mission.predicted_output)
        try:
            outcome = client.prompt(
                mission.brief, timeout=self.prompt_budget(), on_event=self._on_event()
            )
        except PiRpcInterrupted:
            outcome["interrupted"] = True
        except PiRpcError as exc:
            outcome["error"] = str(exc)
        finally:
            usage = outcome.get("usage")
            self._end(index, getattr(usage, "output", 0) or 0, outcome.get("wall_s", 0.0) or 0.0)
        if not outcome.get("interrupted"):
            outcome = resume_after_transient_errors(
                client,
                outcome,
                # The observe() that follows this mission still needs its slice.
                self.deadline - OBSERVE_RESERVE_S,
                self.stop_event,
                controller=self.controller,
                gate_active=self.gate_active,
                on_event=self._on_event(),
                budget_lock=self._budget_lock,
            )
        return outcome

    @staticmethod
    def _fill(result: MissionResult, outcome: Dict[str, Any]) -> MissionResult:
        usage = outcome.get("usage")
        result.settled = bool(outcome.get("settled"))
        result.success = bool(outcome.get("success"))
        result.interrupted = bool(outcome.get("interrupted"))
        result.timed_out = bool(outcome.get("timed_out"))
        result.error = outcome.get("error")
        result.stop_reason = outcome.get("stop_reason")
        result.output_tokens = int(getattr(usage, "output", 0) or 0)
        result.wall_s = float(outcome.get("wall_s") or 0.0)
        result.resume_attempts = int(outcome.get("resume_attempts") or 0)
        result.text = str(outcome.get("text") or "")
        return result

    def _record(self, result: MissionResult) -> MissionResult:
        with self._lock:
            self.results.append(result)
        log(
            "session",
            "{0} · settled={1} success={2} interrupted={3} output={4} {5:.1f}s{6}".format(
                result.label or result.role,
                result.settled,
                result.success,
                result.interrupted,
                result.output_tokens,
                result.wall_s,
                " skipped={0}".format(result.skipped_reason) if result.skipped_reason else "",
            ),
        )
        return result

    def _pending_identity(self, role: str) -> Tuple[str, pathlib.Path]:
        """``(label, session_dir)`` for a mission that never gets a session."""
        if self.session_mode == "single" and self._shared is not None:
            return self._shared_label, self._shared.session_dir
        label = self.peek_label(role)
        return label, self.session_root / label

    # -- the public surface --------------------------------------------------

    def run(self, mission: MissionSpec) -> MissionResult:
        """Run one mission to settlement (or refusal, timeout, interruption)."""
        refusal = self._gate(mission)
        if refusal is not None:
            label, session_dir = self._pending_identity(mission.role)
            log("budget", "{0} · mission refused: {1}".format(mission.role, refusal))
            return self._record(
                MissionResult(
                    role=mission.role,
                    label=label,
                    session_dir=session_dir,
                    skipped_reason=refusal,
                )
            )
        if self.stop_event.is_set():
            # Shutting down: never start a new session (PHASE3_DESIGN §7).
            label, session_dir = self._pending_identity(mission.role)
            return self._record(
                MissionResult(
                    role=mission.role,
                    label=label,
                    session_dir=session_dir,
                    interrupted=True,
                    skipped_reason="shutting down",
                )
            )
        if self.session_mode == "single":
            return self._run_shared(mission)
        return self._run_fresh(mission)

    def _turn(
        self, client: PiRpc, label: str, mission: MissionSpec, result: MissionResult
    ) -> None:
        """One brief into one session -- unless the stop event beat us to it."""
        if self.stop_event.is_set():
            result.interrupted = True
            return
        self._fill(result, self._prompt(client, label, mission))

    def _run_fresh(self, mission: MissionSpec) -> MissionResult:
        label = self.next_label(mission.role)
        session_dir = self.session_root / label
        result = MissionResult(role=mission.role, label=label, session_dir=session_dir)
        client: Optional[PiRpc] = None
        try:
            # Inside the try: a spawn that fails (no binary, no fd) is this
            # mission's error, not an exception the whole loop has to survive.
            client = self._spawn(label, session_dir, mission.tools)
            self._configure(client)
            self._turn(client, label, mission, result)
        except Exception as exc:  # noqa: BLE001 - one mission never crashes the run
            result.error = "{0}: {1}".format(type(exc).__name__, exc)
        finally:
            self._release(label)
            if client is not None:
                close_session(client, result.settled, self.stop_event)
        return self._record(result)

    def _run_shared(self, mission: MissionSpec) -> MissionResult:
        with self._shared_lock:
            result = MissionResult(
                role=mission.role, label=self._shared_label, session_dir=self.session_root
            )
            try:
                client = self._shared
                if client is None:
                    label = self.next_label(SINGLE_SESSION_ROLE)
                    client = self._spawn(label, self.session_root / label, mission.tools)
                    self._shared = client
                    self._shared_label = label
                    self._configure(client)
                result.label = self._shared_label
                result.session_dir = client.session_dir
                self._turn(client, self._shared_label, mission, result)
            except Exception as exc:  # noqa: BLE001
                result.error = "{0}: {1}".format(type(exc).__name__, exc)
            self._shared_settled = result.settled
        return self._record(result)

    def run_parallel(
        self, missions: List[MissionSpec], stagger_s: float = STAGGER_S
    ) -> List[MissionResult]:
        """Start every mission ``stagger_s`` apart and collect all results.

        In ``single`` mode there is one agent, so the missions are sent as
        consecutive prompts instead. In ``per-mission`` mode each gets its own
        thread; the caller joins in :data:`JOIN_SLICE_S` slices so a SIGTERM is
        still observed, and once the stop event fires the workers get
        :data:`STOP_JOIN_S` to finish their own fast close (which is what reaps
        their children) before the results are returned as ``interrupted``.
        """
        pending = list(missions)
        if not pending:
            return []
        if self.session_mode == "single":
            return [self.run(mission) for mission in pending]

        results: List[Optional[MissionResult]] = [None] * len(pending)
        threads: List[threading.Thread] = []
        for position, mission in enumerate(pending):
            if position and stagger_s > 0:
                self._wait(stagger_s)
            if self.stop_event.is_set():
                break
            worker = threading.Thread(
                target=self._worker,
                args=(position, mission, results),
                name="mission-{0}".format(_slug(mission.role)),
                daemon=True,
            )
            threads.append(worker)
            worker.start()

        grace_deadline: Optional[float] = None
        slice_s = JOIN_SLICE_S
        while True:
            alive = [worker for worker in threads if worker.is_alive()]
            if not alive:
                break
            if self.stop_event.is_set():
                slice_s = STOP_POLL_S
                if grace_deadline is None:
                    grace_deadline = time.monotonic() + STOP_JOIN_S
                elif time.monotonic() >= grace_deadline:
                    warn(
                        "{0} mission thread(s) still running after {1:.0f}s; "
                        "returning interrupted results".format(len(alive), STOP_JOIN_S)
                    )
                    break
            alive[0].join(timeout=slice_s)

        for position, mission in enumerate(pending):
            if results[position] is None:
                label, session_dir = self._pending_identity(mission.role)
                results[position] = self._record(
                    MissionResult(
                        role=mission.role,
                        label=label,
                        session_dir=session_dir,
                        interrupted=True,
                        skipped_reason="shutting down",
                    )
                )
        return [result for result in results if result is not None]

    def _worker(
        self, position: int, mission: MissionSpec, results: List[Optional[MissionResult]]
    ) -> None:
        try:
            results[position] = self.run(mission)
        except Exception as exc:  # noqa: BLE001 - a thread's crash is its result
            label, session_dir = self._pending_identity(mission.role)
            results[position] = self._record(
                MissionResult(
                    role=mission.role,
                    label=label,
                    session_dir=session_dir,
                    error="{0}: {1}".format(type(exc).__name__, exc),
                )
            )

    def _wait(self, seconds: float) -> None:
        """A bounded, stop-aware sleep."""
        waited = 0.0
        while waited < seconds and not self.stop_event.is_set():
            slice_s = min(JOIN_SLICE_S, seconds - waited)
            self.stop_event.wait(slice_s)
            waited += slice_s

    def close(self) -> None:
        """Close whatever is still open. Idempotent; never raises.

        In ``single`` mode this is what ends the one session. In ``per-mission``
        mode every session is already closed by its own :meth:`run`, so this is
        a no-op unless a worker died between spawning and closing.
        """
        with self._shared_lock:
            client = self._shared
            label = self._shared_label
            settled = self._shared_settled
            self._shared = None
        if client is not None:
            self._release(label)
            close_session(client, settled, self.stop_event)
        with self._lock:
            leftover = list(self._live.items())
            self._live.clear()
        # One shared deadline for *all* the leftovers. A leftover only exists
        # when a worker thread outlived STOP_JOIN_S (4 s of the runner's 5 s
        # grace already spent), so closing two of them at FAST_CLOSE's full
        # 0.5 + 1.5 + 1.0 s each would guarantee the SIGKILL.
        deadline = time.monotonic() + LEFTOVER_CLOSE_S
        for stale_label, stale in leftover:
            warn("closing leftover session {0}".format(stale_label))
            try:
                stale.close(**_scaled_close(deadline))
            except Exception:  # noqa: BLE001
                pass

    def sessions(self) -> List[str]:
        """Labels of every session this runner created, in creation order."""
        with self._lock:
            seen: List[str] = []
            for result in self.results:
                if result.label and result.label not in seen and not result.skipped_reason:
                    seen.append(result.label)
            return seen
