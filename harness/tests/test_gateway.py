"""Unit tests for :mod:`harness.gateway` against a scripted fake endpoint.

No network beyond ``127.0.0.1``. ``forward_record`` is exercised for real (not
monkeypatched) in every test via a fake ``sys.stdout.buffer`` -- the same
pattern ``harness/tests/test_pirpc.py`` uses -- so the synthetic ``message_end``
line is proven to actually reach the forwarder, not just to be constructed
correctly in isolation.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from harness import gateway, pirpc
from harness.tests import support
from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response


class _GatewayTestCase(unittest.TestCase):
    """Base: a temp harness dir, a running fake server, and a redirected forwarder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.harness_dir = pathlib.Path(self._tmp.name) / "harness"
        self.server = FakeGatewayServer()
        self.server.start()
        self.sink = io.BytesIO()
        self._stdout_patch = mock.patch.object(pirpc.sys, "stdout", mock.Mock(buffer=self.sink))
        self._stdout_patch.start()

    def tearDown(self) -> None:
        self._stdout_patch.stop()
        self.server.stop()
        self._tmp.cleanup()

    def make_client(self, **kwargs) -> gateway.GatewayClient:
        defaults = dict(
            base_url=self.server.base_url,
            api_key="test-key",
            model="zai-org/GLM-5.2",
            provider="berget",
            harness_dir=self.harness_dir,
            backoff=(0.01, 0.01, 0.01, 0.01),
        )
        defaults.update(kwargs)
        return gateway.GatewayClient(**defaults)

    def emitted_lines(self):
        """Every ``message_end`` line the forwarder actually wrote, parsed."""
        raw = self.sink.getvalue()
        return [json.loads(line) for line in raw.split(b"\n") if line]

    def raw_records(self):
        path = self.harness_dir / "direct-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class SuccessPathTest(_GatewayTestCase):
    def test_success_parses_usage_and_writes_one_raw_record_and_one_synthetic_line(self):
        self.server.script(
            [ScriptedResponse(status=200, body=ok_response(content="hello", prompt_tokens=100,
                                                             completion_tokens=20, cached_tokens=15,
                                                             response_id="resp-abc"))]
        )
        client = self.make_client()
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.response_id, "resp-abc")
        self.assertEqual(result.text, "hello")
        # input = prompt_tokens - cached_tokens, never negative.
        self.assertEqual(result.usage, {
            "input": 85, "output": 20, "cacheRead": 15, "cacheWrite": 0, "totalTokens": 120,
        })

        raw = self.raw_records()
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["usage"], result.usage)
        self.assertEqual(raw[0]["status"], 200)
        self.assertEqual(raw[0]["call_id"], result.call_id)
        self.assertIn("messages_sha256", raw[0]["request_meta"])
        self.assertEqual(raw[0]["request_meta"]["roles"], ["user"])

        lines = self.emitted_lines()
        self.assertEqual(len(lines), 1)
        message = lines[0]["message"]
        self.assertEqual(lines[0]["type"], "message_end")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["source"], "direct-gateway")
        self.assertEqual(message["provider"], "berget")
        self.assertEqual(message["responseModel"], "zai-org/GLM-5.2")
        self.assertEqual(message["model"], "zai-org/GLM-5.2")
        self.assertEqual(message["stopReason"], "stop")
        self.assertEqual(message["attempt"], 1)
        self.assertEqual(message["call_id"], result.call_id)
        self.assertEqual(message["provider_response_id"], "resp-abc")
        self.assertNotIn("errorMessage", message)
        self.assertEqual(message["usage"]["input"], 85)
        self.assertEqual(message["usage"]["output"], 20)
        self.assertEqual(message["usage"]["cacheRead"], 15)
        self.assertEqual(message["usage"]["cacheWrite"], 0)
        self.assertEqual(message["usage"]["totalTokens"], 120)
        self.assertIn("cost", message["usage"])
        self.assertIsInstance(message["usage"]["cost"]["total"], float)
        self.assertIsInstance(message["timestamp"], int)


