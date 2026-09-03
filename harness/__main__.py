"""``python3 -m harness`` -- the Phase 1 single-session orchestrator.

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

``HARNESS_PI_BIN`` replaces the Pi binary. It exists for the fake-Pi tests and
the integration dry run only; nothing in a judged run should set it.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from .log import close_file_sink, error as log_error, log, set_file_sink, warn
from .pirpc import PiRpc, PiRpcError, PiRpcInterrupted, base_args, pi_env

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

#: Reserved out of ``--timeout-ms`` for abort + shutdown, so the harness is gone
#: well before the runner's own timer fires.
SHUTDOWN_RESERVE_S = 20.0

#: Grace for the ``abort`` acknowledgement (``agent_settled`` or the response).
ABORT_GRACE_S = 5.0

#: Shutdown budget when the harness itself was signalled: the runner escalates
#: SIGTERM to SIGKILL after 5 s, so everything here must fit inside that.
FAST_CLOSE = {"stdin_grace": 0.5, "term_grace": 1.5, "kill_grace": 1.0}

SESSION_LABEL = "1-builder"

#: Pi's exact, case-sensitive thinking levels. An unrecognised ``--thinking``
#: makes Pi warn, ignore the flag and fall back to its own default (``medium``),
#: which would silently turn thinking on for a judged run.
VALID_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

#: Pi's agent-level auto-retry (3 attempts, 2/4/8 s backoff) stays ON by default:
#: it is what carried the measured baseline through transient 5xx responses. The
#: per-prompt budget still bounds a wedged call. ``HARNESS_PI_AUTO_RETRY=0`` turns
#: it off for experiments.
PI_AUTO_RETRY_ENV = "HARNESS_PI_AUTO_RETRY"

#: When a run still ends on a *transient* provider error (Pi's retries exhausted,
#: or a 503 that arrived outside its retry window) and budget remains, the harness
#: sends one follow-up prompt per attempt so the agent continues where it stopped.
RESUME_MAX_ATTEMPTS = 3
RESUME_BACKOFF_S = (5.0, 10.0, 20.0)
RESUME_MIN_BUDGET_S = 60.0
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
    """Parity with ``buildPiArguments``: system prompt, journeys, app contract."""
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

    repository_root: pathlib.Path = config["repository_root"]
    app_directory: pathlib.Path = config["app_directory"]
    session_root: pathlib.Path = config["session_root"]

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

    skill = repository_root / "solution" / "skills" / "mvp-builder"
    if not skill.is_dir():
        warn("skill directory not found at {0}; running without it".format(skill))
        skill_argument: Optional[pathlib.Path] = None
    else:
        skill_argument = skill

    thinking = normalize_thinking(args.thinking)
    session_dir = session_root / SESSION_LABEL
    pi_arguments = base_args(
        append_system=append_system,
        session_dir=session_dir,
        extensions=list(extensions),
        skill=skill_argument,
        provider=args.provider,
        model=args.model,
        thinking=thinking,
    )

    log(
        "harness",
        "session {0} · thinking={1} · budget={2:.0f}s · cwd={3}".format(
            SESSION_LABEL, thinking, args.timeout_ms / 1000.0, app_directory
        ),
    )

    client = PiRpc(
        pi_bin=config["pi_binary"],
        args=pi_arguments,
        cwd=app_directory,
        env=child_environment(harness_directory, extensions),
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
            prompt_text = "## Product idea\n\n" + config["idea"] + "\n"
            try:
                result = client.prompt(prompt_text, timeout=budget)
            except PiRpcInterrupted:
                result["interrupted"] = True
            except PiRpcError as exc:
                result["error"] = str(exc)
            if not result.get("interrupted"):
                result = _resume_after_transient_errors(client, result, deadline, stop_event)
        else:
            result["interrupted"] = True
    finally:
        _shutdown(client, result, stop_event)
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass

    # The session total: the initial prompt plus every resume prompt.
    usage = client.total
    if usage is not None:
        log("usage", " ".join("{0}={1}".format(k, v) for k, v in usage.as_dict().items()))
    if result.get("resume_attempts"):
        log("harness", "resumed after transient provider errors {0} time(s)".format(result["resume_attempts"]))
    if signalled:
        log("harness", "received {0}; shut the session down early".format(signalled[0]))
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

    success = bool(result.get("success"))
    log("harness", "exit {0} ({1})".format(EXIT_SUCCESS if success else EXIT_FAILURE, "success" if success else "no usable assistant turn"))
    return EXIT_SUCCESS if success else EXIT_FAILURE


def _resume_after_transient_errors(
    client: PiRpc,
    result: Dict[str, Any],
    deadline: float,
    stop_event: threading.Event,
) -> Dict[str, Any]:
    """Follow up a run that settled on a transient provider error, within budget.

    Each attempt waits a backoff (polled in <=0.25 s slices so SIGTERM is still
    observed), then sends :data:`RESUME_PROMPT` into the same session. The
    returned result is the latest prompt's, with ``success`` carried forward if
    any earlier prompt already produced a usable assistant turn.
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
        attempts += 1
        log(
            "harness",
            "transient provider error '{0}' · resuming in {1:.0f}s (attempt {2}/{3})".format(
                result.get("error"), backoff, attempts, policy["attempts"]
            ),
        )
        waited = 0.0
        while waited < backoff and not stop_event.is_set():
            slice_s = min(0.25, backoff - waited)
            stop_event.wait(slice_s)
            waited += slice_s
        if stop_event.is_set():
            result["interrupted"] = True
            break
        previous_success = bool(result.get("success"))
        try:
            follow = client.prompt(RESUME_PROMPT, timeout=max(1.0, remaining))
        except PiRpcInterrupted:
            result["interrupted"] = True
            break
        except PiRpcError as exc:
            result["error"] = str(exc)
            break
        follow["success"] = bool(follow.get("success")) or previous_success
        result = follow
    result["resume_attempts"] = attempts
    return result


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
