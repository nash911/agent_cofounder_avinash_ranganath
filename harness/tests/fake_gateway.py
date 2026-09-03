"""A scripted fake OpenAI-compatible endpoint for :mod:`harness.gateway` tests.

**Test use only.** An ``http.server``-based fixture, started on a free
``127.0.0.1`` port in a background thread, that serves a queue of
:class:`ScriptedResponse` objects in order -- one per POST received -- so a
test can script exactly the attempt sequence a retry policy should walk
through (a 503 then a 200, a connection reset then a 200, four 503s, ...).

Every received request body is recorded in ``.requests`` so a test can assert
on what the client actually sent (payload fields, retry-with-quoted-error
messages, thinking flags).
"""

from __future__ import annotations

import http.server
import json
import socket
import struct
import sys
import threading
import time
from typing import Any, Dict, List, Optional


class ScriptedResponse:
    """One queued reply. ``reset`` forces an abrupt RST instead of a response."""

    def __init__(
        self,
        status: int = 200,
        body: Optional[Dict[str, Any]] = None,
        delay: float = 0.0,
        reset: bool = False,
    ) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.delay = delay
        self.reset = reset


def ok_response(
    *,
    content: str = "{}",
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cached_tokens: int = 0,
    response_id: str = "resp-1",
) -> Dict[str, Any]:
    """A minimal OpenAI-shaped 200 body, usage included."""
    return {
        "id": response_id,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeGateway/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 -- stdlib signature
        pass  # silence -- tests must never depend on server console chatter

    def do_POST(self) -> None:  # noqa: N802 -- stdlib handler name
        server: "FakeGatewayServer" = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {"_non_json_body": raw[:4000].decode("utf-8", "replace")}

        with server.lock:
            server.requests.append(
                {
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": parsed,
                }
            )
            step = server.queue.pop(0) if server.queue else ScriptedResponse(status=200, body={})

        if step.delay:
            time.sleep(step.delay)

        if step.reset:
            # SO_LINGER(on=1, timeout=0) makes the kernel send RST on close
            # instead of the normal FIN, which is what turns into
            # ConnectionResetError / URLError on the client side.
            try:
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
            except OSError:
                pass
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return

        payload = json.dumps(step.body).encode("utf-8")
        try:
            self.send_response(step.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeGatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, queue: Optional[List[ScriptedResponse]] = None, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.lock = threading.Lock()
        self.queue: List[ScriptedResponse] = list(queue or [])
        self.requests: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:{0}".format(self.server_address[1])

    def script(self, responses: List[ScriptedResponse]) -> None:
        with self.lock:
            self.queue = list(responses)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "FakeGatewayServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _main(argv: Optional[List[str]] = None) -> int:
    """Standalone launcher for manual/integration smoke runs. **Test use only.**

    Starts the fake gateway on ``127.0.0.1``, scripted with repeated success
    responses shaped for the Analyst's one ``json_schema`` call (C4: app_name,
    tagline, summary, primary_entity), prints ``port=<n>`` to stdout so a
    driving shell command can capture it, then blocks until killed (SIGINT/
    SIGTERM/EOF on stdin).
    """
    import argparse

    parser = argparse.ArgumentParser(description="Standalone fake gateway server (test use only).")
    parser.add_argument("--port", type=int, default=0, help="bind port; 0 = OS-assigned (default)")
    args = parser.parse_args(argv)

    spec_content = json.dumps(
        {
            "app_name": "Fake App",
            "tagline": "A scripted tagline for integration smoke runs.",
            "summary": "A scripted summary produced by the fake gateway for end-to-end testing.",
            "primary_entity": "item",
        }
    )
    server = FakeGatewayServer(port=args.port)
    server.script([ScriptedResponse(status=200, body=ok_response(content=spec_content)) for _ in range(50)])
    server.start()
    print("port={0}".format(server.server_address[1]), flush=True)
    try:
        while True:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
