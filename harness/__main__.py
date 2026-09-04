"""``python3 -m harness`` -- the orchestrator.

Invoked by ``runHarness()`` in ``src/run-challenge.ts`` with an absolute idea
file, session root, app directory, repository root and a timeout that is already
strictly smaller than the runner's own deadline.

Contract:

- **stdout carries only Pi event lines**, forwarded verbatim by
  :mod:`harness.pirpc`. Everything the harness itself has to say goes to stderr.
- Exit ``0`` iff at least one assistant ``message_end`` had a ``stopReason``
  outside ``{error, aborted}`` **and** reported ``usage.output > 0``.
- Exit ``1`` for any other completed run.
- Exit ``2`` for a usage or configuration error detected *before* a session
  starts.
- Harness-owned files live under ``<dirname(session-root)>/harness/``. The five
  filenames owned by ``verifyGeneratedApp`` and ``events.jsonl`` are never
  written here.

This module owns the run's *framing* -- validation, signal handling,
credentials, the budget controller, the Analyst, the prompt-prefix check, the
budget snapshot and the exit code -- and exactly two bodies to put between them
(``PHASE3_DESIGN.md`` §7):

- ``missions`` (the default): :func:`harness.loop.run_missions` -- Builder ∥
  Tester, then observe → Supervisor → Repairer. It needs a usable spec, so it
  needs a gateway key.
- ``single``: :func:`run_single_session`, the Phase 2 all-in-one Pi session,
  moved here unchanged. It is the fallback for every run without a key or
  without a usable spec, and it is the path all of the pre-Phase-3 tests take.

``HARNESS_MODE=single`` forces the fallback; ``HARNESS_PI_BIN`` replaces the Pi
binary. Both exist for the fake-Pi tests and the integration dry run only;
nothing in a judged run should set either.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .budget import BudgetController
from .log import close_file_sink, error as log_error, log, narrate, set_file_sink, warn
from .loop import RunContext, report_spec, run_missions
from .pirpc import PiRpc, PiRpcError, PiRpcInterrupted, base_args, pi_env
from .plan import derive_plan
from . import missions as missions_mod
from . import prefix
from . import report as report_mod

#: The direct-gateway client (analyst, C4) is owned by a different part of this
#: build; its three modules may not exist yet while this file is being worked
#: on. Imported lazily, once, so a missing module degrades to "direct client
#: unavailable" instead of an import-time crash of the whole harness.
try:
    from . import credentials as _credentials  # type: ignore[attr-defined]
    from . import gateway as _gateway  # type: ignore[attr-defined]
    from . import analyst as _analyst  # type: ignore[attr-defined]

    DIRECT_CLIENT_AVAILABLE = True
    _DIRECT_IMPORT_ERROR: Optional[str] = None
except ImportError as _direct_import_exc:  # pragma: no cover - exercised before those land
    _credentials = None  # type: ignore[assignment]
    _gateway = None  # type: ignore[assignment]
    _analyst = None  # type: ignore[assignment]
    DIRECT_CLIENT_AVAILABLE = False
    _DIRECT_IMPORT_ERROR = str(_direct_import_exc)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

#: Reserved out of ``--timeout-ms`` for abort + shutdown, so the harness is gone
#: well before the runner's own timer fires. Bumped from 20.0 to 30.0 (ratified
#: deviation, PHASE1_READINESS.md) to fit the harness-authored report's capped
#: 15s shutdown observation inside the same margin as ``pirpc.close()``'s own
#: 15s + 5s worst case.
SHUTDOWN_RESERVE_S = 30.0

#: Predicted output tokens for the single-session Builder and each resume prompt
#: (BUILD_PLAN.md rev 6 §1, C6). Real judged runs (900s budget) always satisfy
#: ``BudgetController.can_start`` against these; the refusal path exists for
#: correctness, not because it is expected to fire. Missions mode predicts per
#: role instead -- see ``missions.PREDICTED_OUTPUT_TOKENS``.
BUILDER_PREDICTED_OUTPUT_TOKENS = 12000

DEFAULT_GATEWAY_URL = "https://api.berget.ai/v1"
DEFAULT_DIRECT_MODEL = "zai-org/GLM-5.2"
DEFAULT_DIRECT_PROVIDER = "berget"

#: The Analyst's slice of the run's wall clock. Measured 2026-09-03 (probe1,
#: real Berget call): 444 in / 1,303 out in 42 s. 90 s leaves room for one
#: retry without ever letting a hung gateway eat the Builder's time.
ANALYST_MAX_S = 90.0

#: Grace for the ``abort`` acknowledgement (``agent_settled`` or the response).
ABORT_GRACE_S = 5.0

#: Shutdown budget when the harness itself was signalled: the runner escalates
#: SIGTERM to SIGKILL after 5 s, so everything here must fit inside that.
FAST_CLOSE = {"stdin_grace": 0.5, "term_grace": 1.5, "kill_grace": 1.0}

SESSION_LABEL = "1-builder"

#: ``HARNESS_MODE``: the missions pipeline (the default), or the Phase 2
#: single session.
RUN_MODES = ("missions", "single")

#: Pi's exact, case-sensitive thinking levels. An unrecognised ``--thinking``
#: makes Pi warn, ignore the flag and fall back to its own default (``medium``),
#: which would silently turn thinking on for a judged run.
VALID_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# The transient-error resume loop, Pi's auto-retry flag and the resume policy
# now live in ``harness.missions`` so that every mission session and the single
# session behave identically (PHASE3_DESIGN §5). These names stay here as thin
# aliases: they are part of this module's tested surface.
PI_AUTO_RETRY_ENV = missions_mod.PI_AUTO_RETRY_ENV
RESUME_MAX_ATTEMPTS = missions_mod.RESUME_MAX_ATTEMPTS
RESUME_BACKOFF_S = missions_mod.RESUME_BACKOFF_S
RESUME_MIN_BUDGET_S = missions_mod.RESUME_MIN_BUDGET_S
RESUME_PREDICTED_OUTPUT_TOKENS = missions_mod.RESUME_PREDICTED_OUTPUT_TOKENS
RESUME_PROMPT = missions_mod.RESUME_PROMPT
TRANSIENT_ERROR = missions_mod.TRANSIENT_ERROR

pi_auto_retry_enabled = missions_mod.pi_auto_retry_enabled
is_transient_error = missions_mod.is_transient_error
resume_policy = missions_mod.resume_policy
_resume_after_transient_errors = missions_mod.resume_after_transient_errors


def normalize_thinking(raw: Optional[str]) -> str:
    """Coerce a thinking level to something Pi accepts, only ever toward ``off``.

    A typo must never be fatal: this warns and returns ``off`` rather than
    raising :class:`ConfigError`, so a misconfigured environment still runs.
    """
    candidate = (raw or "").strip().lower()
    if candidate in VALID_THINKING_LEVELS:
        return candidate
    if candidate:
        warn('ignoring invalid --thinking "{0}"; using "off"'.format(raw))
    return "off"


class ConfigError(RuntimeError):
    """A usage or configuration problem detected before any session starts."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m harness",
        description=(
            "Run the AgentCofounder harness: one Pi RPC session against the product "
            "idea, with every Pi stdout line forwarded verbatim to this process's stdout."
        ),
    )
    parser.add_argument("--idea-file", required=True, help="absolute path to the product idea")
    parser.add_argument(
        "--session-root", required=True, help="directory that holds one sub-directory per session"
    )
    parser.add_argument("--cwd", required=True, help="working directory for the generated app")
    parser.add_argument(
        "--timeout-ms",
        required=True,
        type=int,
        help="hard in-process deadline in milliseconds, measured from harness start",
    )
    parser.add_argument("--repo-root", required=True, help="absolute path to the repository root")
    parser.add_argument(
        "--thinking", default="off", help="Pi thinking level for the session (default: off)"
    )
    parser.add_argument("--provider", default=None, help="provider name, when the runner set one")
    parser.add_argument("--model", default=None, help="model id, when the runner set one")
    return parser


