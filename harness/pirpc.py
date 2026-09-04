"""A minimal client for ``pi --mode rpc``. Standard library only.

This is the primitive the harness is built on: one long-lived Pi process driven
by JSON lines on stdin/stdout. Every byte Pi writes to stdout is forwarded
unmodified to the harness's own stdout, which the runner captures into
``artifacts/runs/<id>/events.jsonl``.

Framing follows ``docs/rpc.md``: LF is the only record delimiter, an optional
trailing CR is stripped, and generic line readers (which also split on U+2028 /
U+2029) must not be used.

Forwarding rules, from the measured stdout-integrity work:

- one *complete* record per call: write in a loop that honours the short-write
  return value, then flush. The runner forces ``PYTHONUNBUFFERED=1``
  (``harnessChildEnvironment`` in ``src/run-challenge.ts``), which makes
  ``sys.stdout.buffer`` a raw ``_io.FileIO`` rather than a ``BufferedWriter``:
  one ``write()`` is one ``write(2)``, and a signal landing while a record larger
  than the pipe's free space is in flight returns a short count (measured:
  32768 of 200000). Ignoring that count truncates the record and merges it with
  the next one, so the loop is load-bearing, not defensive;
- never ``print()`` (it splits payload from newline), never ``os.write``, never a
  dup'd unbuffered fd;
- a module-level lock so concurrent sessions cannot interleave;
- malformed records are forwarded too, but never queued as events.

Process rules:

- **never** ``start_new_session`` and **never** ``preexec_fn``. ``signalProcessTree``
  in ``src/process-tree.ts`` kills the harness's process group; Pi must stay in it.
- ``stdin=PIPE`` is required by RPC mode, and closing stdin is the only shutdown
  path that makes Pi flush its stdout (its SIGTERM path does not).
- every blocking wait is at most :data:`POLL_SLICE_S` so a SIGTERM aimed at the
  harness is observed inside the runner's 5 s grace.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

from .log import log, warn
from .usage import Usage, is_success_message, message_text

#: No blocking wait may exceed this, so signals are observed promptly.
POLL_SLICE_S = 0.25

#: How much of the pipe we take per read.
READ_CHUNK = 65536

#: Serialises whole-record writes across every session in this process.
_STDOUT_LOCK = threading.Lock()


class PiRpcError(RuntimeError):
    """Any RPC-level failure: timeout, refused command, dead child."""


class PiRpcInterrupted(PiRpcError):
    """The stop event fired (the harness was asked to shut down)."""


def forward_record(record: bytes) -> None:
    """Forward one complete record to stdout, in full, under the lock.

    ``PYTHONUNBUFFERED=1`` is forced by the runner, so ``sys.stdout.buffer`` is a
    raw ``_io.FileIO``: its ``write()`` is a single ``write(2)`` that returns a
    short count when a signal lands mid-write on a full pipe. Honouring the
    return value is what keeps a record from being truncated and merged with the
    next one. The stream is resolved inside the call so a reassigned stdout (and
    the unit tests' fake buffer) still work.
    """
    payload = memoryview(record + b"\n")
    with _STDOUT_LOCK:
        try:
            stream = sys.stdout.buffer
            while payload:
                written = stream.write(payload)
                if not written:
                    # ``None`` means "would block" on a raw writer, ``0`` means no
                    # progress. Neither can happen on the runner's blocking pipe,
                    # but never spin, and never truncate silently.
                    warn(
                        "stdout accepted no bytes; {0} of {1} not forwarded".format(
                            len(payload), len(record) + 1
                        )
                    )
                    break
                payload = payload[written:]
            stream.flush()
        except (OSError, ValueError):
            # A closed or broken stdout must not take the session down; the
            # runner has already stopped reading in that case.
            pass


def pi_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for a Pi child: the harness environment plus ``PI_OFFLINE=1``.

    ``PI_CODING_AGENT_DIR`` is deliberately **never** set here. If the organizers
    set it, it is inherited verbatim and their configuration wins.
    """
    env = dict(os.environ)
    env["PI_OFFLINE"] = "1"
    if extra:
        env.update(extra)
    return env


def base_args(
    *,
    append_system: Optional[str] = None,
    session_dir: Union[str, pathlib.Path, None] = None,
    extensions: Optional[List[Union[str, pathlib.Path]]] = None,
    skill: Union[str, pathlib.Path, None] = None,
    tools: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    thinking: str = "off",
) -> List[str]:
    """The starter's ``buildPiArguments`` flag set, parameterised.

    Order matches ``src/run-challenge.ts`` so the two invocations stay comparable
    (``--mode`` is prepended by :class:`PiRpc`, and the prompt is sent over RPC
    rather than passed as a positional argument).

    ``tools`` is the only addition to the starter's set: a mission session gets
    exactly the tools its brief needs (``"read,write,edit"``), which is both a
    guard rail -- no ``bash``, so a mission cannot spend a call running
    ``npm test`` the harness runs for free -- and a smaller tool schema in every
    request's prefix. ``None`` (the default, and what the ``--agent pi`` control
    path uses) emits no flag at all, so Pi keeps its full tool set.
    """
    args: List[str] = [
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
    ]
    if append_system is not None:
        args += ["--append-system-prompt", append_system]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]
    for extension in extensions or []:
        args += ["--extension", str(extension)]
    if skill is not None:
        args += ["--skill", str(skill)]
    if tools is not None:
        args += ["--tools", str(tools)]
    if provider:
        args += ["--provider", provider]
    if model:
        args += ["--model", model]
    args += ["--thinking", thinking]
    return args


