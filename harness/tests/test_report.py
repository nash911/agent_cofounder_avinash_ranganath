"""Unit tests for :mod:`harness.report`: shapes, the green detector, and the
write-report race guard. No Pi and no real vitest here -- ``test_main_wiring.py``
covers the end-to-end path."""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest

from harness import report


class GreenDetectorTest(unittest.TestCase):
    def test_green_summary_is_detected(self):
        text = "\n Test Files  1 passed (1)\n      Tests  3 passed (3)\n"
        self.assertTrue(report.is_green_vitest_summary(text))

    def test_red_summary_is_rejected_even_with_a_passed_count(self):
        text = "Test Files  1 failed (1)\n Tests  1 failed | 2 passed (3)\n"
        self.assertFalse(report.is_green_vitest_summary(text))

    def test_no_tests_summary_is_rejected(self):
        self.assertFalse(report.is_green_vitest_summary("no test files found"))

    def test_empty_text_is_rejected(self):
        self.assertFalse(report.is_green_vitest_summary(""))
        self.assertFalse(report.is_green_vitest_summary(None))  # type: ignore[arg-type]

    def test_extract_bash_result_text_concatenates_text_blocks(self):
        event = {
            "type": "tool_execution_end",
            "toolName": "bash",
            "result": {
                "content": [
                    {"type": "text", "text": "Tests  "},
                    {"type": "image", "data": "ignored"},
                    {"type": "text", "text": "3 passed (3)"},
                ]
            },
        }
        self.assertTrue(
            report.is_green_vitest_summary(report._extract_bash_result_text(event))
        )


class ComposeReportTest(unittest.TestCase):
    def _observation(self, **overrides):
        base = {"green": True, "total": 2, "passed": 2, "failed": 0, "names": ["a > b", "a > c"]}
        base.update(overrides)
        return base

    def test_status_is_always_partial(self):
        payload = report.compose_report(None, self._observation(), "An idea.")
        self.assertEqual(payload["status"], "partial")

    def test_without_a_spec_summary_is_the_ideas_first_sentence(self):
        idea = "Track library loans for a small branch. It should also send reminders."
        payload = report.compose_report(None, self._observation(), idea)
        self.assertEqual(payload["summary"], "Track library loans for a small branch.")
        self.assertEqual(payload["implemented_features"], [])
        self.assertEqual(payload["assumptions"], [])

    def test_with_a_spec_summary_comes_from_the_spec(self):
        spec = {"summary": "A library loan tracker.", "app_name": "Loanly"}
        payload = report.compose_report(spec, self._observation(), "irrelevant idea text")
        self.assertEqual(payload["summary"], "A library loan tracker.")

    def test_spec_without_a_summary_falls_back_to_the_tagline_then_the_idea(self):
        idea = "Some idea. More text."
        spec_with_tagline = {"tagline": "A tagline."}
        payload = report.compose_report(spec_with_tagline, self._observation(), idea)
        self.assertEqual(payload["summary"], "A tagline.")

        spec_empty = {}
        payload2 = report.compose_report(spec_empty, self._observation(), idea)
        self.assertEqual(payload2["summary"], "Some idea.")

    def test_features_and_assumptions_pulled_from_spec_independently(self):
        spec = {"summary": "x", "implemented_features": ["f1"], "assumptions": ["a1"]}
        payload = report.compose_report(spec, self._observation(), "idea")
        self.assertEqual(payload["implemented_features"], ["f1"])
        self.assertEqual(payload["assumptions"], ["a1"])

    def test_tests_run_has_one_entry_per_passed_test_name(self):
        payload = report.compose_report(None, self._observation(names=["j1", "j2", "j3"]), "idea")
        self.assertEqual(len(payload["tests_run"]), 3)
        for entry, name in zip(payload["tests_run"], ["j1", "j2", "j3"]):
            self.assertEqual(entry, {"command": "npm test", "journey": name, "result": "passed"})

    def test_no_names_means_no_tests_run_entries(self):
        payload = report.compose_report(None, self._observation(names=[]), "idea")
        self.assertEqual(payload["tests_run"], [])

    def test_first_sentence_falls_back_to_a_truncated_prefix_with_no_punctuation(self):
        idea = "x" * 400  # no sentence-ending punctuation anywhere
        payload = report.compose_report(None, self._observation(), idea)
        self.assertLessEqual(len(payload["summary"]), 240)

    def test_empty_idea_yields_empty_summary_without_a_spec(self):
        payload = report.compose_report(None, self._observation(), "   ")
        self.assertEqual(payload["summary"], "")


class WriteReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_when_nothing_existed_before(self):
        observation = {"green": True, "total": 1, "passed": 1, "failed": 0, "names": ["j"]}
        ok = report.write_report(self.app_dir, None, observation, "An idea.")
        self.assertTrue(ok)
        payload = json.loads((self.app_dir / "report.partial.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["tests_run"][0]["journey"], "j")

    def test_skips_the_write_when_the_file_changed_since_the_captured_mtime(self):
        report_path = self.app_dir / "report.partial.json"
        report_path.write_text('{"status": "model-wrote-this"}\n', encoding="utf-8")
        stale_expected = report._mtime_or_none(report_path) - 1  # any value that will not match
        observation = {"green": True, "total": 1, "passed": 1, "failed": 0, "names": []}
        ok = report.write_report(
            self.app_dir, None, observation, "idea", expected_mtime=stale_expected
        )
        self.assertFalse(ok)
        # The model's content must survive untouched.
        self.assertEqual(
            json.loads(report_path.read_text(encoding="utf-8")), {"status": "model-wrote-this"}
        )

    def test_writes_when_expected_mtime_matches_current(self):
        # The pre-existing report must be the harness's own: a report the
        # harness did not write is the model's and is never replaced.
        report_path = self.app_dir / "report.partial.json"
        first = {"green": True, "total": 1, "passed": 1, "failed": 0, "names": ["old"]}
        self.assertTrue(report.write_report(self.app_dir, None, first, "idea"))
        before = report._mtime_or_none(report_path)
        observation = {"green": True, "total": 2, "passed": 2, "failed": 0, "names": ["old", "new"]}
        ok = report.write_report(self.app_dir, None, observation, "idea", expected_mtime=before)
        self.assertTrue(ok)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(len(payload["tests_run"]), 2)

    def test_default_expected_mtime_captures_now_and_still_writes(self):
        observation = {"green": False, "total": 0, "passed": 0, "failed": 0, "names": []}
        ok = report.write_report(self.app_dir, None, observation, "idea")
        self.assertTrue(ok)

    def test_a_write_failure_returns_false_and_does_not_raise(self):
        # Point the "app dir" at a file, not a directory, so the write can never succeed.
        not_a_dir = self.app_dir / "not-a-dir"
        not_a_dir.write_text("x", encoding="utf-8")
        observation = {"green": True, "total": 1, "passed": 1, "failed": 0, "names": []}
        ok = report.write_report(not_a_dir, None, observation, "idea")
        self.assertFalse(ok)


class ReportWatcherUnitTest(unittest.TestCase):
    """Exercises the watcher's event filtering and single-flight throttle without
    ever actually invoking vitest (``observe`` is monkeypatched out)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app_dir = pathlib.Path(self._tmp.name) / "app"
        self.harness_dir = pathlib.Path(self._tmp.name) / "harness"
        self.app_dir.mkdir()
        self.harness_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _green_event(self):
        return {
            "type": "tool_execution_end",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "Tests  1 passed (1)\n"}]},
        }

    def test_non_bash_or_non_green_events_are_ignored(self):
        watcher = report.ReportWatcher(self.app_dir, self.harness_dir, "idea", min_interval_s=60.0)
        watcher.on_event({"type": "message_end"})
        watcher.on_event({"type": "tool_execution_end", "toolName": "read", "result": {}})
        watcher.on_event(
            {"type": "tool_execution_end", "toolName": "bash", "result": {"content": []}}
        )
        watcher.join(2.0)
        self.assertEqual(watcher.observations, [])

    def test_a_green_bash_event_starts_exactly_one_observation(self):
        calls = []

        def fake_observe(app_dir, harness_dir, timeout_s=60.0):
            calls.append(1)
            return {"green": True, "total": 1, "passed": 1, "failed": 0, "names": ["j"]}

        watcher = report.ReportWatcher(self.app_dir, self.harness_dir, "idea", min_interval_s=60.0)
        watcher._run_observe = lambda: report.write_report(
            watcher.app_dir, watcher.spec, fake_observe(None, None), watcher.idea_text
        )
        watcher.on_event(self._green_event())
        watcher.join(2.0)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.app_dir / "report.partial.json").is_file())

    def test_single_flight_throttle_blocks_a_second_trigger_within_the_window(self):
        started = []

        def slow_run_observe():
            started.append(time.monotonic())
            time.sleep(0.2)

        watcher = report.ReportWatcher(self.app_dir, self.harness_dir, "idea", min_interval_s=60.0)
        watcher._run_observe = slow_run_observe
        watcher.on_event(self._green_event())
        watcher.on_event(self._green_event())  # thread still alive: must be dropped
        watcher.join(2.0)
        self.assertEqual(len(started), 1)

    def test_throttle_window_blocks_a_second_start_even_after_the_first_finishes(self):
        started = []
        watcher = report.ReportWatcher(self.app_dir, self.harness_dir, "idea", min_interval_s=60.0)
        watcher._run_observe = lambda: started.append(time.monotonic())
        watcher.on_event(self._green_event())
        watcher.join(2.0)
        watcher.on_event(self._green_event())  # inside the 60s window: dropped
        watcher.join(2.0)
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()


class AuthorshipGuardTest(unittest.TestCase):
    """The model's report is authoritative; the harness only ever replaces its own."""

    GREEN = {"green": True, "total": 1, "passed": 1, "failed": 0, "names": ["adds a record"]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.app_dir = root / "app"
        self.harness_dir = root / "harness"
        self.app_dir.mkdir()
        self.harness_dir.mkdir()
        self.report_path = self.app_dir / "report.partial.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_model_authored_report_is_never_overwritten(self):
        self.report_path.write_text('{"status": "success", "summary": "the model wrote this"}\n', encoding="utf-8")
        ok = report.write_report(self.app_dir, None, self.GREEN, "idea", harness_dir=self.harness_dir)
        self.assertFalse(ok)
        self.assertEqual(json.loads(self.report_path.read_text(encoding="utf-8"))["summary"], "the model wrote this")

    def test_harness_authored_report_is_refreshed(self):
        self.assertTrue(report.write_report(self.app_dir, None, self.GREEN, "idea", harness_dir=self.harness_dir))
        self.assertTrue((self.harness_dir / "report.harness.sha256").is_file())
        bigger = dict(self.GREEN, total=2, passed=2, names=["adds a record", "edits a record"])
        self.assertTrue(report.write_report(self.app_dir, None, bigger, "idea", harness_dir=self.harness_dir))
        self.assertEqual(len(json.loads(self.report_path.read_text(encoding="utf-8"))["tests_run"]), 2)

    def test_a_model_write_after_the_harness_write_wins(self):
        self.assertTrue(report.write_report(self.app_dir, None, self.GREEN, "idea", harness_dir=self.harness_dir))
        self.report_path.write_text('{"status": "success", "summary": "model, later"}\n', encoding="utf-8")
        ok = report.write_report(self.app_dir, None, self.GREEN, "idea", harness_dir=self.harness_dir)
        self.assertFalse(ok)
        self.assertEqual(json.loads(self.report_path.read_text(encoding="utf-8"))["summary"], "model, later")

    def test_sidecar_alone_identifies_a_harness_report_across_processes(self):
        self.assertTrue(report.write_report(self.app_dir, None, self.GREEN, "idea", harness_dir=self.harness_dir))
        report._LAST_WRITTEN.clear()  # simulate a fresh process
        self.assertTrue(report.harness_authored(self.report_path, self.harness_dir))
        self.report_path.write_text("{}\n", encoding="utf-8")
        self.assertFalse(report.harness_authored(self.report_path, self.harness_dir))


class RepairTestsRunTest(unittest.TestCase):
    """A model report with malformed tests_run gets that one field repaired."""

    GREEN = {"green": True, "total": 2, "passed": 2, "failed": 0, "names": ["adds a record", "edits a record"]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.app_dir = root / "app"
        self.harness_dir = root / "harness"
        self.app_dir.mkdir()
        self.harness_dir.mkdir()
        self.report_path = self.app_dir / "report.partial.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload):
        self.report_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_valid_tests_run_keeps_only_runner_shaped_entries(self):
        entries = report.valid_tests_run([
            {"command": "npm test", "journey": "ok", "result": "passed"},
            {"name": "slip", "status": "passed"},
            {"command": "npm test", "journey": "bad", "result": "pass"},
            "junk",
        ])
        self.assertEqual(entries, [{"command": "npm test", "journey": "ok", "result": "passed"}])

    def test_malformed_entries_are_replaced_and_prose_is_kept(self):
        self._write({"status": "success", "summary": "the model's summary", "implemented_features": ["a"],
                     "assumptions": ["b"], "tests_run": [{"name": "adds a record", "status": "passed"}]})
        self.assertTrue(report.repair_tests_run(self.app_dir, self.harness_dir, self.GREEN))
        payload = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"], "the model's summary")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["implemented_features"], ["a"])
        self.assertEqual([e["journey"] for e in payload["tests_run"]], ["adds a record", "edits a record"])
        self.assertTrue(all(e["command"] == "npm test" and e["result"] == "passed" for e in payload["tests_run"]))

    def test_valid_entries_are_never_touched(self):
        original = {"status": "success", "summary": "s", "implemented_features": [], "assumptions": [],
                    "tests_run": [{"command": "npm test", "journey": "kept", "result": "passed"}]}
        self._write(original)
        before = self.report_path.read_bytes()
        self.assertFalse(report.repair_tests_run(self.app_dir, self.harness_dir, self.GREEN))
        self.assertEqual(self.report_path.read_bytes(), before)

    def test_red_or_empty_observation_does_not_repair(self):
        self._write({"status": "partial", "summary": "s", "tests_run": []})
        self.assertFalse(report.repair_tests_run(self.app_dir, self.harness_dir, dict(self.GREEN, green=False)))
        self.assertFalse(report.repair_tests_run(self.app_dir, self.harness_dir, dict(self.GREEN, names=[])))
        self.assertEqual(json.loads(self.report_path.read_text(encoding="utf-8"))["tests_run"], [])
