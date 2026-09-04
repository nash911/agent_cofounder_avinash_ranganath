"""Regressions for the review findings the fixer applied (Phase 3 review).

Each test names the failure it prevents, because every one of them was
*measured* on a probe run rather than imagined:

- a ``tsc`` that never finished rewrote a green, ten-journey report as
  ``status: failed`` with ``tests_run: []`` (harness/loop.py, harness/observe.py);
- a SIGTERM landing inside an observation took 11 s to shut the harness down
  against the runner's 5 s grace (harness/proc.py);
- a ``vite`` that could not spawn made the policy ask for a build twelve times
  (harness/supervisor.py);
- two parallel missions reported the run generating at ~1.8x its real speed,
  which let the budget admit a mission it could not finish (harness/__main__.py);
- and five brief/prompt defects that each turn into a red test with an
  invisible cause (harness/plan.py, harness/analyst.py).

Every external command is a small Python stub reached through the modules' own
``HARNESS_*_BIN`` overrides: no ``node_modules``, no network, no model.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Sequence
from unittest import mock

from harness import analyst, loop, missions as missions_mod, observe as observe_mod
from harness import plan as plan_mod, proc, report as report_mod
from harness.__main__ import _UsageObserver
from harness.budget import BudgetController
from harness.observe import Observation
from harness.supervisor import Supervisor
from harness.tests import support

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def books_spec() -> Dict[str, Any]:
    """The probe's real Analyst output, normalised."""
    return analyst.normalize_spec(
        json.loads((FIXTURES / "spec-books.json").read_text(encoding="utf-8"))
    )