class PiRpc:
    """One ``pi --mode rpc`` subprocess, driven over JSON lines."""

    def __init__(
        self,
        *,
        pi_bin: Union[str, pathlib.Path],
        args: List[str],
        cwd: Union[str, pathlib.Path],
        env: Dict[str, str],
        session_dir: Union[str, pathlib.Path],
        label: str,
        stderr_path: Union[str, pathlib.Path],
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.label = label
        self.session_dir = pathlib.Path(session_dir)
        self.stderr_path = pathlib.Path(stderr_path)
        self.stop_event = stop_event if stop_event is not None else threading.Event()

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_fh = self.stderr_path.open("ab")

        self.argv: List[str] = [str(pi_bin), "--mode", "rpc", *args]
        # No start_new_session, no preexec_fn: Pi must stay inside the harness's
        # process group so the runner's group kill reaches it.
        self.proc = subprocess.Popen(
            self.argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_fh,
        )

        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._buffer: List[Optional[Dict[str, Any]]] = []
        self._n = 0
        self._eof = False
        self._closed = False
        # A mission's worker thread and MissionRunner.close() can both reach
        # close() for the same child during shutdown; the `_closed` check is
        # not atomic on its own, and two concurrent wind-downs would race on
        # stdin, the reader join and the stderr handle.
        self._close_lock = threading.Lock()
        self.exit_code: Optional[int] = None
        self.total = Usage()
        self.timeline: List[Dict[str, Any]] = []
        self.malformed_lines = 0
        self.forwarded_records = 0

        self._reader = threading.Thread(
            target=self._pump, name="pirpc-{0}".format(label), daemon=True
        )
        self._reader.start()

    # -- transport ---------------------------------------------------------

    def _pump(self) -> None:
        """Read raw bytes, split on LF only, forward every record, queue events."""
        stream = self.proc.stdout
        pending = b""
        try:
            if stream is None:
                return
            while True:
                chunk = stream.read1(READ_CHUNK) if hasattr(stream, "read1") else stream.read(READ_CHUNK)
                if not chunk:
                    break
                pending += chunk
                while True:
                    index = pending.find(b"\n")
                    if index < 0:
                        break
                    record = pending[:index]
                    pending = pending[index + 1:]
                    self._handle_record(record)
        except (OSError, ValueError):
            pass
        finally:
            if pending:
                # Pi died mid-record; forward what arrived rather than lose it.
                self._handle_record(pending)
            self._q.put(None)

    def _handle_record(self, record: bytes) -> None:
        if record.endswith(b"\r"):
            record = record[:-1]
        if not record:
            return
        forward_record(record)
        self.forwarded_records += 1
        try:
            event = json.loads(record.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.malformed_lines += 1
            return
        if isinstance(event, dict):
            self._q.put(event)
        else:
            self.malformed_lines += 1

    def _send(self, command: Dict[str, Any]) -> str:
        stdin = self.proc.stdin
        if stdin is None:
            raise PiRpcError("Pi stdin is not available")
        self._n += 1
        cid = "req-{0}".format(self._n)
        payload = json.dumps({"id": cid, **command}) + "\n"
        try:
            stdin.write(payload.encode("utf-8"))
            stdin.flush()
        except (OSError, ValueError) as exc:
            raise PiRpcError("failed to write {0} to Pi: {1}".format(command.get("type"), exc))
        return cid

    def _next(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Next event in arrival order: buffered ones first, then the queue."""
        if self._buffer:
            return self._buffer.pop(0)
        return self._q.get(timeout=max(0.01, timeout))

    def _wait_response(self, cid: str, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        held: List[Optional[Dict[str, Any]]] = []
        try:
            while True:
                if self.stop_event.is_set():
                    raise PiRpcInterrupted("shutdown requested while awaiting {0}".format(cid))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = self._next(min(POLL_SLICE_S, remaining))
                except queue.Empty:
                    continue
                if event is None:
                    self._eof = True
                    raise PiRpcError("Pi exited before responding to {0}".format(cid))
                if event.get("type") == "response" and event.get("id") == cid:
                    return event
                held.append(event)
        finally:
            # Preserve arrival order for whoever reads next.
            self._buffer = held + self._buffer
        raise PiRpcError("timeout waiting for response to {0}".format(cid))

    def drain(self) -> List[Dict[str, Any]]:
        """Consume everything already arrived.

        Called before a new prompt so a stale ``agent_settled`` from the previous
        run cannot end the new one early.
        """
        out: List[Dict[str, Any]] = [e for e in self._buffer if e is not None]
        self._buffer = []
        while True:
            try:
                event = self._q.get_nowait()
            except queue.Empty:
                break
            if event is None:
                self._eof = True
                self._q.put(None)  # keep the EOF sentinel for later waits
                break
            out.append(event)
        return out

    def command(self, command: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
        started = time.monotonic()
        cid = self._send(command)
        response = self._wait_response(cid, timeout)
        self.timeline.append(
            {
                "cmd": command.get("type"),
                "ok": bool(response.get("success")),
                "s": round(time.monotonic() - started, 3),
            }
        )
        if not response.get("success"):
            raise PiRpcError(
                "{0} failed: {1}".format(command.get("type"), response.get("error") or response)
            )
        return response

    # -- high-level --------------------------------------------------------

    def set_auto_retry(self, enabled: bool, timeout: float = 30.0) -> None:
        """Pi's own auto-retry can burn 4 x 300 s on a hung provider call."""
        self.command({"type": "set_auto_retry", "enabled": enabled}, timeout=timeout)

    def prompt(
        self,
        text: str,
        timeout: float,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Send a prompt and block until the agent settles or the budget expires.

        ``timeout`` is the whole per-call budget and is also the prompt-acceptance
        timeout: preflight runs an auth check and may run an entire compaction
        LLM call before the ``response`` for the prompt command is emitted.

        Only ``agent_settled`` means "done". ``agent_end`` is emitted from inside
        ``agent.prompt()``, *before* Pi checks for auto-compaction: the run then
        emits ``compaction_start``, makes a silent summarization LLM call and
        continues the build, with ``agent_settled`` arriving only in the
        ``finally`` of ``_runAgentPrompt``. Treating ``agent_end`` as terminal
        killed Pi mid-compaction and reported the half-built app as a success, so
        the per-prompt budget is the only backstop here.

        ``on_event``, when given, is called with every event this prompt sees
        (in arrival order, including ``message_end`` and ``agent_settled``
        themselves) so a caller can watch the live stream -- budget accounting,
        the report watcher -- without re-parsing stdout. A raising callback is
        logged and otherwise ignored: an observer must never take the session
        down.
        """
        started = time.monotonic()
        deadline = started + timeout
        self.drain()

        usage = Usage()
        last_text = ""
        error: Optional[str] = None
        stop_reason: Optional[str] = None
        success = False
        settled = False
        interrupted = False

        accepted = True
        try:
            self.command({"type": "prompt", "message": text}, timeout=timeout)
        except PiRpcInterrupted:
            accepted = False
            interrupted = True
        except PiRpcError as exc:
            accepted = False
            error = str(exc)

        while accepted:
            if self.stop_event.is_set():
                interrupted = True
                break
            now = time.monotonic()
            if now >= deadline:
                break
            wait = min(POLL_SLICE_S, deadline - now)
            try:
                event = self._next(wait)
            except queue.Empty:
                continue
            if event is None:
                self._eof = True
                error = "Pi exited mid-prompt"
                break
            if on_event is not None:
                try:
                    on_event(event)
                except Exception as exc:  # noqa: BLE001 - an observer must never break the session
                    warn("prompt on_event callback failed: {0}".format(exc))
            kind = event.get("type")
            if kind == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    payload = message.get("usage")
                    payload = payload if isinstance(payload, dict) else {}
                    usage.add(payload)
                    self.total.add(payload)
                    last_text = message_text(message) or last_text
                    reason = message.get("stopReason")
                    stop_reason = None if reason is None else str(reason)
                    if stop_reason == "error":
                        error = str(message.get("errorMessage") or "provider error")
                    if is_success_message(message):
                        success = True
            elif kind == "agent_settled":
                settled = True
                break

        wall = time.monotonic() - started
        result = {
            "settled": settled,
            "interrupted": interrupted,
            "timed_out": not settled and not interrupted and error is None,
            "success": success,
            "wall_s": wall,
            "usage": usage,
            "text": last_text,
            "error": error,
            "stop_reason": stop_reason,
        }
        self.timeline.append(
            {"cmd": "prompt", "settled": settled, "s": round(wall, 3), **usage.as_dict()}
        )
        return result

    def last_text(self) -> Optional[str]:
        data = self.command({"type": "get_last_assistant_text"}).get("data") or {}
        return data.get("text")

    def stats(self) -> Dict[str, Any]:
        return self.command({"type": "get_session_stats"}).get("data") or {}

    def set_thinking(self, level: str) -> None:
        self.command({"type": "set_thinking_level", "level": level})

    def abort(self, grace: float = 5.0) -> bool:
        """Ask Pi to abort the current run and wait briefly for acknowledgement.

        The ``abort`` response arrives only *after* ``agent_settled``, so either
        of the two ends the wait. Returns ``False`` when neither arrived, in
        which case the caller should fall through to :meth:`close`.
        """
        if self.proc.poll() is not None:
            return True
        try:
            cid = self._send({"type": "abort"})
        except PiRpcError as exc:
            warn("{0}: abort could not be sent ({1})".format(self.label, exc))
            return False
        deadline = time.monotonic() + grace
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                event = self._next(min(POLL_SLICE_S, remaining))
            except queue.Empty:
                continue
            if event is None:
                self._eof = True
                return True
            kind = event.get("type")
            if kind == "agent_settled":
                return True
            if kind == "response" and event.get("id") == cid:
                return True

    def _wait_for_exit(self, seconds: float) -> Optional[int]:
        deadline = time.monotonic() + seconds
        while True:
            code = self.proc.poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(POLL_SLICE_S, max(0.01, deadline - time.monotonic())))

    def close(
        self,
        *,
        stdin_grace: float = 3.0,
        term_grace: float = 3.0,
        kill_grace: float = 2.0,
    ) -> Optional[int]:
        """Shut the child down: stdin EOF, then SIGTERM, then SIGKILL.

        Closing stdin first is deliberate -- Pi flushes its stdout on stdin EOF
        but **not** on SIGTERM.

        Serialised: a second caller waits for the first wind-down (bounded by
        the three graces) and then sees the exit code, instead of racing it.
        """
        with self._close_lock:
            return self._close(stdin_grace, term_grace, kill_grace)

    def _close(self, stdin_grace: float, term_grace: float, kill_grace: float) -> Optional[int]:
        if self._closed:
            return self.exit_code
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass

        code = self._wait_for_exit(stdin_grace)
        if code is None:
            log("pi", "{0}: no exit after stdin EOF, sending SIGTERM".format(self.label))
            try:
                self.proc.terminate()
            except (OSError, ValueError):
                pass
            code = self._wait_for_exit(term_grace)
        if code is None:
            log("pi", "{0}: no exit after SIGTERM, sending SIGKILL".format(self.label))
            try:
                self.proc.kill()
            except (OSError, ValueError):
                pass
            code = self._wait_for_exit(kill_grace)

        self._reader.join(timeout=1.0)
        try:
            self._stderr_fh.close()
        except (OSError, ValueError):
            pass
        self._closed = True
        self.exit_code = code
        return code

    def __enter__(self) -> "PiRpc":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
