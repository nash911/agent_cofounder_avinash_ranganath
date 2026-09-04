#!/usr/bin/env python3
"""A deterministic stand-in for ``pi --mode rpc``. **Test use only.**

Point the harness at it with ``HARNESS_PI_BIN=<this file>``. It speaks just
enough of the RPC protocol (``docs/rpc.md``) to exercise the harness end to end
without a model, a network call, or a token spent.

It accepts and ignores every real Pi flag -- including the missions-mode
``--tools <list>`` -- but reads ``--session-dir`` so it can write a non-empty
session JSONL where the organizer artifact audit looks for one.
Everything it emits is also mirrored, byte for byte, into
``<session-dir>/emitted.jsonl`` so a test can assert forwarding fidelity.

Environment knobs
-----------------
``FAKE_PI_HANG=1``       accept the prompt and never settle; also ignore stdin
                         EOF and SIGTERM, so the harness's SIGKILL fallback is
                         genuinely exercised.
``FAKE_PI_SLOW=<sec>``   accept the prompt and settle only after ``<sec>``
                         seconds, while still answering ``abort`` promptly.
``FAKE_PI_ERROR=1``      assistant ``message_end`` with ``stopReason: "error"``,
                         an ``errorMessage`` and all-zero usage.
``FAKE_PI_LINES=<n>``    emit ``n`` extra 100 KB ``message_update`` records
                         before the assistant message ends.
``FAKE_PI_GARBAGE=1``    emit one non-JSON line, to prove malformed records are
                         still forwarded verbatim.
``FAKE_PI_COMPACT=<sec>`` after ``agent_end``, run an auto-compaction the way the
                         real Pi does: ``compaction_start``, ``<sec>`` seconds of
                         complete silence (the summarization LLM call emits
                         nothing), ``compaction_end``, a second assistant
                         ``message_end``, and only then ``agent_settled``.
``FAKE_PI_WRITE_REPORT=1`` write a minimal ``report.partial.json`` into the cwd.
``FAKE_PI_GREEN_TESTS=1``  before the assistant ``message_end``, emit a
                         ``tool_execution_start``/``tool_execution_end`` pair
                         for a ``bash`` tool call whose result text looks like
                         a green ``vitest`` summary, exactly the shape
                         ``harness.report.ReportWatcher`` watches for.
``FAKE_PI_OUTPUT_TOKENS=<n>`` the assistant ``message_end``'s ``usage.output``
                         is ``<n>`` instead of the default ``42``, so budget
                         tests can drive a specific cumulative-output number.
``FAKE_PI_ERROR_ONCE=1`` only the *first* prompt of this process fails the way
                         ``FAKE_PI_ERROR`` does, so a resume prompt sent into
                         the same session then succeeds.

Missions-mode knobs (PHASE3_DESIGN.md §5)
-----------------------------------------
``FAKE_PI_PROMPT_LOG=<file>`` append one JSON line per prompt received:
                         ``{"n", "t", "pid", "text", "argv"}`` -- the prompt
                         index within this process, a wall-clock timestamp (so
                         a test can assert the parallel stagger), the pid (so a
                         test can tell one session's prompts from another's),
                         the prompt text (so a test can assert a brief) and this
                         process's argv (so a test can assert ``--tools``).
                         Appends are ``flock``-guarded, so several fake Pi
                         processes may share one log.
``FAKE_PI_WRITE_ON_PROMPT=<spec>`` ``target=source[;target=source...]``: when a
                         prompt's text mentions ``target`` (a path relative to
                         the cwd), copy ``source`` to it before settling. This
                         is how the fake "Builder" writes a config file and the
                         fake "Tester" writes a test file.
``FAKE_PI_SETTLE_DELAY=<sec>`` wait ``<sec>`` seconds between accepting a prompt
                         (its response, prompt-log line and file copies are all
                         done first) and the assistant turn that settles it, so
                         a parallelism test can hold both sessions in flight and
                         still see what each was asked. Adds to ``FAKE_PI_SLOW``
                         rather than replacing it; either knob alone works.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:  # POSIX only; the fallback is a plain append, which is still atomic enough
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

BIG_LINE_BYTES = 100 * 1024

#: Matches the ``provider``/``responseModel`` fields real Pi puts on an
#: assistant ``message_end`` (src/usage.ts's ``callFromEvent``), so the
#: derived call has a real, non-"unknown" model string.
FAKE_PROVIDER = "fake-provider"
FAKE_MODEL = "fake-model"

_EMIT_LOCK = threading.Lock()
_MIRROR: Optional[Any] = None

_ABORTED = threading.Event()
_SETTLED = threading.Event()

#: Every assistant message this process emitted on stdout, in emission order,
#: so ``_write_session_file`` can mirror them into ``session.jsonl`` with
#: identical usage -- the audit artifact ``tools/verify-telemetry.ts`` reads
#: (contract C9). Guarded by ``_EMIT_LOCK`` alongside the stdout writes.
_ASSISTANT_MESSAGES: List[Dict[str, Any]] = []


#: This process's argv, recorded once in :func:`main` so the prompt log can
#: carry the flags the harness spawned this session with (``--tools``, ...).
_ARGV: List[str] = []

#: Prompts received by this process, for the prompt log's ``n``.
_PROMPT_SEQ = 0
_PROMPT_LOG_LOCK = threading.Lock()


def _flag(name: str) -> bool:
    return os.environ.get(name) == "1"


def _seconds(name: str) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or 0.0))
    except ValueError:
        return 0.0


def _log_prompt(text: str) -> None:
    """Append one ``{"n", "t", "pid", "text", "argv"}`` line to the prompt log.

    Several fake Pi processes share one log in the parallel tests, so the
    append is taken under ``flock`` as well as the in-process lock: one record
    per write, no interleaving, whatever the brief's size.
    """
    path = os.environ.get("FAKE_PI_PROMPT_LOG")
    if not path:
        return
    global _PROMPT_SEQ
    with _PROMPT_LOG_LOCK:
        _PROMPT_SEQ += 1
        entry = {
            "n": _PROMPT_SEQ,
            "t": time.time(),
            "pid": os.getpid(),
            "text": text,
            "argv": list(_ARGV),
        }
        payload = (json.dumps(entry) + "\n").encode("utf-8")
        try:
            with open(path, "ab") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.write(payload)
                    handle.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _apply_writes(text: str) -> None:
    """``FAKE_PI_WRITE_ON_PROMPT=target=source[;...]``: the fake agent's write.

    A target is only written when the prompt actually names it, so one
    environment can serve both the Builder's and the Tester's mission: each
    session writes the file its own brief asks for and nothing else.
    """
    spec = os.environ.get("FAKE_PI_WRITE_ON_PROMPT")
    if not spec:
        return
    for pair in spec.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        target, _, source = pair.partition("=")
        target, source = target.strip(), source.strip()
        if not target or not source or target not in text:
            continue
        try:
            destination = pathlib.Path(target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, str(destination))
        except OSError as exc:
            sys.stderr.write("fake-pi: could not write {0}: {1}\n".format(target, exc))
            sys.stderr.flush()


def _session_dir(argv: List[str]) -> Optional[pathlib.Path]:
    for index, value in enumerate(argv):
        if value == "--session-dir" and index + 1 < len(argv):
            return pathlib.Path(argv[index + 1])
    return None


def _open_mirror(session_dir: Optional[pathlib.Path]) -> None:
    global _MIRROR
    if session_dir is None:
        return
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        _MIRROR = (session_dir / "emitted.jsonl").open("wb")
    except OSError:
        _MIRROR = None


def emit(event: Dict[str, Any]) -> None:
    """One record, one write, one flush -- the same rule the harness follows."""
    if event.get("type") == "message_end":
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            with _EMIT_LOCK:
                _ASSISTANT_MESSAGES.append(dict(message))
    payload = (json.dumps(event) + "\n").encode("utf-8")
    _emit_bytes(payload)


def emit_raw(line: str) -> None:
    _emit_bytes((line + "\n").encode("utf-8"))


def _emit_bytes(payload: bytes) -> None:
    with _EMIT_LOCK:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        if _MIRROR is not None:
            _MIRROR.write(payload)
            _MIRROR.flush()


def _usage(output: int) -> Dict[str, Any]:
    return {
        "input": 120 if output else 0,
        "output": output,
        "cacheRead": 2048 if output else 0,
        "cacheWrite": 64 if output else 0,
        "totalTokens": (120 + output + 2048 + 64) if output else 0,
        "cost": {"total": 0.0001 if output else 0.0},
    }


def _iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "{0:03d}Z".format(now.microsecond // 1000)


def _write_session_file(session_dir: Optional[pathlib.Path]) -> None:
    """Session header + the initial (fake) user turn. Called once, at startup."""
    if session_dir is None:
        return
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "fake-pi.pid").write_text(str(os.getpid()), encoding="utf-8")
        path = session_dir / "session.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "session",
                        "version": 3,
                        "id": "fake-session",
                        "timestamp": _iso_now(),
                        "cwd": os.getcwd(),
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "u0000001",
                        "parentId": None,
                        "timestamp": _iso_now(),
                        "message": {"role": "user", "content": "fake", "timestamp": int(time.time() * 1000)},
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def _finalize_session_file(session_dir: Optional[pathlib.Path]) -> None:
    """Appends one ``message`` entry per assistant turn emitted on stdout.

    Mirrors ``usage``/``stopReason`` byte for byte and carries the same
    ``provider``/``model`` fields, so ``tools/verify-telemetry.ts`` -- which
    derives its call list from ``session.jsonl`` alone, independent of
    ``events.jsonl`` -- reconciles against what this process actually emitted
    (contract C9). Called once, after every prompt has settled.
    """
    if session_dir is None:
        return
    with _EMIT_LOCK:
        messages = list(_ASSISTANT_MESSAGES)
    if not messages:
        return
    try:
        path = session_dir / "session.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            parent = "u0000001"
            for message in messages:
                entry_id = uuid.uuid4().hex[:8]
                session_message = dict(message)
                session_message.setdefault("api", "fake")
                session_message.setdefault("provider", FAKE_PROVIDER)
                session_message.setdefault("model", FAKE_MODEL)
                session_message.setdefault("timestamp", int(time.time() * 1000))
                handle.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": entry_id,
                            "parentId": parent,
                            "timestamp": _iso_now(),
                            "message": session_message,
                        }
                    )
                    + "\n"
                )
                parent = entry_id
    except OSError:
        pass


def _write_report() -> None:
    try:
        pathlib.Path("report.partial.json").write_text(
            json.dumps(
                {
                    "summary": "fake pi report",
                    "implemented_features": [],
                    "assumptions": [],
                    "tests_run": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


_PROMPTS_FINISHED = 0


def _finish_prompt() -> None:
    """Emit the assistant turn and settle.

    ``FAKE_PI_ERROR_ONCE=1`` fails only the first prompt of the process, so a
    resume prompt sent into the same session then succeeds.
    """
    lines = 0
    try:
        lines = int(os.environ.get("FAKE_PI_LINES") or 0)
    except ValueError:
        lines = 0
    for index in range(max(0, lines)):
        emit(
            {
                "type": "message_update",
                "index": index,
                "delta": {"type": "text", "text": "x" * BIG_LINE_BYTES},
            }
        )

    if _flag("FAKE_PI_GREEN_TESTS"):
        emit(
            {
                "type": "tool_execution_start",
                "toolCallId": "t1",
                "toolName": "bash",
                "args": {"command": "npm test"},
            }
        )
        emit(
            {
                "type": "tool_execution_end",
                "toolCallId": "t1",
                "toolName": "bash",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "\n Test Files  1 passed (1)\n      Tests  3 passed (3)\n",
                        }
                    ]
                },
                "isError": False,
            }
        )

    global _PROMPTS_FINISHED
    _PROMPTS_FINISHED += 1
    error_once = _flag("FAKE_PI_ERROR_ONCE") and _PROMPTS_FINISHED == 1
    try:
        output_tokens = int(os.environ.get("FAKE_PI_OUTPUT_TOKENS") or 42)
    except ValueError:
        output_tokens = 42
    if _flag("FAKE_PI_ERROR") or error_once:
        message = {
            "role": "assistant",
            "content": [],
            "provider": FAKE_PROVIDER,
            "responseModel": FAKE_MODEL,
            "model": FAKE_MODEL,
            "stopReason": "error",
            "errorMessage": '503 "Service Unavailable" (fake provider error)',
            "usage": _usage(0),
        }
    else:
        message = {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "provider": FAKE_PROVIDER,
            "responseModel": FAKE_MODEL,
            "model": FAKE_MODEL,
            "stopReason": "stop",
            "usage": _usage(output_tokens),
        }
    emit({"type": "message_end", "message": message})
    emit({"type": "turn_end", "message": message})
    emit({"type": "agent_end", "messages": [message], "willRetry": False})

    compaction = 0.0
    try:
        compaction = float(os.environ.get("FAKE_PI_COMPACT") or 0.0)
    except ValueError:
        compaction = 0.0
    if compaction > 0.0:
        # Exactly the real ordering: agent_end comes from inside agent.prompt(),
        # and auto-compaction runs afterwards -- silently -- before the agent
        # continues and agent_settled is finally emitted.
        emit({"type": "compaction_start", "reason": "threshold"})
        waited = 0.0
        while waited < compaction:
            if _ABORTED.is_set():
                return
            time.sleep(0.05)
            waited += 0.05
        emit({"type": "compaction_end", "reason": "threshold"})
        continued = {
            "role": "assistant",
            "content": [{"type": "text", "text": "built the rest"}],
            "provider": FAKE_PROVIDER,
            "responseModel": FAKE_MODEL,
            "model": FAKE_MODEL,
            "stopReason": "stop",
            "usage": _usage(400),
        }
        emit({"type": "message_end", "message": continued})
        emit({"type": "agent_end", "messages": [continued], "willRetry": False})

    emit({"type": "agent_settled"})
    _SETTLED.set()


def _handle_prompt(cid: Optional[str], text: str = "") -> None:
    emit(_response(cid, "prompt"))
    _log_prompt(text)
    _apply_writes(text)
    if _flag("FAKE_PI_GARBAGE"):
        emit_raw("this line is not json {{{")
    if _flag("FAKE_PI_HANG"):
        return
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    emit({"type": "message_start", "message": {"role": "user"}})

    delay = _seconds("FAKE_PI_SLOW") + _seconds("FAKE_PI_SETTLE_DELAY")
    waited = 0.0
    while waited < delay:
        if _ABORTED.is_set():
            return
        time.sleep(0.05)
        waited += 0.05
    if _ABORTED.is_set():
        return
    _finish_prompt()


def _response(cid: Optional[str], command: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    event: Dict[str, Any] = {"type": "response", "command": command, "success": True}
    if cid is not None:
        event = {"id": cid, **event}
    if data is not None:
        event["data"] = data
    return event


def _handle_abort(cid: Optional[str]) -> None:
    _ABORTED.set()
    message = {
        "role": "assistant",
        "content": [],
        "provider": FAKE_PROVIDER,
        "responseModel": FAKE_MODEL,
        "model": FAKE_MODEL,
        "stopReason": "aborted",
        "usage": _usage(0),
    }
    emit({"type": "message_end", "message": message})
    emit({"type": "agent_end", "messages": [message], "willRetry": False})
    emit({"type": "agent_settled"})
    _SETTLED.set()
    # The real Pi emits the abort response only after agent_settled.
    emit(_response(cid, "abort"))


def main(argv: List[str]) -> int:
    global _ARGV
    _ARGV = list(argv)
    session_dir = _session_dir(argv)
    _open_mirror(session_dir)
    _write_session_file(session_dir)
    if _flag("FAKE_PI_WRITE_REPORT"):
        _write_report()
    if _flag("FAKE_PI_HANG"):
        # Only SIGKILL may reap this process, so the fallback path is real.
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except (OSError, ValueError):
            pass

    sys.stderr.write("fake-pi: started with {0} args\n".format(len(argv)))
    sys.stderr.flush()

    workers: List[threading.Thread] = []
    stream = sys.stdin.buffer
    pending = b""
    while True:
        chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
        if not chunk:
            break
        pending += chunk
        while True:
            index = pending.find(b"\n")
            if index < 0:
                break
            record = pending[:index]
            pending = pending[index + 1:]
            worker = _dispatch(record)
            if worker is not None:
                workers.append(worker)

    if _flag("FAKE_PI_HANG"):
        # Ignore stdin EOF as well; wait to be killed.
        while True:
            time.sleep(0.25)

    for worker in workers:
        worker.join(timeout=5.0)
    _finalize_session_file(session_dir)
    if _MIRROR is not None:
        try:
            _MIRROR.close()
        except OSError:
            pass
    return 0


def _dispatch(record: bytes) -> Optional[threading.Thread]:
    if record.endswith(b"\r"):
        record = record[:-1]
    if not record.strip():
        return None
    try:
        command = json.loads(record.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(command, dict):
        return None
    cid = command.get("id")
    kind = command.get("type")
    if kind == "prompt":
        message = command.get("message")
        text = message if isinstance(message, str) else ""
        worker = threading.Thread(target=_handle_prompt, args=(cid, text), daemon=True)
        worker.start()
        return worker
    if kind == "abort":
        _handle_abort(cid)
        return None
    if kind in ("set_auto_retry", "set_thinking_level", "get_session_stats", "get_last_assistant_text"):
        data: Optional[Dict[str, Any]] = None
        if kind == "get_session_stats":
            data = {"messageCount": 1}
        elif kind == "get_last_assistant_text":
            data = {"text": "ok"}
        emit(_response(cid, kind, data))
        return None
    emit(_response(cid, str(kind)))
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
