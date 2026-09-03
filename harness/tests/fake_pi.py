#!/usr/bin/env python3
"""A deterministic stand-in for ``pi --mode rpc``. **Test use only.**

Point the harness at it with ``HARNESS_PI_BIN=<this file>``. It speaks just
enough of the RPC protocol (``docs/rpc.md``) to exercise the harness end to end
without a model, a network call, or a token spent.

It accepts and ignores every real Pi flag, but reads ``--session-dir`` so it can
write a non-empty session JSONL where the organizer artifact audit looks for one.
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
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

BIG_LINE_BYTES = 100 * 1024

_EMIT_LOCK = threading.Lock()
_MIRROR: Optional[Any] = None

_ABORTED = threading.Event()
_SETTLED = threading.Event()


def _flag(name: str) -> bool:
    return os.environ.get(name) == "1"


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


def _write_session_file(session_dir: Optional[pathlib.Path]) -> None:
    if session_dir is None:
        return
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "fake-pi.pid").write_text(str(os.getpid()), encoding="utf-8")
        path = session_dir / "session.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "session", "id": "fake-session", "cwd": os.getcwd()}) + "\n")
            handle.write(
                json.dumps({"type": "message", "message": {"role": "user", "content": "fake"}}) + "\n"
            )
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

    global _PROMPTS_FINISHED
    _PROMPTS_FINISHED += 1
    error_once = _flag("FAKE_PI_ERROR_ONCE") and _PROMPTS_FINISHED == 1
    if _flag("FAKE_PI_ERROR") or error_once:
        message = {
            "role": "assistant",
            "content": [],
            "stopReason": "error",
            "errorMessage": '503 "Service Unavailable" (fake provider error)',
            "usage": _usage(0),
        }
    else:
        message = {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stopReason": "stop",
            "usage": _usage(42),
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
            "stopReason": "stop",
            "usage": _usage(400),
        }
        emit({"type": "message_end", "message": continued})
        emit({"type": "agent_end", "messages": [continued], "willRetry": False})

    emit({"type": "agent_settled"})
    _SETTLED.set()


def _handle_prompt(cid: Optional[str]) -> None:
    emit(_response(cid, "prompt"))
    if _flag("FAKE_PI_GARBAGE"):
        emit_raw("this line is not json {{{")
    if _flag("FAKE_PI_HANG"):
        return
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    emit({"type": "message_start", "message": {"role": "user"}})

    delay = 0.0
    try:
        delay = float(os.environ.get("FAKE_PI_SLOW") or 0.0)
    except ValueError:
        delay = 0.0
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
        worker = threading.Thread(target=_handle_prompt, args=(cid,), daemon=True)
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