def parse_arguments(argv: List[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def resolve_pi_binary(repository_root: pathlib.Path) -> pathlib.Path:
    """The Pi binary, or the test-only ``HARNESS_PI_BIN`` override."""
    override = os.environ.get("HARNESS_PI_BIN")
    if override:
        return pathlib.Path(override)
    name = "pi.cmd" if sys.platform == "win32" else "pi"
    return repository_root / "node_modules" / ".bin" / name


def build_append_system_prompt(repository_root: pathlib.Path, app_directory: pathlib.Path) -> str:
    """Parity with ``buildPiArguments``: system prompt, journeys, app contract.

    This is the *single-session* prefix. Missions mode builds its own, much
    smaller one (``harness.loop.build_missions_system_prompt``): the journeys
    checklist moved into the Analyst's prompt, and a mission's instruction is
    its brief.
    """
    system_prompt_path = repository_root / "solution" / "system-prompt.md"
    journeys_path = repository_root / "contract-public" / "journeys.md"
    agents_path = app_directory / "AGENTS.md"

    missing = [p for p in (system_prompt_path, journeys_path) if not p.is_file()]
    if missing:
        raise ConfigError(
            "missing starter prompt material: " + ", ".join(str(p) for p in missing)
        )

    parts = [_read_text(system_prompt_path).strip(), _read_text(journeys_path).strip()]
    if agents_path.is_file():
        parts.append(_read_text(agents_path).strip())
    else:
        warn("no AGENTS.md at {0}; appending prompt without it".format(agents_path))
    return "\n\n".join(parts)


def read_journeys(repository_root: pathlib.Path) -> str:
    """``contract-public/journeys.md``, or ``""``.

    The Analyst turns its "Behaviors to implement and test when implied" list
    into a coverage checklist; missions themselves never see this file.
    """
    path = repository_root / "contract-public" / "journeys.md"
    try:
        return _read_text(path)
    except OSError:
        return ""


def collect_extensions(repository_root: pathlib.Path) -> List[pathlib.Path]:
    """Explicit ``--extension`` paths survive ``--no-extensions``."""
    extensions: List[pathlib.Path] = []
    protected = repository_root / "solution" / "extensions" / "protected-paths.ts"
    if protected.is_file():
        extensions.append(protected)
    else:
        warn("protected-paths.ts not found at {0}; running without it".format(protected))
    guard = repository_root / "solution" / "extensions" / "thinking-guard.ts"
    if guard.is_file():
        extensions.append(guard)
    else:
        warn("thinking-guard.ts not found at {0}; running without it".format(guard))
    return extensions


def child_environment(harness_directory: pathlib.Path, extensions: List[pathlib.Path]) -> Dict[str, str]:
    """Pi's environment: ours plus ``PI_OFFLINE=1``.

    ``PI_CODING_AGENT_DIR`` is never set or invented here -- the organizers' own
    Pi configuration has to win. ``HARNESS_PAYLOAD_LOG`` is only defaulted when
    the thinking guard (its sole reader) is actually loaded and the runner did
    not already point it somewhere.
    """
    extra: Dict[str, str] = {}
    guard_loaded = any(p.name == "thinking-guard.ts" for p in extensions)
    if guard_loaded and not os.environ.get("HARNESS_PAYLOAD_LOG"):
        extra["HARNESS_PAYLOAD_LOG"] = str(harness_directory / "payload.jsonl")
    return pi_env(extra)


def payload_log_path(harness_directory: pathlib.Path) -> pathlib.Path:
    """Where the thinking guard's payload log actually lands (C5).

    Mirrors :func:`child_environment`'s own default so the prefix check reads
    the same file the guard was told to write, whether the runner pointed
    ``HARNESS_PAYLOAD_LOG`` somewhere else or not.
    """
    override = os.environ.get("HARNESS_PAYLOAD_LOG")
    return pathlib.Path(override) if override else harness_directory / "payload.jsonl"


# -- direct-gateway client (credentials, budget, analyst) -------------------
#
# ``gateway.GatewayClient`` already defaults its own cost table from
# ``<repo>/.pi-agent/models.json`` when ``cost_table`` is omitted, so this
# module does not load or pass one itself -- doing so would just be a second,
# divergence-prone copy of the same lookup.


def resolve_credentials() -> Tuple[Optional[str], Optional[str]]:
    """``(api_key, name_used)``, or ``(None, None)`` when unavailable.

    Never raises: a broken ``credentials`` module must not take the whole
    harness down over a feature (the analyst) that is allowed to fail open.
    """
    if _credentials is None:
        return None, None
    try:
        return _credentials.resolve_api_key()
    except Exception as exc:  # noqa: BLE001 - credential resolution must not crash the harness
        warn("credentials.resolve_api_key failed: {0}".format(exc))
        return None, None


def build_direct_client(
    harness_directory: pathlib.Path,
    api_key: str,
    args: argparse.Namespace,
    stop_event: Optional[threading.Event] = None,
) -> Any:
    """The one :class:`~harness.gateway.GatewayClient` for the whole run, or ``None``.

    Built once and shared by the Analyst, the model Supervisor and the Reviewer
    so all three appear as one client in the usage log and against one cost
    table. Model/provider follow the same precedence as the rest of the
    harness: the CLI flag, then the matching ``CHALLENGE_*`` env var, then the
    contract default.
    """
    if _gateway is None:
        return None
    try:
        return _gateway.GatewayClient(
            base_url=os.environ.get("HARNESS_GATEWAY_URL", DEFAULT_GATEWAY_URL),
            api_key=api_key,
            model=args.model or os.environ.get("CHALLENGE_MODEL") or DEFAULT_DIRECT_MODEL,
            provider=args.provider or os.environ.get("CHALLENGE_PROVIDER") or DEFAULT_DIRECT_PROVIDER,
            harness_dir=harness_directory,
            stop_event=stop_event,
        )
    except Exception as exc:  # noqa: BLE001 - the direct client is never a blocker
        warn("direct client unavailable ({0}); continuing without one".format(exc))
        return None


def run_analyst(
    harness_directory: pathlib.Path,
    idea: str,
    client: Any,
    deadline: float,
    journeys_md: str = "",
) -> Optional[Dict[str, Any]]:
    """Run the Analyst (C4); ``None`` on any failure.

    Any failure here is logged and swallowed -- the run must proceed without a
    spec (in single mode) rather than block on it.

    ``deadline`` is a small, bounded slice of the harness's own wall-clock
    budget (see ``run()``), never the full run deadline -- a hung or
    error-looping gateway must not be able to eat the time the missions need.
    """
    if client is None or _analyst is None:
        return None
    try:
        return _analyst.run_analyst(
            client, idea, harness_directory, deadline=deadline, journeys_md=journeys_md
        )
    except Exception as exc:  # noqa: BLE001 - the analyst is a seed feature, never a blocker
        warn("analyst failed ({0}); the run proceeds without a spec".format(exc))
        return None


def budget_gate_active() -> bool:
    """Whether :class:`BudgetController`'s wall-clock predictions should gate.

    ``HARNESS_PI_BIN`` (documented in ``pirpc.py`` as fake-Pi-tests/dry-run
    only, and never set in a judged run) is also the one reliable signal that
    there is no real model on the other end to predict a generation speed
    for: every harness test drives the fake Pi, which answers near-instantly
    regardless of predicted output size, so gating on a 30 tok/s assumption
    against it would refuse missions the fixture never intended to be slow.
    A judged run never sets this variable, so the gate is always live there.
    """
    return not os.environ.get("HARNESS_PI_BIN")


#: ``None`` when the mission may start, else the refusal reason. A thin,
#: directly-testable wrapper around ``controller.can_start``; ``harness.missions``
#: owns the implementation so mission sessions and this module gate identically.
budget_gate_reason = missions_mod.budget_gate_reason


def requested_mode() -> str:
    """``HARNESS_MODE`` as given, or ``""`` when unset/invalid."""
    requested = (os.environ.get("HARNESS_MODE") or "").strip().lower()
    if requested and requested not in RUN_MODES:
        warn('ignoring invalid HARNESS_MODE "{0}"'.format(requested))
        return ""
    return requested


def post_analyst_output_tokens() -> int:
    """Output tokens the run still owes after the Analyst, for its deadline.

    Mode-aware, because the two bodies are an order of magnitude apart: the
    Phase 2 session writes both files, a report and its prose in one turn
    (12,000 predicted), while missions mode's first two turns are one file
    each (1,500 + 1,800). Reserving the single-session figure for a missions
    run would price the Analyst out of every budget under ~8 minutes.
    """
    if requested_mode() == "single":
        return BUILDER_PREDICTED_OUTPUT_TOKENS
    return (
        missions_mod.BUILDER_PREDICTED_OUTPUT_TOKENS
        + missions_mod.TESTER_PREDICTED_OUTPUT_TOKENS
    )


def resolve_mode(spec: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """``(mode, reason)`` -- which body runs, and why, for the log line."""
    if requested_mode() == "single":
        return "single", "HARNESS_MODE=single"
    if not spec:
        return "single", "no usable spec"
    return "missions", "spec with {0} journey(s)".format(len(spec.get("journeys") or []))


# -- the narration (demo surface) -------------------------------------------
#
# Plain English for whoever is watching this process's stderr in the demo
# recording. Pure functions over data the run already holds: no model call, no
# extra I/O, and nothing here may change an exit code, the report or stdout.


def _count(number: int, noun: str) -> str:
    """``"1 field"`` / ``"6 fields"`` -- the narration says numbers out loud."""
    return "{0} {1}".format(number, noun if number == 1 else noun + "s")


def spec_narration(spec: Dict[str, Any]) -> str:
    """What the Analyst understood, from the spec the run already has."""
    return "Understood the product: {0}, {1}".format(
        _count(len(spec.get("fields") or []), "field"),
        _count(len(spec.get("journeys") or []), "user journey"),
    )


def single_finish_narration(
    observation: Optional[Dict[str, Any]], signalled: bool
) -> str:
    """How the single session ended, in plain words.

    ``observation`` is the shutdown observation (``ReportWatcher.final_observe``)
    when one was taken, and ``None`` when it was not -- either because a signal
    is in flight (no time for it) or because the model wrote its own report and
    the harness left it alone.
    """
    if signalled:
        return "Stopping: the run was told to shut down — no final check"
    if observation is None:
        return "Done: the app and its report are written"
    total = int(observation.get("total") or 0)
    failed = int(observation.get("failed") or 0)
    if observation.get("green") and total:
        if total == 1:
            return "Done: the one test passes — report written"
        return "Done: all {0} tests pass — report written".format(total)
    if failed:
        return "Stopping: {0} of {1} tests still {2}".format(
            failed, total, "fails" if failed == 1 else "fail"
        )
    return "Stopping: no tests ran"


#: How many unmatched ``agent_start`` timestamps are remembered. Two missions
#: run at once; anything beyond this is a turn whose ``message_end`` never
#: arrived (a timeout), and an unbounded memory of those would slowly shift
#: every later turn's measurement onto a stale start.
MAX_PENDING_TURNS = 4


class _UsageObserver:
    """Feeds every assistant ``message_end`` into the budget controller.

    A turn's output tokens and the wall time *that turn* took is what keeps
    ``BudgetController.tokens_per_s`` live. ``agent_start`` is when a session's
    turn begins, so the pending starts are queued and each ``message_end``
    takes the oldest one; only a message with no start left to match (a second
    assistant message inside one turn, or the synthetic Analyst event) falls
    back to the gap since the previous ``message_end``.

    Why not the gap alone, which is what this did first: with Builder ∥ Tester
    two turns share one wall-clock interval, so charging the second turn only
    the gap after the first settles reports the run generating at roughly the
    parallelism factor of its real speed -- measured 54 tok/s against an honest
    30, which let ``can_start`` admit a mission with 75 s left that needed 90.

    Every read-modify-write here is taken under a lock: missions mode calls
    this from two mission threads at once.
    """

    def __init__(self, controller: BudgetController) -> None:
        self._controller = controller
        self._last_ts = time.monotonic()
        self._lock = threading.Lock()
        self._pending: List[float] = []

    def __call__(self, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "agent_start":
            with self._lock:
                self._pending.append(time.monotonic())
                del self._pending[:-MAX_PENDING_TURNS]
            return
        if kind != "message_end":
            return
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        usage = message.get("usage")
        output = 0
        if isinstance(usage, dict):
            try:
                output = int(usage.get("output") or 0)
            except (TypeError, ValueError):
                output = 0
        with self._lock:
            now = time.monotonic()
            started = self._pending.pop(0) if self._pending else self._last_ts
            elapsed = max(0.0, now - started)
            self._last_ts = now
            self._controller.observe_usage(output, elapsed)


def _dispatch_event(callbacks: List[Callable[[Dict[str, Any]], None]]) -> Callable[[Dict[str, Any]], None]:
    """Combine several ``on_event`` observers into the one :meth:`PiRpc.prompt` takes."""

    def _handler(event: Dict[str, Any]) -> None:
        for callback in callbacks:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 - one observer's bug must not sink another's
                warn("on_event observer failed: {0}".format(exc))

    return _handler


def _validate(args: argparse.Namespace) -> Dict[str, Any]:
    if args.timeout_ms < 1000:
        raise ConfigError("--timeout-ms must be an integer of at least 1000")

    idea_file = pathlib.Path(args.idea_file)
    if not idea_file.is_file():
        raise ConfigError("--idea-file does not exist: {0}".format(idea_file))
    idea = _read_text(idea_file).strip()
    if not idea:
        raise ConfigError("--idea-file is empty: {0}".format(idea_file))

    repository_root = pathlib.Path(args.repo_root)
    if not repository_root.is_dir():
        raise ConfigError("--repo-root does not exist: {0}".format(repository_root))

    app_directory = pathlib.Path(args.cwd)
    if not app_directory.is_dir():
        raise ConfigError("--cwd does not exist: {0}".format(app_directory))

    pi_binary = resolve_pi_binary(repository_root)
    if not pi_binary.exists():
        raise ConfigError("Pi binary not found at {0}".format(pi_binary))

    session_root = pathlib.Path(args.session_root)
    harness_directory = session_root.parent / "harness"
    try:
        session_root.mkdir(parents=True, exist_ok=True)
        harness_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError("cannot create harness directories: {0}".format(exc))

    return {
        "idea": idea,
        "repository_root": repository_root,
        "app_directory": app_directory,
        "pi_binary": pi_binary,
        "session_root": session_root,
        "harness_directory": harness_directory,
    }


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    deadline = started + args.timeout_ms / 1000.0

    config = _validate(args)
    harness_directory: pathlib.Path = config["harness_directory"]
    set_file_sink(harness_directory / "harness.log")
    # The demo recording's first line: the run has an idea and is about to
    # start. Everything the narration says afterwards is a stage boundary.
    narrate("Reading the product idea…")

    repository_root: pathlib.Path = config["repository_root"]
    app_directory: pathlib.Path = config["app_directory"]

    append_system = build_append_system_prompt(repository_root, app_directory)
    extensions = collect_extensions(repository_root)

    stop_event = threading.Event()
    signalled: List[str] = []

    def _on_signal(signum: int, _frame: Any) -> None:
        signalled.append(signal.Signals(signum).name)
        stop_event.set()

    previous_handlers: Dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, _on_signal)
        except (OSError, ValueError):
            pass

    def _restore_signals() -> None:
        for number, handler in previous_handlers.items():
            try:
                signal.signal(number, handler)
            except (OSError, ValueError):
                pass

    # -- credentials, budget controller, analyst (C4/C6) --------------------
    api_key, key_name = resolve_credentials()
    log("credentials", "using {0}".format(key_name) if api_key else "none found")

    controller = BudgetController(deadline_monotonic=deadline)
    gate_active = budget_gate_active()

    spec: Optional[Dict[str, Any]] = None
    client: Any = None
    direct_disabled = os.environ.get("HARNESS_DIRECT", "").strip() == "0"
    if direct_disabled:
        log("harness", "direct client disabled (HARNESS_DIRECT=0)")
    elif not DIRECT_CLIENT_AVAILABLE:
        log("harness", "direct client unavailable ({0})".format(_DIRECT_IMPORT_ERROR))
    elif not api_key:
        log("harness", "analyst · skipped, no API key available")
    elif stop_event.is_set():
        log("harness", "analyst · skipped, shutting down")
    else:
        # A small, fixed slice of the run's own budget -- never more than
        # ANALYST_MAX_S, and less when the first mission's own predicted finish
        # already leaves little enough margin that every second counts. This is
        # what keeps a hung/erroring gateway from eating the time the missions
        # need (they are attempted next, regardless of whether the analyst
        # produced a spec).
        analyst_reserve_s = (
            controller.predict_seconds(post_analyst_output_tokens()) + controller.finish_margin_s
        )
        analyst_deadline = min(
            time.monotonic() + ANALYST_MAX_S, deadline - SHUTDOWN_RESERVE_S - analyst_reserve_s
        )
        client = build_direct_client(harness_directory, api_key, args, stop_event)
        spec = run_analyst(
            harness_directory,
            config["idea"],
            client,
            deadline=analyst_deadline,
            journeys_md=read_journeys(repository_root),
        )
        log("harness", "analyst · {0}".format("spec produced" if spec else "no spec (continuing without one)"))
        if spec:
            narrate(spec_narration(spec))

    context = RunContext(
        args=args,
        idea=config["idea"],
        repository_root=repository_root,
        app_directory=app_directory,
        session_root=config["session_root"],
        harness_directory=harness_directory,
        pi_binary=config["pi_binary"],
        extensions=extensions,
        child_env=child_environment(harness_directory, extensions),
        append_system=append_system,
        thinking=normalize_thinking(args.thinking),
        deadline=deadline,
        stop_event=stop_event,
        signalled=signalled,
        controller=controller,
        gate_active=gate_active,
        spec=spec,
        client=client,
        restore_signals=_restore_signals,
    )

    mode, reason = resolve_mode(spec)
    log("harness", "mode · {0} ({1})".format(mode, reason))
    if mode == "single" and not spec:
        # The only fallback a viewer needs explained: no spec means no missions,
        # so the whole app is written in one session instead.
        narrate("Could not derive a spec — building in one session instead")
    try:
        if mode == "missions":
            # Missions mode has no ReportWatcher: no mission has a `bash` tool,
            # so nothing in a session can run vitest for the watcher to notice.
            context.usage_observer = _dispatch_event([_UsageObserver(controller)])
            return finalize(harness_directory, controller, run_missions(context))
        return run_single_session(context)
    finally:
        # Both bodies restore the handlers themselves once their last session is
        # closed; this is the backstop for a body that raised before it could.
        _restore_signals()


def finalize(
    harness_directory: pathlib.Path, controller: BudgetController, success: bool
) -> int:
    """The prefix check (C5), the budget snapshot (C6), and the exit code.

    Shared by both run bodies so a judged run's artifacts are the same shape
    whichever one ran.
    """
    prefix.check(payload_log_path(harness_directory))

    snapshot = controller.snapshot()
    predicted_total_s = sum(p.get("predicted_s", 0.0) for p in snapshot["predictions"])
    actual_total_s = sum(p.get("actual_s", 0.0) for p in snapshot["predictions"])
    log(
        "budget",
        "elapsed={0:.0f}s output={1} peak={2} tok/s={3:.1f} predicted={4:.0f}s actual={5:.0f}s".format(
            snapshot["elapsed_s"], snapshot["cumulative_output"], snapshot["peak_output"],
            snapshot["tokens_per_s"], predicted_total_s, actual_total_s,
        ),
    )
    try:
        (harness_directory / "budget.json").write_text(
            json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        warn("could not write budget.json: {0}".format(exc))

    log("harness", "exit {0} ({1})".format(
        EXIT_SUCCESS if success else EXIT_FAILURE,
        "success" if success else "no usable assistant turn",
    ))
    return EXIT_SUCCESS if success else EXIT_FAILURE


def run_single_session(context: RunContext) -> int:
    """The Phase 2 all-in-one Pi session: one prompt, the model does the rest.

    Unchanged from Phase 2 apart from taking its inputs off ``context``: this
    is the proven path, the fallback whenever there is no usable spec, and the
    one every pre-Phase-3 test exercises.
    """
    args = context.args
    deadline = context.deadline
    stop_event = context.stop_event
    controller = context.controller
    harness_directory = context.harness_directory
    app_directory = context.app_directory

    skill = context.repository_root / "solution" / "skills" / "mvp-builder"
    if not skill.is_dir():
        warn("skill directory not found at {0}; running without it".format(skill))
        skill_argument: Optional[pathlib.Path] = None
    else:
        skill_argument = skill

    session_dir = context.session_root / SESSION_LABEL
    pi_arguments = base_args(
        append_system=context.append_system,
        session_dir=session_dir,
        extensions=list(context.extensions),
        skill=skill_argument,
        provider=args.provider,
        model=args.model,
        thinking=context.thinking,
    )

    log(
        "harness",
        "session {0} · thinking={1} · budget={2:.0f}s · cwd={3}".format(
            SESSION_LABEL, context.thinking, args.timeout_ms / 1000.0, app_directory
        ),
    )
    narrate("Writing the whole app and its tests in one session…")

    if context.gate_active:
        refusal = budget_gate_reason(controller, BUILDER_PREDICTED_OUTPUT_TOKENS)
        if refusal is not None:
            log_error("budget · cannot start Builder mission: {0}".format(refusal))
            context.restore_signals()
            return EXIT_FAILURE

    # The v2 spec no longer carries ``implemented_features`` -- the Architect
    # derives them -- so a harness-authored fallback report would come back
    # with an empty feature list. Deriving the plan here (a pure function, no
    # model call) restores exactly what Phase 2's report had.
    spec_for_report = context.spec
    if spec_for_report:
        spec_for_report = report_spec(spec_for_report, derive_plan(spec_for_report))

    report_watcher = report_mod.ReportWatcher(
        app_dir=app_directory,
        harness_dir=harness_directory,
        idea_text=context.idea,
        spec=spec_for_report,
    )
    usage_observer = _UsageObserver(controller)
    on_event = _dispatch_event([usage_observer, report_watcher.on_event])

    client = PiRpc(
        pi_bin=context.pi_binary,
        args=pi_arguments,
        cwd=app_directory,
        env=dict(context.child_env),
        session_dir=session_dir,
        label=SESSION_LABEL,
        stderr_path=harness_directory / "{0}.stderr.log".format(SESSION_LABEL),
        stop_event=stop_event,
    )

    result: Dict[str, Any] = {
        "success": False,
        "settled": False,
        "interrupted": False,
        "timed_out": False,
        "error": None,
        "stop_reason": None,
    }
    try:
        auto_retry = pi_auto_retry_enabled()
        try:
            client.set_auto_retry(auto_retry, timeout=min(30.0, max(1.0, deadline - time.monotonic())))
            log("harness", "pi auto-retry {0}".format("on" if auto_retry else "off"))
        except PiRpcInterrupted:
            stop_event.set()
        except PiRpcError as exc:
            warn("set_auto_retry failed ({0}); Pi keeps its own retry policy".format(exc))

        if not stop_event.is_set():
            budget = max(1.0, deadline - time.monotonic() - SHUTDOWN_RESERVE_S)
            prompt_text = "## Product idea\n\n" + context.idea + "\n"
            mission = controller.begin_mission("builder", BUILDER_PREDICTED_OUTPUT_TOKENS)
            try:
                result = client.prompt(prompt_text, timeout=budget, on_event=on_event)
            except PiRpcInterrupted:
                result["interrupted"] = True
            except PiRpcError as exc:
                result["error"] = str(exc)
            finally:
                call_usage = result.get("usage")
                controller.end_mission(
                    mission,
                    getattr(call_usage, "output", 0) or 0,
                    result.get("wall_s", 0.0) or 0.0,
                )
            if not result.get("interrupted"):
                result = _resume_after_transient_errors(
                    client, result, deadline, stop_event,
                    controller=controller, gate_active=context.gate_active, on_event=on_event,
                )
        else:
            result["interrupted"] = True
    finally:
        _shutdown(client, result, stop_event)
        context.restore_signals()

    # The session total: the initial prompt plus every resume prompt.
    usage = client.total
    if usage is not None:
        log("usage", " ".join("{0}={1}".format(k, v) for k, v in usage.as_dict().items()))
    if result.get("resume_attempts"):
        log("harness", "resumed after transient provider errors {0} time(s)".format(result["resume_attempts"]))
    if context.signalled:
        log("harness", "received {0}; shut the session down early".format(context.signalled[0]))
    if result.get("error"):
        log_error("last error: {0}".format(result["error"]))
    log(
        "harness",
        "settled={0} timed_out={1} interrupted={2} stopReason={3} pi_exit={4} forwarded={5} malformed={6}".format(
            bool(result.get("settled")),
            bool(result.get("timed_out")),
            bool(result.get("interrupted")),
            result.get("stop_reason"),
            client.exit_code,
            client.forwarded_records,
            client.malformed_lines,
        ),
    )

    # -- harness-authored report (C7) ----------------------------------
    # A signalled shutdown races the runner's own 5s SIGTERM->SIGKILL grace
    # (src/process-tree.ts); spawning vitest here is exactly the kind of
    # unbounded-relative-to-that-grace call the shutdown path must not make,
    # so a real OS signal skips this step outright rather than risking it.
    report_path = app_directory / "report.partial.json"
    needs_final_report = bool(result.get("interrupted")) or bool(result.get("timed_out")) or not report_path.is_file()
    final_observation: Optional[Dict[str, Any]] = None
    if context.signalled:
        log("report", "final observe skipped (signal received); shutdown must stay fast")
    elif needs_final_report:
        final_observation = report_watcher.final_observe()
        log(
            "report",
            "final observe · green={0} total={1} passed={2} failed={3}".format(
                final_observation.get("green"), final_observation.get("total"),
                final_observation.get("passed"), final_observation.get("failed"),
            ),
        )
    else:
        report_watcher.join(2.0)
        # The model finished and wrote its own report. If its tests_run entries
        # are not in the runner's shape (measured slip: {name, status}), the
        # runner drops them all and degrades a green run to partial. Fill that
        # one field from the real vitest JSON; keep everything else the model wrote.
        repaired = report_watcher.repair_model_report()
        if repaired is not None:
            log("report", "repaired tests_run from vitest ({0} entries); model prose kept".format(repaired))

    narrate(single_finish_narration(final_observation, bool(context.signalled)))
    return finalize(harness_directory, controller, bool(result.get("success")))


def _shutdown(client: PiRpc, result: Dict[str, Any], stop_event: threading.Event) -> None:
    """Close the session. Never raises; always reaps the child."""
    try:
        if stop_event.is_set() or result.get("interrupted"):
            # The runner escalates to SIGKILL after 5 s -- no time for an abort
            # handshake. Closing stdin still lets Pi flush its stdout.
            client.close(**FAST_CLOSE)
            return
        if not result.get("settled"):
            acknowledged = client.abort(grace=ABORT_GRACE_S)
            if not acknowledged:
                warn("abort was not acknowledged within {0:.0f}s".format(ABORT_GRACE_S))
        client.close()
    except Exception as exc:  # noqa: BLE001 - shutdown must not mask the result
        log_error("shutdown problem: {0}".format(exc))
        try:
            client.close(**FAST_CLOSE)
        except Exception:  # noqa: BLE001
            pass


def main(argv: Optional[List[str]] = None) -> int:
    arguments = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    try:
        return run(arguments)
    except ConfigError as exc:
        log_error(str(exc))
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - a crash must still be a clean exit code
        log_error("unhandled harness error: {0}: {1}".format(type(exc).__name__, exc))
        return EXIT_FAILURE
    finally:
        try:
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass
        close_file_sink()


if __name__ == "__main__":
    sys.exit(main())
