"""The direct gateway client: ``urllib``-only calls straight to the model gateway.

Ported from ``/home/nash/Dropbox/AgentCofounder/probes/common.py`` (``httpx``,
tested against Berget) to the standard library only, for the reasoning agents
(Analyst, Architect, Supervisor, Reviewer) that never need a Pi session.
Implements the cross-agent telemetry contract literally:

- **C1** -- one synthetic ``message_end`` line, tagged ``source":"direct-gateway"``,
  emitted through :func:`harness.pirpc.forward_record` for *every* HTTP attempt
  (including failed and retried ones), so the organizers' own event-stream parser
  counts direct calls exactly like Pi calls.
- **C2** -- one raw JSON line per attempt appended to
  ``<harness_dir>/direct-calls.jsonl`` *before* any caller (``json_schema``, the
  Analyst) parses the response content -- the audit artifact for the BYO track.
- **C3** -- retry on transport errors, HTTP 5xx and 429 with configurable
  backoff; never on other 4xx. ``json_schema`` strips ``` ```json `` `` fences and
  retries once on parse failure, quoting the parse error back to the model.

Every blocking wait here is either a single bounded HTTP request (``timeout_s``,
itself clamped to an optional caller ``deadline``) or a short ``time.sleep``
between attempts -- never an unbounded wait.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from .pirpc import forward_record

#: Serialises writes to ``direct-calls.jsonl`` across concurrent gateway calls
#: (Analyst/Architect/Supervisor/Reviewer may run from different threads).
_FILE_LOCK = threading.Lock()

#: A fenced code block wrapping an otherwise-valid JSON body, e.g. ```json\n{...}\n```.
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)

#: Transport-level failures that mean "no HTTP response arrived at all".
_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    socket.timeout,
    http.client.HTTPException,
    ConnectionResetError,
)


@dataclass
class GatewayResult:
    """Everything a caller needs from one :meth:`GatewayClient.chat` call.

    Reflects the *last* attempt only; the full attempt-by-attempt history lives
    in ``direct-calls.jsonl`` (C2), keyed by ``call_id``.
    """

    ok: bool
    text: str
    body: Dict[str, Any]
    usage: Dict[str, Any]
    status: int
    attempts: int
    error: Optional[str]
    response_id: Optional[str]
    call_id: str


class GatewayClient:
    """One configured route to the model gateway's ``/chat/completions``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        provider: str,
        harness_dir: Union[str, pathlib.Path],
        cost_table: Optional[Dict[str, Dict[str, float]]] = None,
        backoff: Tuple[float, ...] = (1, 2, 4, 8),
        timeout_s: float = 180,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.harness_dir = pathlib.Path(harness_dir)
        self.direct_calls_path = self.harness_dir / "direct-calls.jsonl"
        self.cost_table = cost_table if cost_table is not None else _default_cost_table()
        self.backoff = tuple(backoff) if backoff else (0.0,)
        self.timeout_s = float(timeout_s)
        #: Same fallback pattern as :class:`harness.pirpc.PiRpc`: an
        #: un-updated caller (or a test's ``make_client``) that never passes
        #: one gets a private ``Event`` that is never set, so the backoff wait
        #: below behaves exactly like a plain ``time.sleep`` for it.
        self.stop_event = stop_event if stop_event is not None else threading.Event()

    # -- public API ----------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        label: str,
        max_tokens: int,
        temperature: float = 0,
        thinking: bool = False,
        thinking_token_budget: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
    ) -> GatewayResult:
        """One logical call: builds the OpenAI-shaped payload, retries per C3.

        ``deadline`` is an optional ``time.monotonic()`` instant: no attempt is
        started once it has passed, and no backoff sleep is entered if it would
        run past it -- the call fails fast with whatever the last real attempt
        reported instead of silently overrunning the caller's budget.
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": bool(thinking)},
        }
        if thinking and thinking_token_budget is not None:
            payload["thinking_token_budget"] = thinking_token_budget
        if extra:
            payload.update(extra)
        return self._execute(payload, label=label, deadline=deadline)

    def json_schema(
        self,
        messages: List[Dict[str, Any]],
        *,
        name: str,
        schema: Dict[str, Any],
        label: str,
        max_tokens: int,
        temperature: float = 0,
        thinking: bool = False,
        thinking_token_budget: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
    ) -> Tuple[Optional[Any], GatewayResult]:
        """A ``chat`` call constrained to a JSON schema, with one parse retry.

        Every attempt of both the original call and the retry is logged and
        emitted exactly like any other :meth:`chat` call (C2/C1) -- the retry is
        simply a second, independent call sequence with its own ``call_id``.
        """
        request_extra: Dict[str, Any] = dict(extra) if extra else {}
        request_extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema, "strict": True},
        }
        result = self.chat(
            messages,
            label=label,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            thinking_token_budget=thinking_token_budget,
            extra=request_extra,
            deadline=deadline,
        )
        if not result.ok:
            return None, result

        obj, parse_error = _parse_json_content(result.text)
        if obj is not None:
            return obj, result

        retry_messages = list(messages) + [
            {"role": "assistant", "content": result.text},
            {
                "role": "user",
                "content": (
                    "That reply was not valid JSON matching the schema. "
                    "Parse error: {0}. Reply again with ONLY valid JSON matching "
                    "the schema -- no commentary, no code fences.".format(parse_error)
                ),
            },
        ]
        retry_result = self.chat(
            retry_messages,
            label="{0}-retry".format(label),
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            thinking_token_budget=thinking_token_budget,
            extra=request_extra,
            deadline=deadline,
        )
        if not retry_result.ok:
            return None, retry_result
        obj2, _ = _parse_json_content(retry_result.text)
        return obj2, retry_result

    # -- attempt loop ----------------------------------------------------

    def _execute(
        self, payload: Dict[str, Any], *, label: str, deadline: Optional[float]
    ) -> GatewayResult:
        call_id = str(uuid.uuid4())
        max_attempts = max(1, len(self.backoff))

        status = 0
        body: Dict[str, Any] = {}
        error: Optional[str] = "deadline exceeded before first attempt"
        usage = _zero_usage()
        response_id: Optional[str] = None
        attempts_done = 0

        for attempt in range(1, max_attempts + 1):
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            remaining = None if deadline is None else max(0.001, deadline - now)
            attempt_timeout = self.timeout_s if remaining is None else min(self.timeout_s, remaining)

            attempts_done = attempt
            started = time.monotonic()
            status, body, error = self._post(payload, attempt_timeout)
            latency_s = time.monotonic() - started
            usage = _extract_usage(body)
            response_id = body.get("id") if isinstance(body, dict) else None
            timestamp_ms = int(time.time() * 1000)

            self._append_raw(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "timestamp": timestamp_ms,
                    "call_id": call_id,
                    "attempt": attempt,
                    "label": label,
                    "model": self.model,
                    "provider": self.provider,
                    "status": status,
                    "latency_s": round(latency_s, 3),
                    "error": error,
                    "request_meta": _request_meta(payload),
                    "response_body": body,
                    "usage": usage,
                }
            )
            self._emit_synthetic(
                usage=usage,
                call_id=call_id,
                attempt=attempt,
                response_id=response_id,
                status=status,
                error=error,
                timestamp_ms=timestamp_ms,
            )

            if status == 200 and error is None:
                break
            retryable = status == 0 or status >= 500 or status == 429
            if not retryable or attempt >= max_attempts:
                break
            delay = self.backoff[attempt - 1] if attempt - 1 < len(self.backoff) else self.backoff[-1]
            if deadline is not None and time.monotonic() + delay >= deadline:
                break
            # Sliced to <= 0.25s per the harness-wide blocking-wait rule, and
            # breaks the outer attempt loop the moment shutdown is signalled
            # so no further HTTP attempt starts once the runner is winding
            # down -- the same shape as harness/__main__.py's resume backoff.
            waited = 0.0
            while waited < delay and not self.stop_event.is_set():
                slice_s = min(0.25, delay - waited)
                self.stop_event.wait(slice_s)
                waited += slice_s
            if self.stop_event.is_set():
                break

        ok = status == 200 and error is None
        text = _extract_text(body) if isinstance(body, dict) else ""
        return GatewayResult(
            ok=ok,
            text=text,
            body=body if isinstance(body, dict) else {},
            usage=usage,
            status=status,
            attempts=attempts_done,
            error=error,
            response_id=response_id,
            call_id=call_id,
        )

    # -- transport ---------------------------------------------------------

    def _post(self, payload: Dict[str, Any], timeout_s: float) -> Tuple[int, Dict[str, Any], Optional[str]]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=data,
            headers={
                "Authorization": "Bearer {0}".format(self.api_key),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        raw = b""
        try:
            with urllib.request.urlopen(request, timeout=max(0.001, timeout_s)) as response:
                status = response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 -- best-effort error body
                raw = b""
            finally:
                exc.close()
        except _TRANSPORT_ERRORS as exc:
            return 0, {}, "{0}: {1}".format(type(exc).__name__, exc)

        try:
            parsed: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {"_non_json_body": raw[:4000].decode("utf-8", "replace")}
        body: Dict[str, Any] = parsed if isinstance(parsed, dict) else {"_non_json_body": str(parsed)[:4000]}

        error: Optional[str] = None
        if status >= 400:
            error = "HTTP {0}: {1}".format(status, json.dumps(body, ensure_ascii=False)[:500])
        return status, body, error

    # -- persistence and emission ------------------------------------------

    def _append_raw(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        with _FILE_LOCK:
            with self.direct_calls_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _emit_synthetic(
        self,
        *,
        usage: Dict[str, Any],
        call_id: str,
        attempt: int,
        response_id: Optional[str],
        status: int,
        error: Optional[str],
        timestamp_ms: int,
    ) -> None:
        ok = status == 200 and error is None
        message: Dict[str, Any] = {
            "role": "assistant",
            "provider": self.provider,
            "responseModel": self.model,
            "model": self.model,
            "usage": {
                "input": usage["input"],
                "output": usage["output"],
                "cacheRead": usage["cacheRead"],
                "cacheWrite": usage["cacheWrite"],
                "totalTokens": usage["totalTokens"],
                "reasoning": 0,
                "cost": {"total": round(self._cost_for(usage), 6)},
            },
            "source": "direct-gateway",
            "call_id": call_id,
            "attempt": attempt,
            "provider_response_id": response_id,
            "stopReason": "stop" if ok else "error",
            "timestamp": timestamp_ms,
        }
        if not ok:
            message["errorMessage"] = error or "request failed"
        record = {"type": "message_end", "message": message}
        forward_record(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    def _cost_for(self, usage: Dict[str, Any]) -> float:
        entry = self.cost_table.get(self.model)
        if not isinstance(entry, dict):
            return 0.0
        return (
            usage["input"] / 1_000_000 * float(entry.get("input", 0.0) or 0.0)
            + usage["output"] / 1_000_000 * float(entry.get("output", 0.0) or 0.0)
            + usage["cacheRead"] / 1_000_000 * float(entry.get("cacheRead", 0.0) or 0.0)
        )


# --------------------------------------------------------------------------
# module-level helpers
# --------------------------------------------------------------------------


def _default_cost_table() -> Dict[str, Dict[str, float]]:
    """The per-million cost table baked into ``<repo>/.pi-agent/models.json``."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    models_path = repo_root / ".pi-agent" / "models.json"
    try:
        data = json.loads(models_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    table: Dict[str, Dict[str, float]] = {}
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return table
    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            continue
        for model in provider_cfg.get("models") or []:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            cost = model.get("cost")
            if isinstance(model_id, str) and isinstance(cost, dict):
                table[model_id] = {
                    "input": _as_float(cost.get("input")),
                    "output": _as_float(cost.get("output")),
                    "cacheRead": _as_float(cost.get("cacheRead")),
                    "cacheWrite": _as_float(cost.get("cacheWrite")),
                }
    return table


def _request_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    messages = payload.get("messages")
    messages = messages if isinstance(messages, list) else []
    blob = json.dumps(messages, sort_keys=True, ensure_ascii=False)
    return {
        "messages_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "messages_chars": len(blob),
        "roles": [m.get("role") if isinstance(m, dict) else None for m in messages],
        "params": {k: v for k, v in payload.items() if k != "messages"},
    }


def _extract_usage(body: Any) -> Dict[str, Any]:
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}

    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    cached = details.get("cached_tokens")
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens")
    cached = _as_int(cached)

    input_tokens = prompt - cached
    if input_tokens < 0:
        input_tokens = 0

    return {
        "input": input_tokens,
        "output": completion,
        "cacheRead": cached,
        "cacheWrite": 0,
        "totalTokens": prompt + completion,
    }


def _zero_usage() -> Dict[str, Any]:
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0}


def _extract_text(body: Dict[str, Any]) -> str:
    try:
        return body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


def _parse_json_content(text: str) -> Tuple[Optional[Any], Optional[str]]:
    candidate = _strip_fences(text or "")
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
