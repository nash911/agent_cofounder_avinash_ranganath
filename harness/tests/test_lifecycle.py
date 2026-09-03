"""Timeout, abort, signal handling and exit codes -- with no model involved."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from harness.tests import support


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.run_dir = pathlib.Path(self._tmp.name) / "run"

    def tearDown(self):
        self._tmp.cleanup()

    # -- exit codes --------------------------------------------------------

    def test_successful_session_exits_zero(self):
        code, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000)
        self.assertEqual(code, 0, stderr)
        self.assertIn("exit 0", stderr)

    def test_provider_error_exits_one(self):
        code, stdout, stderr = support.run_harness(
            self.run_dir, timeout_ms=60_000, env_extra={"FAKE_PI_ERROR": "1"}
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn("fake provider error", stderr)
        reasons = [
            json.loads(line.decode("utf-8")).get("message", {}).get("stopReason")
            for line in stdout.split(b"\n")
            if line and b'"message_end"' in line
        ]
        self.assertIn("error", reasons)

    # -- transient provider errors -----------------------------------------

    @staticmethod
    def _prompt_responses(stdout: bytes) -> int:
        count = 0
        for line in stdout.split(b"\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if event.get("type") == "response" and event.get("command") == "prompt":
                count += 1
        return count

    def test_resumes_once_after_a_transient_provider_error(self):
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=120_000,
            env_extra={"FAKE_PI_ERROR_ONCE": "1", "HARNESS_RESUME_BACKOFF_S": "0.1"},
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("resuming in", stderr)
        self.assertIn("resumed after transient provider errors 1 time", stderr)
        self.assertEqual(self._prompt_responses(stdout), 2, stderr)
        self.assertIn("model_calls=2", stderr)

    def test_resume_attempts_are_bounded(self):
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=120_000,
            env_extra={
                "FAKE_PI_ERROR": "1",
                "HARNESS_RESUME_BACKOFF_S": "0.1",
                "HARNESS_RESUME_ATTEMPTS": "2",
            },
        )
        self.assertEqual(code, 1, stderr)
        self.assertEqual(self._prompt_responses(stdout), 3, stderr)

    def test_resume_is_skipped_without_budget(self):
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=120_000,
            env_extra={"FAKE_PI_ERROR_ONCE": "1", "HARNESS_RESUME_MIN_BUDGET_S": "100000"},
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn("not resuming", stderr)
        self.assertEqual(self._prompt_responses(stdout), 1, stderr)

    def test_pi_auto_retry_defaults_on_and_can_be_disabled(self):
        _, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000)
        self.assertIn("pi auto-retry on", stderr)
        other = pathlib.Path(self._tmp.name) / "run-off"
        _, _, stderr = support.run_harness(
            other, timeout_ms=60_000, env_extra={"HARNESS_PI_AUTO_RETRY": "0"}
        )
        self.assertIn("pi auto-retry off", stderr)

    def test_missing_idea_file_is_a_configuration_error(self):
        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=60_000,
            idea_file=pathlib.Path(self._tmp.name) / "no-such-idea.txt",
        )
        self.assertEqual(code, 2, stderr)

    def test_empty_idea_file_is_a_configuration_error(self):
        empty = pathlib.Path(self._tmp.name) / "empty.txt"
        empty.write_text("   \n", encoding="utf-8")
        code, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000, idea_file=empty)
        self.assertEqual(code, 2, stderr)

    def test_help_exits_zero(self):
        completed = subprocess.run(
            [sys.executable, "-m", "harness", "--help"],
            cwd=str(support.REPO_ROOT),
            env=support.harness_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        for flag in ("--idea-file", "--session-root", "--cwd", "--timeout-ms",
                     "--repo-root", "--thinking", "--provider", "--model"):
            self.assertIn(flag, completed.stdout.decode())

    # -- timeout / abort ---------------------------------------------------

    def test_timeout_aborts_and_settles_within_grace(self):
        started = time.monotonic()
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=1_000,
            env_extra={"FAKE_PI_SLOW": "60"},
            wait_s=60.0,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1, stderr)
        self.assertLess(elapsed, 30.0, "abort + close must be quick")

        kinds = [
            json.loads(line.decode("utf-8")).get("type")
            for line in stdout.split(b"\n")
            if line
        ]
        self.assertIn("agent_settled", kinds)
        commands = [
            json.loads(line.decode("utf-8")).get("command")
            for line in stdout.split(b"\n")
            if line and b'"response"' in line
        ]
        self.assertIn("abort", commands)
        self.assertNotIn("abort was not acknowledged", stderr)

    def test_hang_falls_through_abort_to_sigkill_and_reaps_the_child(self):
        started = time.monotonic()
        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=1_000,
            env_extra={"FAKE_PI_HANG": "1"},
            wait_s=90.0,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1, stderr)
        self.assertLess(elapsed, 45.0)
        self.assertIn("SIGKILL", stderr)

        pid_file = support.session_dir(self.run_dir) / "fake-pi.pid"
        self.assertTrue(pid_file.is_file())
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        self.assertTrue(
            support.wait_for(lambda: not support.process_alive(pid), timeout=10.0),
            "the fake Pi process must be reaped",
        )

    # -- signals -----------------------------------------------------------

    @unittest.skipIf(os.name != "posix", "signal delivery is POSIX only")
    def test_sigterm_mid_prompt_exits_within_five_seconds(self):
        process = support.spawn_harness(
            self.run_dir, timeout_ms=600_000, env_extra={"FAKE_PI_HANG": "1"}
        )
        pid_file = support.session_dir(self.run_dir) / "fake-pi.pid"
        events = self.run_dir / "events.jsonl"
        try:
            self.assertTrue(
                support.wait_for(
                    lambda: pid_file.is_file() and events.is_file() and events.stat().st_size > 0,
                    timeout=30.0,
                ),
                "the fake Pi never started",
            )
            # Wait until the prompt has actually been accepted.
            support.wait_for(
                lambda: events.read_bytes().count(b'"command": "prompt"') > 0, timeout=30.0
            )
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())

            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=15)
            elapsed = time.monotonic() - started
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

        self.assertLess(elapsed, 5.0, "SIGTERM must be honoured inside the runner's grace")
        self.assertEqual(code, 1)
        self.assertTrue(
            support.wait_for(lambda: not support.process_alive(child_pid), timeout=10.0),
            "the fake Pi process must be reaped on the signal path",
        )
        stderr = (self.run_dir / "harness.stderr.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("SIGTERM", stderr)


if __name__ == "__main__":
    unittest.main()
