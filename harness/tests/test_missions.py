"""Mission sessions: labels, parallelism, budget refusal, resume, shutdown.

Every test here drives a real :class:`~harness.missions.MissionRunner` in this
process against ``harness/tests/fake_pi.py`` -- real subprocesses, real threads,
a real ``threading.Event`` -- so what is exercised is exactly the path a judged
run takes, minus the model.

``gate_active`` is ``False`` everywhere except the refusal test: the fake Pi
answers in milliseconds regardless of a mission's predicted output, so gating
those answers against the 30 tok/s assumption would refuse missions the fixture
never intended to be slow (the same reason ``__main__.budget_gate_active()``
turns the gate off whenever ``HARNESS_PI_BIN`` is set).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from harness import pirpc
from harness.budget import BudgetController
from harness.missions import (
    BUILDER_PREDICTED_OUTPUT_TOKENS,
    REPAIRER_PREDICTED_OUTPUT_TOKENS,
    RESUME_PROMPT,
    TESTER_PREDICTED_OUTPUT_TOKENS,
    MissionResult,
    MissionRunner,
    MissionSpec,
)
from harness.pirpc import base_args
from harness.tests import support

#: The two briefs are deliberately disjoint: neither names the other's file, so
#: ``FAKE_PI_WRITE_ON_PROMPT`` proves each session wrote only its own.
BUILDER_BRIEF = (
    "## Mission: write `src/app-config.ts`\n\n"
    '{"app_name": "Home Library", "noun": "book", "fields": [{"name": "title"}]}\n\n'
    "One write of the whole file. Do not read any file. End the turn after the write.\n"
)
TESTER_BRIEF = (
    "## Mission: write `src/journeys.test.tsx`\n\n"
    '{"journeys": ["Add a book", "Lend a book", "Return a book"]}\n\n'
    "One `it` per journey, helpers from `./test/helpers.js`. End the turn after the write.\n"
)
REPAIR_BRIEF = (
    "## Mission: fix the failing check\n\n"
    "src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable.\n"
)


class _ByteSink:
    """A stdout buffer that records what was forwarded, one write at a time.

    ``forward_record`` resolves ``sys.stdout.buffer`` inside the call and holds
    the module-level lock across write + flush, so appending here is safe even
    with two sessions forwarding concurrently (the technique
    ``test_stdout_integrity.py`` uses on a real pipe, in process).
    """

    def __init__(self) -> None:
        self.data = bytearray()
        self.flushes = 0

    def write(self, payload: Any) -> int:
        chunk = bytes(payload)
        self.data += chunk
        return len(chunk)

    def flush(self) -> None:
        self.flushes += 1


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = _ByteSink()


class ToolFlagTest(unittest.TestCase):
    """``base_args(tools=...)``: additive, and in the starter's flag order."""

    def test_tools_sits_between_the_skill_and_the_provider(self):
        argv = base_args(
            append_system="PROMPT",
            session_dir="/runs/1/sessions/1-builder",
            extensions=["/repo/solution/extensions/protected-paths.ts"],
            skill="/repo/solution/skills/mvp-builder",
            tools="read,write,edit",
            provider="berget",
            model="zai-org/GLM-5.2",
        )
        self.assertEqual(argv[argv.index("--tools") + 1], "read,write,edit")
        self.assertLess(argv.index("--skill"), argv.index("--tools"))
        self.assertLess(argv.index("--extension"), argv.index("--tools"))
        self.assertLess(argv.index("--tools"), argv.index("--provider"))
        self.assertEqual(argv[-2:], ["--thinking", "off"])

    def test_no_tools_means_no_flag(self):
        self.assertNotIn("--tools", base_args())
        self.assertNotIn("--tools", base_args(tools=None, provider="berget"))


class MissionResultTest(unittest.TestCase):
    def test_as_dict_is_json_serialisable_and_drops_the_prose(self):
        result = MissionResult(
            role="builder",
            label="1-builder",
            session_dir=pathlib.Path("/runs/1/sessions/1-builder"),
            settled=True,
            success=True,
            output_tokens=1234,
            wall_s=12.3456789,
            text="a long assistant answer",
        )
        body = json.loads(json.dumps(result.as_dict()))
        self.assertEqual(body["session_dir"], "/runs/1/sessions/1-builder")
        self.assertEqual(body["wall_s"], 12.346)
        self.assertEqual(body["output_tokens"], 1234)
        self.assertNotIn("text", body)


