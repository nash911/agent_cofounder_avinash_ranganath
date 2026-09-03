"""Auto-compaction must not be mistaken for the end of the run.

Pi emits ``agent_end`` from inside ``agent.prompt()`` and only then checks for
auto-compaction: ``compaction_start``, a silent summarization LLM call, the
continued build, and ``agent_settled`` last. A harness that treated a short
silence after ``agent_end`` as "settled" killed Pi mid-compaction, threw away
everything the agent would still have built, and reported the half-built app as
a success.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from harness.tests import support

COMPACTION_SILENCE_S = 8


class CompactionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.run_dir = pathlib.Path(self._tmp.name) / "run"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_silent_compaction_does_not_end_the_prompt(self):
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=120_000,
            env_extra={"FAKE_PI_COMPACT": str(COMPACTION_SILENCE_S)},
            wait_s=120.0,
        )
        self.assertEqual(code, 0, stderr)

        events = [json.loads(line.decode("utf-8")) for line in stdout.split(b"\n") if line]
        kinds = [event.get("type") for event in events]
        self.assertIn("compaction_start", kinds)
        self.assertIn("compaction_end", kinds, "the harness stopped inside the compaction")
        self.assertIn("agent_settled", kinds)
        self.assertLess(kinds.index("compaction_end"), kinds.index("agent_settled"))

        outputs = [
            event["message"]["usage"]["output"]
            for event in events
            if event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "assistant"
        ]
        self.assertEqual(len(outputs), 2, "the post-compaction turn was lost")
        self.assertIn(400, outputs)

        # settled must come from agent_settled, never from an agent_end shortcut.
        self.assertIn("settled=True", stderr)
        self.assertNotIn("abort was not acknowledged", stderr)


if __name__ == "__main__":
    unittest.main()