class RetryTest(_GatewayTestCase):
    def test_503_then_200_is_two_attempts_two_records_first_error_zero_usage(self):
        self.server.script([
            ScriptedResponse(status=503, body={"error": "busy"}),
            ScriptedResponse(status=200, body=ok_response(content="ok")),
        ])
        client = self.make_client()
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)

        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)

        raw = self.raw_records()
        self.assertEqual(len(raw), 2)
        self.assertEqual(raw[0]["status"], 503)
        self.assertEqual(raw[0]["attempt"], 1)
        self.assertEqual(raw[1]["status"], 200)
        self.assertEqual(raw[1]["attempt"], 2)
        self.assertEqual(raw[0]["call_id"], raw[1]["call_id"], "same attempt sequence")

        lines = self.emitted_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["message"]["stopReason"], "error")
        self.assertEqual(lines[0]["message"]["usage"], {
            "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
            "reasoning": 0, "cost": {"total": 0.0},
        })
        self.assertIn("errorMessage", lines[0]["message"])
        self.assertEqual(lines[1]["message"]["stopReason"], "stop")

    def test_400_is_one_attempt_no_retry_not_ok(self):
        self.server.script([ScriptedResponse(status=400, body={"error": "bad request"})])
        client = self.make_client()
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)

        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.status, 400)
        self.assertEqual(len(self.raw_records()), 1)
        self.assertEqual(len(self.emitted_lines()), 1)

    def test_connection_reset_then_200_is_retried(self):
        self.server.script([
            ScriptedResponse(reset=True),
            ScriptedResponse(status=200, body=ok_response(content="ok")),
        ])
        client = self.make_client()
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.attempts, 2)
        raw = self.raw_records()
        self.assertEqual(raw[0]["status"], 0)
        self.assertIsNotNone(raw[0]["error"])

    def test_four_consecutive_503s_fails_after_four_attempts(self):
        self.server.script([ScriptedResponse(status=503, body={"error": "busy"})] * 4)
        client = self.make_client()  # default backoff has 4 entries -> 4 attempts
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)

        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(len(self.raw_records()), 4)
        self.assertEqual(len(self.emitted_lines()), 4)
        for record in self.raw_records():
            self.assertEqual(record["status"], 503)


class ThinkingAndPayloadTest(_GatewayTestCase):
    def test_thinking_true_with_budget_sets_both_fields(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(content="ok"))])
        client = self.make_client()
        client.chat(
            [{"role": "user", "content": "hi"}], label="probe", max_tokens=50,
            thinking=True, thinking_token_budget=2048,
        )
        sent = self.server.requests[-1]["body"]
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(sent["thinking_token_budget"], 2048)

    def test_default_body_has_thinking_disabled_and_no_budget_field(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(content="ok"))])
        client = self.make_client()
        client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)
        sent = self.server.requests[-1]["body"]
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("thinking_token_budget", sent)
        self.assertEqual(sent["temperature"], 0)
        self.assertEqual(sent["stream"], False)
        self.assertEqual(sent["model"], "zai-org/GLM-5.2")


class JsonSchemaTest(_GatewayTestCase):
    def test_parse_failure_then_valid_is_two_attempts_total_and_quotes_the_error(self):
        self.server.script([
            ScriptedResponse(status=200, body=ok_response(content="not json at all")),
            ScriptedResponse(status=200, body=ok_response(content=json.dumps({"a": 1}))),
        ])
        client = self.make_client()
        obj, result = client.json_schema(
            [{"role": "user", "content": "hi"}],
            name="thing", schema={"type": "object"}, label="probe", max_tokens=50,
        )
        self.assertEqual(obj, {"a": 1})
        self.assertTrue(result.ok)
        self.assertEqual(len(self.server.requests), 2)
        # The retry's own attempt count is 1 (its own fresh call sequence); two
        # HTTP attempts happened in total across the two chat() calls.
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(self.raw_records()), 2)
        distinct_call_ids = {r["call_id"] for r in self.raw_records()}
        self.assertEqual(len(distinct_call_ids), 2, "retry is a new attempt sequence")

        retry_body = self.server.requests[-1]["body"]
        retry_text = json.dumps(retry_body["messages"])
        self.assertIn("not json at all", retry_text)
        self.assertIn("Parse error", retry_text)

    def test_valid_first_try_is_one_attempt(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(content='{"a": 2}'))])
        client = self.make_client()
        obj, result = client.json_schema(
            [{"role": "user", "content": "hi"}],
            name="thing", schema={"type": "object"}, label="probe", max_tokens=50,
        )
        self.assertEqual(obj, {"a": 2})
        self.assertEqual(len(self.server.requests), 1)

    def test_fenced_json_is_stripped(self):
        content = "```json\n{\"a\": 3}\n```"
        self.server.script([ScriptedResponse(status=200, body=ok_response(content=content))])
        client = self.make_client()
        obj, result = client.json_schema(
            [{"role": "user", "content": "hi"}],
            name="thing", schema={"type": "object"}, label="probe", max_tokens=50,
        )
        self.assertEqual(obj, {"a": 3})

    def test_request_carries_response_format(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(content='{"a": 1}'))])
        client = self.make_client()
        client.json_schema(
            [{"role": "user", "content": "hi"}],
            name="thing", schema={"type": "object", "properties": {}}, label="probe", max_tokens=50,
        )
        sent = self.server.requests[-1]["body"]
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["response_format"]["json_schema"]["name"], "thing")
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])


