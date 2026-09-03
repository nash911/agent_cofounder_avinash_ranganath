"""Unit tests for the exit predicate and usage accumulation."""

from __future__ import annotations

import unittest

from harness.usage import (
    Usage,
    assistant_message_ends,
    is_success_message,
    last_stop_reason,
    message_text,
    parse_events,
    run_succeeded,
    summarize,
)


def assistant_end(stop_reason, output, **extra):
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "stopReason": stop_reason,
        "usage": {
            "input": 100,
            "output": output,
            "cacheRead": 900,
            "cacheWrite": 10,
            "totalTokens": 1010 + output,
            "cost": {"total": 0.001},
        },
    }
    message.update(extra)
    return {"type": "message_end", "message": message}


SUCCESS_EVENTS = [
    {"type": "response", "id": "req-1", "command": "prompt", "success": True},
    {"type": "agent_start"},
    assistant_end("tool_use", 120),
    assistant_end("stop", 40),
    {"type": "agent_settled"},
]

ALL_ERROR_EVENTS = [
    {"type": "response", "id": "req-1", "command": "prompt", "success": True},
    assistant_end("error", 0, errorMessage="503 from provider"),
    assistant_end("error", 0, errorMessage="503 from provider"),
    {"type": "agent_settled"},
]

ABORTED_EVENTS = [
    {"type": "response", "id": "req-1", "command": "prompt", "success": True},
    assistant_end("aborted", 0),
    {"type": "agent_settled"},
]


class ExitPredicateTest(unittest.TestCase):
    def test_success_list_exits_zero(self):
        self.assertTrue(run_succeeded(SUCCESS_EVENTS))

    def test_all_error_list_does_not_succeed(self):
        self.assertFalse(run_succeeded(ALL_ERROR_EVENTS))

    def test_aborted_list_does_not_succeed(self):
        self.assertFalse(run_succeeded(ABORTED_EVENTS))

    def test_zero_output_with_good_stop_reason_does_not_succeed(self):
        self.assertFalse(run_succeeded([assistant_end("stop", 0)]))

    def test_error_stop_reason_with_output_does_not_succeed(self):
        # An errored turn can still be reported with non-zero output; the stop
        # reason is authoritative.
        self.assertFalse(run_succeeded([assistant_end("error", 500)]))

    def test_empty_list_does_not_succeed(self):
        self.assertFalse(run_succeeded([]))

    def test_user_messages_are_ignored(self):
        events = [{"type": "message_end", "message": {"role": "user", "usage": {"output": 9}}}]
        self.assertFalse(run_succeeded(events))

    def test_missing_usage_object_does_not_succeed(self):
        self.assertFalse(is_success_message({"role": "assistant", "stopReason": "stop"}))

    def test_one_good_turn_among_errors_succeeds(self):
        events = list(ALL_ERROR_EVENTS) + [assistant_end("stop", 3)]
        self.assertTrue(run_succeeded(events))


class UsageTest(unittest.TestCase):
    def test_accumulates_pi_field_names(self):
        usage = Usage()
        usage.add({"input": 10, "output": 20, "cacheRead": 30, "cacheWrite": 40,
                   "totalTokens": 100, "reasoning": 5, "cost": {"total": 0.5}})
        usage.add({"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4,
                   "totalTokens": 10, "cost": {"total": 0.25}})
        self.assertEqual(usage.calls, 2)
        self.assertEqual((usage.input, usage.output, usage.cache_read, usage.cache_write), (11, 22, 33, 44))
        self.assertEqual(usage.total, 110)
        self.assertEqual(usage.reasoning, 5)
        self.assertAlmostEqual(usage.cost, 0.75)

    def test_efficiency_matches_the_organizer_formula(self):
        usage = Usage(input=1000, output=100, cache_read=2000)
        self.assertAlmostEqual(usage.efficiency, 1000 + 300 + 200.0)

    def test_missing_and_malformed_fields_count_as_zero(self):
        usage = Usage()
        usage.add({})
        usage.add({"input": None, "output": "not a number", "cost": None})
        self.assertEqual(usage.calls, 2)
        self.assertEqual(usage.output, 0)
        self.assertEqual(usage.cost, 0.0)

    def test_as_dict_shape(self):
        usage = Usage()
        usage.add({"input": 1, "output": 2, "cacheRead": 3})
        keys = set(usage.as_dict())
        self.assertIn("model_calls", keys)
        self.assertIn("efficiency_points", keys)

    def test_summarize_over_events(self):
        total = summarize(SUCCESS_EVENTS)
        self.assertEqual(total.calls, 2)
        self.assertEqual(total.output, 160)


class ParsingTest(unittest.TestCase):
    def test_parse_events_skips_blank_and_malformed(self):
        text = '{"type":"a"}\n\nnot json\n{"type":"b"}\n'
        self.assertEqual([e["type"] for e in parse_events(text)], ["a", "b"])

    def test_parse_events_skips_non_objects(self):
        self.assertEqual(parse_events('[1,2]\n"text"\n{"type":"c"}\n'), [{"type": "c"}])

    def test_assistant_message_ends_filters_role_and_type(self):
        events = [
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "message_end", "message": {"role": "user"}},
            assistant_end("stop", 1),
        ]
        self.assertEqual(len(assistant_message_ends(events)), 1)

    def test_message_text_handles_both_content_shapes(self):
        self.assertEqual(message_text({"content": "plain"}), "plain")
        self.assertEqual(
            message_text({"content": [{"type": "text", "text": "a"},
                                      {"type": "tool_call"},
                                      {"type": "text", "text": "b"}]}),
            "ab",
        )
        self.assertEqual(message_text({}), "")

    def test_last_stop_reason(self):
        self.assertEqual(last_stop_reason(SUCCESS_EVENTS), "stop")
        self.assertIsNone(last_stop_reason([]))


if __name__ == "__main__":
    unittest.main()