class MissionRunnerTestCase(unittest.TestCase):
    """Shared fixture: an app dir, a session root, and the fake Pi's knobs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.app = self.root / "app"
        self.harness_dir = self.root / "harness"
        self.session_root = self.root / "sessions"
        for directory in (self.app, self.harness_dir, self.session_root):
            directory.mkdir(parents=True, exist_ok=True)

        self.seed_config = self.root / "seed-app-config.ts"
        self.seed_config.write_text(
            "export const appConfig = defineApp({ noun: 'book' });\n", encoding="utf-8"
        )
        self.seed_test = self.root / "seed-journeys.test.tsx"
        self.seed_test.write_text(
            "import { renderApp } from './test/helpers.js';\n", encoding="utf-8"
        )

        self.prompt_log = self.root / "prompts.jsonl"
        self.knobs: Dict[str, str] = {}
        self.stop_event = threading.Event()
        self.controller = BudgetController(deadline_monotonic=time.monotonic() + 3600.0)
        self.runners: List[MissionRunner] = []

        # Pi's forwarded lines would otherwise land on this process's own stdout
        # (that is the harness's whole point). Capturing them keeps the suite's
        # output readable and gives one test its assertion material. The patch is
        # lifted only after tearDown has closed every session, so no record can
        # escape to the real stdout afterwards.
        patcher = mock.patch.object(pirpc.sys, "stdout", _FakeStdout())
        self.stdout: _FakeStdout = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for runner in self.runners:
            try:
                runner.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask a failure
                pass
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def make_runner(self, **overrides: Any) -> MissionRunner:
        environment = support.harness_environment(self.knobs)
        environment["PI_OFFLINE"] = "1"
        parameters: Dict[str, Any] = {
            "pi_binary": support.FAKE_PI,
            "app_directory": self.app,
            "harness_directory": self.harness_dir,
            "session_root": self.session_root,
            "append_system": "MISSION SYSTEM PROMPT",
            "extensions": [],
            "provider": None,
            "model": None,
            "thinking": "off",
            "env": environment,
            "stop_event": self.stop_event,
            "controller": self.controller,
            "gate_active": False,
            "on_event": None,
            "deadline": time.monotonic() + 3600.0,
        }
        parameters.update(overrides)
        runner = MissionRunner(**parameters)
        self.runners.append(runner)
        return runner

    def mission_builder(self) -> MissionSpec:
        return MissionSpec("builder", BUILDER_BRIEF, BUILDER_PREDICTED_OUTPUT_TOKENS)

    def mission_tester(self) -> MissionSpec:
        return MissionSpec("tester", TESTER_BRIEF, TESTER_PREDICTED_OUTPUT_TOKENS)

    def mission_repairer(self) -> MissionSpec:
        return MissionSpec("repairer", REPAIR_BRIEF, REPAIRER_PREDICTED_OUTPUT_TOKENS)

    def log_prompts(self) -> None:
        self.knobs["FAKE_PI_PROMPT_LOG"] = str(self.prompt_log)

    def prompt_entries(self) -> List[Dict[str, Any]]:
        if not self.prompt_log.is_file():
            return []
        entries = [
            json.loads(line)
            for line in self.prompt_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return sorted(entries, key=lambda entry: entry["t"])

    def session_names(self) -> List[str]:
        return sorted(path.name for path in self.session_root.iterdir())

    def child_pids(self) -> List[int]:
        pids = []
        for pid_file in sorted(self.session_root.glob("*/fake-pi.pid")):
            pids.append(int(pid_file.read_text(encoding="utf-8").strip()))
        return pids


class PerMissionModeTest(MissionRunnerTestCase):
    def test_each_mission_gets_its_own_numbered_session(self):
        runner = self.make_runner()
        first = runner.run(self.mission_builder())
        second = runner.run(self.mission_tester())
        runner.close()

        self.assertEqual([first.label, second.label], ["1-builder", "2-tester"])
        self.assertEqual(self.session_names(), ["1-builder", "2-tester"])
        for result in (first, second):
            self.assertTrue(result.settled, result.error)
            self.assertTrue(result.success, result.error)
            self.assertFalse(result.interrupted)
            self.assertIsNone(result.skipped_reason)
            self.assertGreater(result.output_tokens, 0)
            session_file = result.session_dir / "session.jsonl"
            self.assertTrue(session_file.is_file(), "no session jsonl in {0}".format(result.label))
            self.assertGreater(session_file.stat().st_size, 0)
        self.assertEqual(runner.sessions(), ["1-builder", "2-tester"])

    def test_run_parallel_sends_both_briefs_with_the_mission_tool_set(self):
        self.log_prompts()
        runner = self.make_runner()
        results = runner.run_parallel([self.mission_builder(), self.mission_tester()], stagger_s=0.05)

        self.assertEqual([result.label for result in results], ["1-builder", "2-tester"])
        self.assertTrue(all(result.settled and result.success for result in results))

        entries = self.prompt_entries()
        self.assertEqual(len(entries), 2, "both missions must have been prompted")
        self.assertEqual(len({entry["pid"] for entry in entries}), 2, "one session each")
        texts = "".join(entry["text"] for entry in entries)
        self.assertIn(BUILDER_BRIEF, texts)
        self.assertIn(TESTER_BRIEF, texts)
        for entry in entries:
            argv = entry["argv"]
            self.assertIn("--tools", argv)
            self.assertEqual(argv[argv.index("--tools") + 1], "read,write,edit")
            # A mission session never carries a skill (PHASE3_DESIGN.md §9).
            self.assertNotIn("--skill", argv)
            self.assertEqual(argv[argv.index("--thinking") + 1], "off")

    def test_a_mission_writes_only_the_file_its_own_brief_names(self):
        self.log_prompts()
        self.knobs["FAKE_PI_WRITE_ON_PROMPT"] = "src/app-config.ts={0};src/journeys.test.tsx={1}".format(
            self.seed_config, self.seed_test
        )
        runner = self.make_runner()
        runner.run_parallel([self.mission_builder(), self.mission_tester()], stagger_s=0.05)

        config = self.app / "src" / "app-config.ts"
        tests = self.app / "src" / "journeys.test.tsx"
        self.assertTrue(config.is_file(), "the Builder mission wrote no config")
        self.assertTrue(tests.is_file(), "the Tester mission wrote no test file")
        self.assertEqual(config.read_text(encoding="utf-8"), self.seed_config.read_text(encoding="utf-8"))
        self.assertEqual(tests.read_text(encoding="utf-8"), self.seed_test.read_text(encoding="utf-8"))

    def test_the_second_mission_starts_a_stagger_later(self):
        self.log_prompts()
        stagger = 1.0
        runner = self.make_runner()
        runner.run_parallel([self.mission_builder(), self.mission_tester()], stagger_s=stagger)

        entries = self.prompt_entries()
        self.assertEqual(len(entries), 2)
        gap = entries[1]["t"] - entries[0]["t"]
        # The gap can only be longer than the stagger (process spawn); a shorter
        # one means the second request could not have found the first's prefix
        # in the provider's cache.
        self.assertGreaterEqual(gap, stagger * 0.9, "the stagger was not respected")

    def test_stop_event_returns_promptly_with_interrupted_results(self):
        self.knobs["FAKE_PI_SETTLE_DELAY"] = "10"
        runner = self.make_runner()
        stopped_at: List[float] = []

        def _stop() -> None:
            stopped_at.append(time.monotonic())
            self.stop_event.set()

        timer = threading.Timer(0.6, _stop)
        timer.start()
        try:
            results = runner.run_parallel([self.mission_builder(), self.mission_tester()], stagger_s=0.05)
        finally:
            timer.cancel()
        finished_at = time.monotonic()

        self.assertEqual(len(stopped_at), 1, "the stop event never fired")
        # Measured 0.98s here, five runs: FAST_CLOSE's 0.5s stdin grace (the fake
        # is mid-prompt and will not exit on EOF) plus one 0.25s exit poll after
        # SIGTERM. The bound that matters is the runner's 5s SIGTERM->SIGKILL
        # grace, which this leaves four fifths of.
        self.assertLess(finished_at - stopped_at[0], 1.5, "shutdown must be prompt")
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertTrue(result.interrupted, result.as_dict())
            self.assertFalse(result.settled)

        pids = self.child_pids()
        self.assertEqual(len(pids), 2, "both sessions must have spawned a child")
        for pid in pids:
            self.assertTrue(
                support.wait_for(lambda: not support.process_alive(pid), timeout=10.0),
                "the fake Pi process must be reaped",
            )

    def test_a_budget_refusal_skips_the_mission_without_spawning_it(self):
        self.log_prompts()
        # 1500 predicted tokens at 30 tok/s is 50s, plus a 50s finish margin,
        # against 5s of clock: refused.
        near = time.monotonic() + 5.0
        controller = BudgetController(deadline_monotonic=near)
        runner = self.make_runner(controller=controller, gate_active=True, deadline=near)
        result = runner.run(self.mission_builder())

        self.assertIsNotNone(result.skipped_reason)
        self.assertIn("exceeds", result.skipped_reason or "")
        self.assertFalse(result.settled)
        self.assertFalse(result.success)
        self.assertEqual(result.label, "1-builder")
        self.assertFalse(result.session_dir.exists(), "a refused mission must not spawn a session")
        self.assertEqual(self.session_names(), [])
        self.assertEqual(self.prompt_entries(), [])
        # The refused number is not burnt: the next mission is still session 1.
        self.assertEqual(runner.peek_label("tester"), "1-tester")

    def test_a_transient_provider_error_is_resumed_once(self):
        self.log_prompts()
        self.knobs["FAKE_PI_ERROR_ONCE"] = "1"
        runner = self.make_runner()
        with mock.patch.dict(os.environ, {"HARNESS_RESUME_BACKOFF_S": "0.1"}):
            result = runner.run(self.mission_builder())

        self.assertEqual(result.resume_attempts, 1)
        self.assertTrue(result.settled, result.error)
        self.assertTrue(result.success, "the resumed turn must count as usable")

        entries = self.prompt_entries()
        self.assertEqual(len(entries), 2, "the resume prompt must reuse the same session")
        self.assertEqual(len({entry["pid"] for entry in entries}), 1)
        self.assertEqual(entries[1]["text"], RESUME_PROMPT)


class ObservabilityTest(MissionRunnerTestCase):
    def test_on_event_sees_every_assistant_turn_from_both_sessions(self):
        seen: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def observer(event: Dict[str, Any]) -> None:
            with lock:
                seen.append(event)

        runner = self.make_runner(on_event=observer)
        runner.run_parallel([self.mission_builder(), self.mission_tester()], stagger_s=0.05)

        message_ends = [
            event
            for event in seen
            if event.get("type") == "message_end"
            and (event.get("message") or {}).get("role") == "assistant"
        ]
        self.assertEqual(len(message_ends), 2, "one assistant turn per mission")
        settled = [event for event in seen if event.get("type") == "agent_settled"]
        self.assertEqual(len(settled), 2)

    def test_every_forwarded_line_from_both_sessions_is_intact_json(self):
        runner = self.make_runner()
        results = runner.run_parallel(
            [self.mission_builder(), self.mission_tester()], stagger_s=0.05
        )
        self.assertTrue(all(result.settled for result in results))

        payload = bytes(self.stdout.buffer.data)
        self.assertTrue(payload.endswith(b"\n"), "every record must be newline terminated")
        lines = payload.split(b"\n")[:-1]
        self.assertGreater(len(lines), 10)
        events = []
        for line in lines:
            # A truncated or interleaved record from the other session's thread
            # would fail here -- that is the whole assertion.
            events.append(json.loads(line.decode("utf-8")))
        kinds = [event.get("type") for event in events]
        self.assertEqual(kinds.count("agent_settled"), 2)
        prompts = [
            event
            for event in events
            if event.get("type") == "response" and event.get("command") == "prompt"
        ]
        self.assertEqual(len(prompts), 2, "both sessions' lines must reach stdout")


class SingleSessionModeTest(MissionRunnerTestCase):
    def test_three_missions_share_one_agent_session(self):
        self.log_prompts()
        runner = self.make_runner(session_mode="single")
        results = [runner.run(self.mission_builder()), runner.run(self.mission_tester()), runner.run(self.mission_repairer())]
        runner.close()

        self.assertEqual([result.label for result in results], ["1-agent"] * 3)
        self.assertEqual(self.session_names(), ["1-agent"])
        for result in results:
            self.assertTrue(result.settled, result.error)
            self.assertTrue(result.success, result.error)

        entries = self.prompt_entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual([entry["n"] for entry in entries], [1, 2, 3])
        self.assertEqual(len({entry["pid"] for entry in entries}), 1, "one Pi process for all three")
        self.assertEqual(
            [BUILDER_BRIEF, TESTER_BRIEF, REPAIR_BRIEF], [entry["text"] for entry in entries]
        )

    def test_run_parallel_is_sequential_in_single_mode(self):
        self.log_prompts()
        runner = self.make_runner(session_mode="single")
        results = runner.run_parallel([self.mission_builder(), self.mission_tester()], stagger_s=0.05)
        runner.close()

        self.assertEqual([result.label for result in results], ["1-agent", "1-agent"])
        self.assertEqual(self.session_names(), ["1-agent"])
        entries = self.prompt_entries()
        self.assertEqual([entry["n"] for entry in entries], [1, 2])


if __name__ == "__main__":
    unittest.main()
