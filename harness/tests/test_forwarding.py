"""Stdout forwarding fidelity: every Pi record, byte for byte, in order."""

from __future__ import annotations

import json
import tempfile
import unittest
import pathlib

from harness.tests import support
from harness.usage import run_succeeded


class ForwardingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.run_dir = pathlib.Path(self._tmp.name) / "run"

    def tearDown(self):
        self._tmp.cleanup()

    def test_stdout_is_byte_identical_to_what_pi_wrote(self):
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=60_000,
            env_extra={"FAKE_PI_GARBAGE": "1", "FAKE_PI_LINES": "3"},
        )
        self.assertEqual(code, 0, stderr)

        emitted = (support.session_dir(self.run_dir) / "emitted.jsonl").read_bytes()
        self.assertGreater(len(emitted), 300 * 1024, "the 100 KB stress lines should dominate")
        self.assertEqual(stdout, emitted, "harness stdout must be byte-identical to Pi's stdout")

    def test_malformed_line_is_forwarded_verbatim_and_not_parsed(self):
        _, stdout, stderr = support.run_harness(
            self.run_dir, timeout_ms=60_000, env_extra={"FAKE_PI_GARBAGE": "1"}
        )
        lines = stdout.split(b"\n")
        self.assertEqual(lines[-1], b"", "every record must be newline terminated")
        records = lines[:-1]
        garbage = [line for line in records if line == b"this line is not json {{{"]
        self.assertEqual(len(garbage), 1, "the non-JSON line must survive unchanged")

        parsed = []
        for line in records:
            if line == b"this line is not json {{{":
                continue
            parsed.append(json.loads(line.decode("utf-8")))
        self.assertTrue(run_succeeded(parsed))
        self.assertIn("malformed=1", stderr)

    def test_large_records_are_not_split(self):
        _, stdout, stderr = support.run_harness(
            self.run_dir, timeout_ms=60_000, env_extra={"FAKE_PI_LINES": "5"}
        )
        updates = []
        for line in stdout.split(b"\n"):
            if not line:
                continue
            event = json.loads(line.decode("utf-8"))
            if event.get("type") == "message_update":
                updates.append(event)
        self.assertEqual(len(updates), 5, stderr)
        self.assertEqual([u["index"] for u in updates], [0, 1, 2, 3, 4])
        for update in updates:
            self.assertEqual(len(update["delta"]["text"]), 100 * 1024)

    def test_event_order_is_preserved(self):
        _, stdout, _ = support.run_harness(self.run_dir, timeout_ms=60_000)
        kinds = []
        for line in stdout.split(b"\n"):
            if not line:
                continue
            kinds.append(json.loads(line.decode("utf-8")).get("type"))
        self.assertEqual(kinds[0], "response")  # set_auto_retry
        self.assertEqual(kinds[-1], "agent_settled")
        self.assertLess(kinds.index("agent_start"), kinds.index("message_end"))
        self.assertLess(kinds.index("message_end"), kinds.index("agent_end"))

    def test_no_harness_chatter_on_stdout(self):
        _, stdout, stderr = support.run_harness(self.run_dir, timeout_ms=60_000)
        for line in stdout.split(b"\n"):
            if not line:
                continue
            json.loads(line.decode("utf-8"))
        self.assertIn("harness · ", stderr)

    def test_session_jsonl_and_harness_files_land_in_the_right_places(self):
        support.run_harness(self.run_dir, timeout_ms=60_000)
        session_file = support.session_dir(self.run_dir) / "session.jsonl"
        self.assertTrue(session_file.is_file())
        self.assertGreater(session_file.stat().st_size, 0)

        harness_dir = self.run_dir / "harness"
        self.assertTrue((harness_dir / "harness.log").is_file())
        self.assertTrue((harness_dir / "1-builder.stderr.log").is_file())

        forbidden = {
            "app-test-results.json",
            "app-test.log",
            "app-build.log",
            "app-dev.log",
            "app-verification-error.log",
            "events.jsonl",
        }
        written = {p.name for p in harness_dir.iterdir()}
        self.assertEqual(written & forbidden, set())


if __name__ == "__main__":
    unittest.main()