def _executable(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _tsc_stub(path: pathlib.Path, *, sleep_s: float = 0.0, exit_code: int = 0) -> pathlib.Path:
    return _executable(
        path,
        "import sys, time\ntime.sleep({0})\nsys.exit({1})\n".format(float(sleep_s), int(exit_code)),
    )


def _vitest_stub(path: pathlib.Path, *, passed: Sequence[str] = ()) -> pathlib.Path:
    data = {
        "numTotalTests": len(passed),
        "numPassedTests": len(passed),
        "numFailedTests": 0,
        "testResults": [{"assertionResults": [{"fullName": n, "status": "passed"} for n in passed]}],
    }
    return _executable(
        path,
        (
            "import json, sys\n"
            "out = None\n"
            "for arg in sys.argv[1:]:\n"
            "    if arg.startswith('--outputFile='):\n"
            "        out = arg.split('=', 1)[1]\n"
            "if out:\n"
            "    open(out, 'w', encoding='utf-8').write({payload!r})\n"
            "sys.exit(0)\n"
        ).format(payload=json.dumps(data)),
    )


def observation(**overrides: Any) -> Observation:
    """A green, built observation unless a field says otherwise."""
    fields: Dict[str, Any] = {
        "tsc_ran": True,
        "tsc_ok": True,
        "tsc_errors": [],
        "vitest": {"green": True, "total": 2, "passed": 2, "failed": 0,
                   "names": ["journeys > adds a book"], "failures": []},
        "build_ran": True,
        "build_ok": True,
        "files": {
            observe_mod.CONFIG_FILE: {"exists": True, "lines": 60, "changed_from_seed": True},
            observe_mod.TESTS_FILE: {"exists": True, "lines": 90, "changed_from_seed": True},
        },
        "over_limit": [],
        "coverage": {"missing": [], "matched": 2, "total": 2},
        "signature": "sig",
        "green": True,
    }
    fields.update(overrides)
    return Observation(**fields)


class FinalStatusTest(unittest.TestCase):
    """``loop.final_status``: what the harness is willing to claim."""

    def test_a_typecheck_that_never_ran_is_not_a_failed_one(self):
        # observe.py's contract: a spawn failure or a timeout is *no*
        # typecheck. Reporting `failed` there published a green, production
        # built app as broken.
        status = loop.final_status(
            observation(tsc_ran=False, tsc_ok=False, green=False,
                        tsc_errors=["tsc did not finish within 60s"])
        )
        self.assertEqual(status, "partial")

    def test_a_red_typecheck_is_still_failed(self):
        self.assertEqual(
            loop.final_status(observation(tsc_ok=False, green=False, tsc_errors=["e"])), "failed"
        )

    def test_a_build_that_never_ran_is_partial_not_failed(self):
        self.assertEqual(
            loop.final_status(observation(build_ran=False, build_ok=None)), "partial"
        )

    def test_a_failed_build_is_failed(self):
        self.assertEqual(loop.final_status(observation(build_ok=False)), "failed")

    def test_untested_journeys_keep_a_green_run_partial(self):
        # PHASE3_DESIGN §7.5: `partial` covers "coverage incomplete". A report
        # that says `success` claims journeys nothing ever ran.
        status = loop.final_status(
            observation(coverage={"missing": ["lends a book"], "matched": 7, "total": 8})
        )
        self.assertEqual(status, "partial")
        self.assertEqual(loop.final_status(observation()), "success")

    def test_evidence_is_what_makes_an_observation_reportable(self):
        self.assertTrue(loop.observation_has_evidence(observation()))
        self.assertFalse(loop.observation_has_evidence(None))
        self.assertFalse(
            loop.observation_has_evidence(
                observation(
                    tsc_ran=False, tsc_ok=False, green=False, build_ran=False, build_ok=None,
                    vitest=report_mod.empty_observation(),
                )
            )
        )


class SuperviseKeepsTheLastRealObservationTest(unittest.TestCase):
    """The final report is composed from the last round that judged the app."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.app = self.root / "app"
        (self.app / "src").mkdir(parents=True)
        self.harness_dir = self.root / "harness"
        self.harness_dir.mkdir()

    def _context(self) -> loop.RunContext:
        return loop.RunContext(
            args=mock.Mock(provider=None, model=None),
            idea="an idea",
            repository_root=support.REPO_ROOT,
            app_directory=self.app,
            session_root=self.root / "sessions",
            harness_directory=self.harness_dir,
            pi_binary=support.FAKE_PI,
            extensions=[],
            child_env={},
            append_system="",
            thinking="off",
            deadline=time.monotonic() + 600.0,
            stop_event=threading.Event(),
            signalled=[],
            controller=BudgetController(time.monotonic() + 600.0),
            gate_active=False,
        )

    def test_a_round_that_judged_nothing_does_not_replace_the_one_that_did(self):
        spec = books_spec()
        plan = plan_mod.derive_plan(spec)
        # Round 1: green, build not tried -> "build". Round 2: the tool-chain
        # broke, so nothing ran -> "stop". The report must come from round 1.
        judged = observation(
            build_ran=False,
            build_ok=None,
            coverage={"missing": [], "matched": 10, "total": 10},
        )
        blind = observation(
            tsc_ran=False, tsc_ok=False, green=False, build_ran=False, build_ok=None,
            tsc_errors=["could not run tsc: No such file"],
            vitest=report_mod.empty_observation(), signature="blind",
        )
        context = self._context()
        supervisor = Supervisor(
            spec=spec, plan=plan, controller=context.controller, gate_active=False,
            harness_dir=self.harness_dir, app_dir=self.app, stop_event=context.stop_event,
        )
        with mock.patch.object(loop, "observe_app", side_effect=[judged, blind]):
            kept = loop._supervise(context, mock.Mock(), supervisor, spec, plan)
        self.assertIs(kept, judged)
        self.assertEqual(loop.final_status(kept), "partial")


class RunBoundedTest(unittest.TestCase):
    """:mod:`harness.proc`: bounded, stop-aware, and never raising."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def test_a_finished_child_reports_its_output_and_code(self):
        binary = _executable(
            self.root / "ok.py", "import sys\nsys.stdout.write('hello\\n')\nsys.exit(3)\n"
        )
        completed = proc.run_bounded([str(binary)], cwd=str(self.root), timeout_s=10.0)
        self.assertTrue(completed.ok)
        self.assertEqual(completed.returncode, 3)
        self.assertIn("hello", completed.output)

    def test_a_slow_child_times_out_and_is_reaped(self):
        binary = _tsc_stub(self.root / "slow.py", sleep_s=30.0)
        started = time.monotonic()
        completed = proc.run_bounded([str(binary)], cwd=str(self.root), timeout_s=0.5)
        self.assertEqual(completed.status, proc.TIMEOUT)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_stop_event_ends_the_wait_long_before_the_timeout(self):
        binary = _tsc_stub(self.root / "slow.py", sleep_s=30.0)
        stop_event = threading.Event()
        threading.Timer(0.2, stop_event.set).start()
        started = time.monotonic()
        completed = proc.run_bounded(
            [str(binary)], cwd=str(self.root), timeout_s=30.0, stop_event=stop_event
        )
        self.assertEqual(completed.status, proc.INTERRUPTED)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_a_missing_binary_is_an_error_not_an_exception(self):
        completed = proc.run_bounded(
            [str(self.root / "absent")], cwd=str(self.root), timeout_s=5.0
        )
        self.assertEqual(completed.status, proc.ERROR)
        self.assertTrue(completed.error)

    def test_no_budget_left_never_spawns(self):
        self.assertEqual(
            proc.run_bounded(["/bin/true"], cwd=str(self.root), timeout_s=0.0).status,
            proc.TIMEOUT,
        )


class ObserveTest(unittest.TestCase):
    """The two observation defects: the vitest fast path and interruptibility."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.app = self.root / "app"
        (self.app / "src").mkdir(parents=True)
        self.harness_dir = self.root / "harness"
        self.harness_dir.mkdir()
        self.tsc = self.root / "tsc.py"
        self.vitest = _vitest_stub(self.root / "vitest.py", passed=["journeys > adds a book"])

    def _observe(self, **kwargs: Any) -> Observation:
        env = {"HARNESS_TSC_BIN": str(self.tsc), "HARNESS_VITEST_BIN": str(self.vitest)}
        with mock.patch.dict("os.environ", env):
            return observe_mod.observe(
                self.app, self.harness_dir, seed_dir=None, **kwargs
            )

    def test_a_typecheck_that_never_ran_still_runs_the_tests(self):
        # The measured failure: `tsc` timed out, the fast path keyed off
        # `tsc_ok` skipped vitest, and ten passing journeys vanished from the
        # report.
        self.tsc = self.root / "absent"
        observation_ = self._observe(timeout_s=30.0)
        self.assertFalse(observation_.tsc_ran)
        self.assertTrue(observation_.vitest["green"])
        self.assertEqual(observation_.vitest["passed"], 1)
        self.assertFalse(observation_.green, "no typecheck is never green")

    def test_a_red_typecheck_still_skips_the_tests(self):
        _tsc_stub(self.tsc, exit_code=2)
        observation_ = self._observe(timeout_s=30.0)
        self.assertTrue(observation_.tsc_ran)
        self.assertEqual(observation_.vitest, report_mod.empty_observation())

    def test_the_skip_decision_itself(self):
        self.assertTrue(observe_mod._skip_vitest(True, False, True))
        self.assertFalse(observe_mod._skip_vitest(False, False, True))
        self.assertFalse(observe_mod._skip_vitest(True, True, True))
        self.assertFalse(observe_mod._skip_vitest(True, False, False))

    def test_a_stop_event_ends_an_observation_inside_the_shutdown_grace(self):
        # The runner escalates SIGTERM to SIGKILL after 5 s; an observation
        # that is not stop-aware held it for 11 s and lost every artifact.
        _tsc_stub(self.tsc, sleep_s=30.0)
        stop_event = threading.Event()
        threading.Timer(0.3, stop_event.set).start()
        started = time.monotonic()
        observation_ = self._observe(timeout_s=60.0, stop_event=stop_event)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.0, "observe held the shutdown for {0:.1f}s".format(elapsed))
        self.assertFalse(observation_.tsc_ran)
        self.assertIn("shutting down", observation_.tsc_errors[0])
        self.assertEqual(observation_.vitest, report_mod.empty_observation())


class SupervisorDegradeTest(unittest.TestCase):
    """Rule 8a and the build latch."""

    def make(self, **overrides: Any) -> Supervisor:
        parameters: Dict[str, Any] = {
            "spec": {}, "plan": {}, "controller": None, "gate_active": False,
        }
        parameters.update(overrides)
        return Supervisor(**parameters)

    def test_no_typecheck_but_green_tests_continues_to_the_build(self):
        # Stopping here ended a run whose app was perfectly green because its
        # `tsc` binary was missing. The tests and the build get the last word.
        supervisor = self.make()
        decision = supervisor.decide(
            observation(tsc_ran=False, tsc_ok=False, green=False, build_ran=False,
                        build_ok=None, tsc_errors=["could not run tsc"])
        )
        self.assertEqual(decision.action, "build")
        self.assertEqual(supervisor.tsc_skipped, 1)
        self.assertEqual(supervisor.repairs, 0)

    def test_no_typecheck_and_red_tests_repairs_on_the_failures(self):
        supervisor = self.make()
        decision = supervisor.decide(
            observation(
                tsc_ran=False, tsc_ok=False, green=False, tsc_errors=["could not run tsc"],
                vitest={"green": False, "total": 1, "passed": 0, "failed": 1, "names": [],
                        "failures": [{"name": "journeys > lends a book",
                                      "message": "Unable to find text: Out: Sam"}]},
            )
        )
        self.assertEqual(decision.action, "repair")
        self.assertIn("Out: Sam", decision.brief_hint)

    def test_no_typecheck_and_no_test_at_all_still_stops(self):
        supervisor = self.make()
        decision = supervisor.decide(
            observation(tsc_ran=False, tsc_ok=False, green=False, tsc_errors=["could not run tsc"],
                        vitest=report_mod.empty_observation())
        )
        self.assertEqual(decision.action, "stop")
        self.assertIn("typecheck did not run", decision.rationale)

    def test_the_build_is_asked_for_once(self):
        supervisor = self.make()
        green = observation(build_ran=False, build_ok=None)
        self.assertEqual(supervisor.decide(green).action, "build")
        for _ in range(3):
            decision = supervisor.decide(green)
        self.assertEqual(decision.action, "done")
        self.assertEqual(supervisor.builds, 1)
        self.assertEqual(supervisor.repairs, 0)
        self.assertEqual(supervisor.summary()["builds"], 1)


class MissionThinkingTest(unittest.TestCase):
    """§9: a mission session never has thinking on, whatever the run asked for."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def _runner(self, thinking: str) -> missions_mod.MissionRunner:
        return missions_mod.MissionRunner(
            pi_binary=support.FAKE_PI,
            app_directory=self.root,
            harness_directory=self.root / "harness",
            session_root=self.root / "sessions",
            append_system="PROMPT",
            thinking=thinking,
            env={"PI_OFFLINE": "1"},
            deadline=time.monotonic() + 600.0,
        )

    def test_challenge_thinking_never_reaches_a_mission_session(self):
        runner = self._runner("high")
        self.assertEqual(runner.thinking, "off")
        self.assertEqual(runner.requested_thinking, "high")
        argv = missions_mod.base_args(thinking=runner.thinking)
        self.assertEqual(argv[argv.index("--thinking") + 1], "off")

    def test_thinking_off_is_left_alone(self):
        runner = self._runner("off")
        self.assertEqual((runner.thinking, runner.requested_thinking), ("off", "off"))


class LeftoverCloseTest(unittest.TestCase):
    """One shared deadline for every session a dead worker left behind."""

    def test_the_graces_shrink_to_what_is_left_of_the_deadline(self):
        full = missions_mod._scaled_close(time.monotonic() + 60.0)
        self.assertEqual(full, dict(missions_mod.FAST_CLOSE))

        half = missions_mod._scaled_close(time.monotonic() + sum(missions_mod.FAST_CLOSE.values()) / 2)
        self.assertLess(sum(half.values()), sum(missions_mod.FAST_CLOSE.values()))

        spent = missions_mod._scaled_close(time.monotonic() - 1.0)
        self.assertEqual(sum(spent.values()), 0.0)
        # Never negative: PiRpc.close would treat that as "poll once", not as
        # "wait forever", but a negative grace is a bug either way.
        self.assertTrue(all(value >= 0.0 for value in spent.values()))


class UsageAttributionTest(unittest.TestCase):
    """Two concurrent turns must not each be charged the same wall clock."""

    def test_a_parallel_turn_is_charged_its_own_wall_time(self):
        controller = BudgetController(time.monotonic() + 600.0)
        observer = _UsageObserver(controller)
        end = {"type": "message_end",
               "message": {"role": "assistant", "usage": {"output": 100}}}

        observer({"type": "agent_start"})
        observer({"type": "agent_start"})
        time.sleep(0.2)
        observer(end)          # first mission settles after 0.2 s
        time.sleep(0.1)
        observer(end)          # second settles 0.1 s later, but ran 0.3 s

        # Charging the second turn only the 0.1 s gap gives 200/0.3 = 667 tok/s;
        # charging it its own 0.3 s gives 200/0.5 = 400. The estimate must be
        # the honest, slower one -- an over-estimate admits missions the run
        # cannot finish.
        self.assertLess(controller.tokens_per_s, 550.0)
        self.assertGreater(controller.tokens_per_s, 250.0)

    def test_a_message_with_no_turn_start_still_measures_something(self):
        controller = BudgetController(time.monotonic() + 600.0)
        observer = _UsageObserver(controller)
        time.sleep(0.05)
        observer({"type": "message_end", "message": {"role": "assistant", "usage": {"output": 10}}})
        self.assertEqual(controller.cumulative_output, 10)
        self.assertGreater(controller.tokens_per_s, 0.0)


class BuilderBriefTest(unittest.TestCase):
    """What the Builder is told, checked against what the scaffold does."""

    def setUp(self) -> None:
        self.spec = books_spec()
        self.plan = plan_mod.derive_plan(self.spec)

    def _outline(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return plan_mod._config_outline(spec, plan_mod.derive_plan(spec))

    def test_empty_field_lists_are_omitted_not_written_as_empty_arrays(self):
        # `RecordList.metaNames` is `if (config.metaFields) return config.metaFields;`
        # and `[]` is truthy, so an empty array suppresses the row's meta line
        # instead of falling back to every listable field.
        spec = books_spec()
        spec["subtitle_fields"] = []
        spec["meta_fields"] = []
        outline = self._outline(spec)
        self.assertNotIn("subtitleFields", outline)
        self.assertNotIn("metaFields", outline)
        self.assertNotIn('"metaFields"', plan_mod.builder_brief(spec, plan_mod.derive_plan(spec)))

    def test_a_populated_field_list_still_reaches_the_builder(self):
        outline = self._outline(self.spec)
        self.assertTrue(outline["metaFields"])
        self.assertIn('"metaFields"', plan_mod.builder_brief(self.spec, self.plan))

    def test_the_builder_is_told_the_key_the_outline_actually_uses(self):
        brief = plan_mod.builder_brief(self.spec, self.plan)
        self.assertIn('"match"', brief, "a state filter's predicate arrives as `match`")
        self.assertIn("Every `match`/`when`/`available`/`compute`/`apply`", brief)

    def test_the_builder_is_told_never_to_open_a_browser_dialog(self):
        # The real Analyst returned "set borrower from prompt"; jsdom's
        # `window.prompt` returns undefined without throwing, so tsc stays
        # green and the badge silently never renders.
        brief = plan_mod.builder_brief(self.spec, self.plan)
        self.assertIn("Never call `prompt()`, `confirm()` or `alert()`", brief)
        self.assertIn("`(row, input) => ({ ... })`", brief)

    def test_a_number_field_only_declares_integer_when_the_spec_says_so(self):
        spec = books_spec()
        spec["fields"].append({"name": "rating", "label": "Rating", "kind": "number",
                               "required": False, "options": [], "integer": False,
                               "unit": "stars", "in_form": True, "message": ""})
        entry = [f for f in self._outline(spec)["fields"] if f["name"] == "rating"][0]
        self.assertNotIn("integer", entry)
        self.assertEqual(entry["min"], 0)


class TesterBriefTest(unittest.TestCase):
    """Every visible-string line the Tester needs to write a passing query."""

    def setUp(self) -> None:
        self.spec = books_spec()
        self.plan = plan_mod.derive_plan(self.spec)
        self.brief = plan_mod.tester_brief(self.spec, self.plan)

    def test_the_list_is_declared_sorted(self):
        # `_config_outline` hard-codes `sort: {field: titleField}`, so
        # `rowTitles()` is alphabetical, not insertion-ordered.
        self.assertIn("sorted by", self.brief)
        self.assertIn("alphabetically, not in the order you added them", self.brief)

    def test_delete_and_reload_are_described_as_the_scaffold_behaves(self):
        self.assertIn("never call `confirmDialog` after it", self.brief)
        self.assertIn("resets the search box and the active filter", self.brief)

    def test_a_badge_placeholder_is_shown_substituted(self):
        self.assertIn("substitute the row's own value", self.brief)
        self.assertIn('reads "Out: Sam"', self.brief)

    def test_a_chip_is_described_with_its_count_and_its_all_chip(self):
        self.assertIn('Every filter chip reads "<label> (count)"', self.brief)
        self.assertIn("never an anchored regex", self.brief)
        self.assertIn("clears every filter", self.brief)

    def test_a_validation_message_is_shown_in_both_places_it_renders(self):
        # `RecordForm` renders it as the field's alert *and* in the problem
        # summary as "<Label>: <message>", so a regex matches twice and throws.
        self.assertIn("problem summary", self.brief)
        self.assertIn("never a regex", self.brief)

    def test_a_shadowed_chip_label_is_called_out(self):
        spec = books_spec()
        for field in spec["fields"]:
            if field["kind"] == "select":
                field["options"] = list(field["options"]) + ["Out Of Print"]
        brief = plan_mod.tester_brief(spec, plan_mod.derive_plan(spec))
        self.assertIn("is the start of another chip's name", brief)

    def test_the_brief_stays_inside_its_budget(self):
        self.assertLessEqual(len(self.brief), plan_mod.MAX_BRIEF_CHARS)
        self.assertLessEqual(
            len(plan_mod.builder_brief(self.spec, self.plan)), plan_mod.MAX_BRIEF_CHARS
        )


class AnalystPromptTest(unittest.TestCase):
    """The four constraints that only lived in schema descriptions."""

    def test_the_system_prompt_carries_the_style_rules(self):
        prompt = analyst.SYSTEM_PROMPT
        self.assertIn("journey titles are lowercase verb phrases", prompt)
        self.assertIn("never a prompt or dialog", prompt)
        self.assertIn("count of rows where <predicate>", prompt)

    def test_the_effect_description_forbids_a_dialog(self):
        effect = analyst.SCHEMA["properties"]["actions"]["items"]["properties"]["effect"]
        self.assertIn("never mention a prompt, dialog or popup", effect["description"])

    def test_a_number_field_owns_its_integer_decision(self):
        field = analyst.SCHEMA["properties"]["fields"]["items"]
        self.assertIn("integer", field["properties"])
        self.assertIn("integer", field["required"])
        spec = analyst.normalize_spec({
            "fields": [
                {"name": "rating", "label": "Rating", "kind": "number", "integer": False},
                {"name": "count", "label": "Count", "kind": "number"},
                {"name": "title", "label": "Title", "kind": "text"},
            ],
            "title_field": "title",
        })
        by_name = {field["name"]: field for field in spec["fields"]}
        self.assertFalse(by_name["rating"]["integer"])
        self.assertTrue(by_name["count"]["integer"], "a count is whole by default")
        self.assertFalse(by_name["title"]["integer"], "only a number field carries it")


if __name__ == "__main__":
    unittest.main()