class DeadlineTest(_GatewayTestCase):
    def test_deadline_shorter_than_backoff_stops_early_with_error(self):
        self.server.script([ScriptedResponse(status=503, body={"error": "busy"})] * 4)
        client = self.make_client(backoff=(5, 5, 5, 5))
        deadline = time.monotonic() + 0.3
        result = client.chat(
            [{"role": "user", "content": "hi"}], label="probe", max_tokens=50, deadline=deadline,
        )
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        # Only the first attempt could run; the 5s backoff would blow the deadline.
        self.assertEqual(result.attempts, 1)
        self.assertLess(len(self.server.requests), 4)


class StopEventTest(_GatewayTestCase):
    """A shutdown signal mid-backoff must cut the wait short (harness-semantics
    finding "analyst-retry-sleep-not-interruptible"): every other blocking
    wait in the harness is <= 0.25s-sliced and ``stop_event``-aware, and the
    inter-attempt backoff sleep must be too.
    """

    def test_stop_event_set_during_backoff_ends_the_call_without_a_further_attempt(self):
        self.server.script([ScriptedResponse(status=503, body={"error": "busy"})] * 4)
        stop_event = threading.Event()
        client = self.make_client(backoff=(5, 5, 5, 5), stop_event=stop_event)

        def _set_stop_event_soon() -> None:
            time.sleep(0.05)
            stop_event.set()

        threading.Thread(target=_set_stop_event_soon, daemon=True).start()

        started = time.monotonic()
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)
        elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        # Only the first attempt's 503 could land before the stop event fired
        # mid-backoff; the unbounded 5s sleep would otherwise have blocked here.
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertLess(elapsed, 2.0)

    def test_no_stop_event_passed_behaves_like_a_plain_sleep(self):
        # The fallback private Event (never set) must not change behaviour for
        # a caller that predates this parameter.
        self.server.script([ScriptedResponse(status=503, body={"error": "busy"}), ScriptedResponse(status=200, body=ok_response())])
        client = self.make_client(backoff=(0.01, 0.01, 0.01, 0.01))
        result = client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)


class CostTableTest(_GatewayTestCase):
    def test_cost_computed_from_matching_model_id(self):
        self.server.script([
            ScriptedResponse(status=200, body=ok_response(prompt_tokens=1_000_000, completion_tokens=1_000_000,
                                                            cached_tokens=0, content="ok"))
        ])
        client = self.make_client(
            model="priced-model",
            cost_table={"priced-model": {"input": 2.0, "output": 3.0, "cacheRead": 1.0, "cacheWrite": 0.0}},
        )
        client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)
        lines = self.emitted_lines()
        self.assertAlmostEqual(lines[0]["message"]["usage"]["cost"]["total"], 5.0)

    def test_unknown_model_costs_zero(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(prompt_tokens=1000, completion_tokens=1000))])
        client = self.make_client(model="unknown/model", cost_table={})
        client.chat([{"role": "user", "content": "hi"}], label="probe", max_tokens=50)
        lines = self.emitted_lines()
        self.assertEqual(lines[0]["message"]["usage"]["cost"]["total"], 0.0)


if __name__ == "__main__":
    unittest.main()
