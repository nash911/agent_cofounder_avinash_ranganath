"""Unit tests for :mod:`harness.supervisor` -- the policy, the escalation, the review.

The observations here are hand-built ``SimpleNamespace`` objects carrying the
field names PHASE3_DESIGN §4 fixes for ``Observation``, not real
``harness.observe`` results: the Supervisor's contract is those field names, and
a policy test that had to run ``tsc`` and vitest to reach a decision would be a
much slower test of a much smaller thing. One test feeds the same fields as a
plain ``dict`` to prove both shapes decide identically.

Model calls go to :class:`harness.tests.fake_gateway.FakeGatewayServer` on
127.0.0.1 -- never to a real gateway -- and ``sys.stdout`` is redirected the way
``test_gateway.py`` does it, because a real :class:`GatewayClient` emits its
synthetic ``message_end`` line through ``forward_record``.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import time
import types
import unittest
from typing import Any, Dict, List
from unittest import mock

from harness import gateway, pirpc, supervisor
from harness.budget import BudgetController
from harness.tests import support
from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

CONFIG = "src/app-config.ts"
TESTS = "src/journeys.test.tsx"

PLAN: Dict[str, Any] = {
    "files": {"config": CONFIG, "tests": TESTS},
    "tests": [{"title": "adds a book", "kind": "explicit", "steps": "…", "expect": "row shown"}],
}

SPEC: Dict[str, Any] = {
    "app_name": "Home Library",
    "journeys": [
        {"title": "adds a book", "kind": "explicit", "steps": "add", "expect": "row shown"},
        {"title": "lends a book", "kind": "explicit", "steps": "lend", "expect": "badge shown"},
    ],
}


def observation(**overrides: Any) -> types.SimpleNamespace:
    """A green observation with every field §4 promises; override what a test needs."""
    fields: Dict[str, Any] = {
        "tsc_ran": True,
        "tsc_ok": True,
        "tsc_errors": [],
        "vitest": {
            "green": True,
            "total": 2,
            "passed": 2,
            "failed": 0,
            "names": ["adds a book", "lends a book"],
            "failures": [],
        },
        "build_ran": False,
        "build_ok": None,
        "build_tail": "",
        "files": {
            CONFIG: {"exists": True, "lines": 62, "changed_from_seed": True},
            TESTS: {"exists": True, "lines": 118, "changed_from_seed": True},
        },
        "over_limit": [],
        "coverage": {"missing": [], "matched": 2, "total": 2},
        "signature": "sig-a",
        "green": True,
        "elapsed_s": 1.5,
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def failing_vitest(name: str = "adds a book", message: str = "Unable to find Lend") -> Dict[str, Any]:
    return {
        "green": False,
        "total": 2,
        "passed": 1,
        "failed": 1,
        "names": ["lends a book"],
        "failures": [{"name": name, "message": message}],
    }


class _SupervisorTestCase(unittest.TestCase):
    """A temp harness dir, a generous budget, and a deterministic reviewer flag."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.harness_dir = self.root / "harness"
        self.app_dir = self.root / "app"
        (self.app_dir / "src").mkdir(parents=True, exist_ok=True)
        # HARNESS_REVIEWER may be set in the developer's shell; every test that
        # cares sets it explicitly, so the default here is a hard "off".
        self._env = mock.patch.dict("os.environ", {"HARNESS_REVIEWER": "0"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def controller(self, seconds_left: float = 600.0) -> BudgetController:
        return BudgetController(deadline_monotonic=time.monotonic() + seconds_left)

    def make(self, **kwargs: Any) -> supervisor.Supervisor:
        defaults: Dict[str, Any] = dict(
            spec=SPEC,
            plan=PLAN,
            controller=self.controller(),
            gate_active=False,
            harness_dir=self.harness_dir,
            app_dir=self.app_dir,
        )
        defaults.update(kwargs)
        return supervisor.Supervisor(**defaults)


class PolicyOrderTest(_SupervisorTestCase):
    """The thirteen rules of §6, each reachable, in the stated order."""

    def test_rule_2_config_unchanged_reruns_the_builder_exactly_once(self):
        sup = self.make()
        obs = observation(
            files={
                CONFIG: {"exists": True, "lines": 40, "changed_from_seed": False},
                TESTS: {"exists": True, "lines": 118, "changed_from_seed": True},
            }
        )

        first = sup.decide(obs)
        self.assertEqual((first.action, first.role, first.source), ("rerun", "builder", "policy"))
        self.assertIn(CONFIG, first.brief_hint)

        # Same observation again: the rerun is spent, so the policy moves on.
        second = sup.decide(obs)
        self.assertEqual(second.action, "build")
        self.assertEqual(sup.reruns["builder"], 1)
        self.assertEqual(sup.repairs, 0)

    def test_rule_3_missing_test_file_reruns_the_tester_exactly_once(self):
        sup = self.make()
        obs = observation(
            files={
                CONFIG: {"exists": True, "lines": 62, "changed_from_seed": True},
                TESTS: {"exists": False, "lines": 0, "changed_from_seed": False},
            }
        )

        first = sup.decide(obs)
        self.assertEqual((first.action, first.role), ("rerun", "tester"))
        self.assertIn(TESTS, first.brief_hint)

        second = sup.decide(obs)
        self.assertEqual(second.action, "build")
        self.assertEqual(sup.reruns["tester"], 1)

    def test_rule_7_over_limit_repairs_and_names_the_file_and_beats_tsc(self):
        sup = self.make()
        decision = sup.decide(
            observation(
                over_limit=[TESTS],
                green=False,
                tsc_ok=False,
                tsc_errors=["src/app-config.ts(3,1): error TS2322: nope"],
            )
        )
        self.assertEqual((decision.action, decision.role), ("repair", "repairer"))
        self.assertIn(TESTS, decision.brief_hint)
        self.assertIn("150", decision.brief_hint)
        # Rule 7 comes before rule 8: the tsc lines are not what this repair is about.
        self.assertNotIn("TS2322", decision.brief_hint)
        self.assertEqual(sup.repairs, 1)

    def test_rule_8_tsc_red_repairs_with_the_tsc_lines_and_nothing_else(self):
        sup = self.make()
        decision = sup.decide(
            observation(
                green=False,
                tsc_ok=False,
                tsc_errors=[
                    "src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable",
                    "src/journeys.test.tsx(4,1): error TS2304: Cannot find name 'foo'",
                ],
                vitest=failing_vitest(message="Unable to find an element with the text: Lend"),
            )
        )
        self.assertEqual(decision.action, "repair")
        self.assertIn("TS2322", decision.brief_hint)
        self.assertIn("TS2304", decision.brief_hint)
        # Rule 8 is the fast path: vitest was skipped, so its noise stays out.
        self.assertNotIn("Unable to find", decision.brief_hint)

    def test_rule_8_a_typecheck_that_never_ran_stops_instead_of_repairing(self):
        # observe.py returns tsc_ran=False for a spawn failure or a timeout and
        # skips vitest; repairing against errors nobody produced would burn a
        # mission on a broken toolchain.
        sup = self.make()
        decision = sup.decide(
            observation(
                green=False,
                tsc_ran=False,
                tsc_ok=False,
                tsc_errors=["could not run /app/node_modules/.bin/tsc: No such file"],
                vitest={"green": False, "total": 0, "passed": 0, "failed": 0, "names": [], "failures": []},
            )
        )
        self.assertEqual(decision.action, "stop")
        self.assertIn("typecheck did not run", decision.rationale)
        self.assertIn("No such file", decision.rationale)
        self.assertEqual(sup.repairs, 0)

    def test_rule_9_vitest_red_repairs_with_the_failures(self):
        sup = self.make()
        decision = sup.decide(
            observation(
                green=False,
                vitest=failing_vitest(
                    name="lends a book", message="Unable to find an element with the text: Lend"
                ),
            )
        )
        self.assertEqual(decision.action, "repair")
        self.assertIn("lends a book", decision.brief_hint)
        self.assertIn("Unable to find an element", decision.brief_hint)
        self.assertEqual(sup.repairs, 1)

    def test_rule_10_coverage_repair_only_behind_the_flag(self):
        obs = observation(
            build_ran=True,
            build_ok=True,
            coverage={"missing": ["reloads and keeps the data"], "matched": 1, "total": 2},
        )

        without_flag = self.make()
        self.assertEqual(without_flag.decide(obs).action, "done")

        with_flag = self.make(coverage_repair=True)
        decision = with_flag.decide(obs)
        self.assertEqual(decision.action, "repair")
        self.assertIn("reloads and keeps the data", decision.brief_hint)
        self.assertEqual(with_flag.repairs, 1)

    def test_rule_11_green_without_a_build_asks_for_the_build(self):
        sup = self.make()
        decision = sup.decide(observation(build_ran=False))
        self.assertEqual((decision.action, decision.role, decision.source), ("build", "", "policy"))
        self.assertEqual(sup.repairs, 0)

    def test_rule_12_failed_build_repairs_and_counts_toward_the_cap(self):
        sup = self.make()
        decision = sup.decide(
            observation(build_ran=True, build_ok=False, build_tail="error: Rollup failed to resolve")
        )
        self.assertEqual(decision.action, "repair")
        self.assertIn("Rollup failed to resolve", decision.brief_hint)
        self.assertEqual(sup.repairs, 1)

    def test_rule_13_green_and_built_is_done(self):
        sup = self.make()
        decision = sup.decide(observation(build_ran=True, build_ok=True))
        self.assertEqual((decision.action, decision.role, decision.source), ("done", "", "policy"))

    def test_rule_4_repair_cap_stops_after_three_repairs(self):
        sup = self.make()
        for index in range(3):
            decision = sup.decide(
                observation(
                    green=False,
                    tsc_ok=False,
                    tsc_errors=["src/app-config.ts({0},1): error TS2322: x".format(index)],
                    signature="sig-{0}".format(index),
                )
            )
            self.assertEqual(decision.action, "repair", "round {0}".format(index))

        stopped = sup.decide(
            observation(green=False, tsc_ok=False, tsc_errors=["e"], signature="sig-final")
        )
        self.assertEqual(stopped.action, "stop")
        self.assertIn("repair cap", stopped.rationale)
        self.assertEqual(sup.repairs, 3)

    def test_a_green_observation_on_the_cap_th_repair_is_built_not_stopped(self):
        # Regression (2026-09-04, jobhunt holdout): the 3rd repair turned the app
        # green with all tests passing, but the repair cap fired before the green
        # branch and the run was reported `partial`. A green observation must
        # reach build/done regardless of how many repairs preceded it.
        sup = self.make()
        for index in range(3):
            decision = sup.decide(
                observation(
                    green=False,
                    tsc_ok=False,
                    tsc_errors=["src/app-config.ts({0},1): error TS2322: x".format(index)],
                    signature="sig-{0}".format(index),
                )
            )
            self.assertEqual(decision.action, "repair", "round {0}".format(index))
        self.assertEqual(sup.repairs, 3)

        # The next observation is GREEN even though repairs == repair_cap.
        healed = sup.decide(observation(build_ran=False))
        self.assertEqual((healed.action, healed.source), ("build", "policy"))
        built = sup.decide(observation(build_ran=True, build_ok=True))
        self.assertEqual(built.action, "done")

    def test_rule_5_two_no_progress_rounds_stop(self):
        sup = self.make()
        stuck = observation(
            green=False,
            tsc_ok=False,
            tsc_errors=["src/app-config.ts(12,5): error TS2322: x"],
            signature="stuck",
        )

        self.assertEqual(sup.decide(stuck).action, "repair")
        self.assertEqual(sup.decide(stuck).action, "repair")  # one wasted round, no client
        stopped = sup.decide(stuck)
        self.assertEqual(stopped.action, "stop")
        self.assertIn("no progress", stopped.rationale)
        self.assertEqual(sup.no_progress, 2)

    def test_a_repair_that_changes_the_signature_resets_the_streak(self):
        sup = self.make(repair_cap=9)
        stuck = observation(green=False, tsc_ok=False, tsc_errors=["a"], signature="one")
        moved = observation(green=False, tsc_ok=False, tsc_errors=["b"], signature="two")

        sup.decide(stuck)
        sup.decide(stuck)
        self.assertEqual(sup.no_progress, 1)
        sup.decide(moved)
        self.assertEqual(sup.no_progress, 0)

    def test_a_build_decision_does_not_count_as_a_wasted_repair(self):
        sup = self.make()
        green = observation(signature="same")
        self.assertEqual(sup.decide(green).action, "build")
        # Rule 11 is latched: a build that was asked for and still did not run
        # (no vite binary, no budget left) cannot be asked for again -- that
        # loop ran 12 rounds of a full tsc + vitest pair and ended on the
        # loop's own round cap. It finishes honestly instead.
        second = sup.decide(green)
        self.assertEqual(second.action, "done")
        self.assertIn("could not run", second.rationale)
        self.assertEqual(sup.no_progress, 0)
        self.assertEqual(sup.repairs, 0)
        self.assertEqual(sup.builds, 1)

    def test_stop_event_beats_every_other_rule(self):
        stop_event = types.SimpleNamespace(is_set=lambda: True)
        sup = self.make(stop_event=stop_event)
        decision = sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["e"]))
        self.assertEqual(decision.action, "stop")
        self.assertIn("shutdown", decision.rationale)
        self.assertEqual(sup.repairs, 0)

    def test_a_plain_dict_observation_decides_identically(self):
        sup = self.make()
        as_dict = dict(vars(observation(green=False, tsc_ok=False, tsc_errors=["error TS1005"])))
        decision = sup.decide(as_dict)
        self.assertEqual(decision.action, "repair")
        self.assertIn("TS1005", decision.brief_hint)


class BudgetGateTest(_SupervisorTestCase):
    def test_a_refused_repair_becomes_a_stop_carrying_the_reason(self):
        sup = self.make(gate_active=True, controller=self.controller(seconds_left=1.0))
        decision = sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["e"]))
        self.assertEqual(decision.action, "stop")
        self.assertIn("budget refused", decision.rationale)
        self.assertIn("remaining", decision.rationale)
        self.assertEqual(sup.repairs, 0, "a refused repair must not count toward the cap")

    def test_a_refused_rerun_becomes_a_stop(self):
        sup = self.make(gate_active=True, controller=self.controller(seconds_left=1.0))
        decision = sup.decide(
            observation(
                files={
                    CONFIG: {"exists": True, "lines": 40, "changed_from_seed": False},
                    TESTS: {"exists": True, "lines": 118, "changed_from_seed": True},
                }
            )
        )
        self.assertEqual(decision.action, "stop")
        self.assertIn("budget refused", decision.rationale)
        self.assertEqual(sup.reruns["builder"], 0)

    def test_an_inactive_gate_never_refuses(self):
        sup = self.make(gate_active=False, controller=self.controller(seconds_left=1.0))
        self.assertEqual(
            sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["e"])).action, "repair"
        )

    def test_the_local_gate_wrapper_matches_the_orchestrator_s(self):
        # supervisor._gate_reason is a deliberate copy of __main__.budget_gate_reason
        # (importing the orchestrator by package name under `python3 -m harness`
        # would execute a second copy of it). Prove the two agree.
        from harness.__main__ import budget_gate_reason

        rich = self.controller(seconds_left=600.0)
        poor = self.controller(seconds_left=1.0)
        for controller in (rich, poor):
            self.assertEqual(
                supervisor._gate_reason(controller, supervisor.REPAIRER_PREDICTED_OUTPUT_TOKENS),
                budget_gate_reason(controller, supervisor.REPAIRER_PREDICTED_OUTPUT_TOKENS),
            )


class _GatewayBackedTestCase(_SupervisorTestCase):
    """Adds a scripted fake gateway and a redirected ``forward_record`` sink."""

    def setUp(self) -> None:
        super().setUp()
        self.server = FakeGatewayServer()
        self.server.start()
        self.sink = io.BytesIO()
        self._stdout_patch = mock.patch.object(pirpc.sys, "stdout", mock.Mock(buffer=self.sink))
        self._stdout_patch.start()

    def tearDown(self) -> None:
        self._stdout_patch.stop()
        self.server.stop()
        super().tearDown()

    def make_client(self, **kwargs: Any) -> gateway.GatewayClient:
        defaults = dict(
            base_url=self.server.base_url,
            api_key="test-key",
            model="zai-org/GLM-5.2",
            provider="berget",
            harness_dir=self.harness_dir,
            backoff=(0.01, 0.01),
        )
        defaults.update(kwargs)
        return gateway.GatewayClient(**defaults)

    def script_json(self, payloads: List[Dict[str, Any]]) -> None:
        self.server.script(
            [
                ScriptedResponse(status=200, body=ok_response(content=json.dumps(payload)))
                for payload in payloads
            ]
        )

    def request_bodies(self) -> List[Dict[str, Any]]:
        with self.server.lock:
            return [dict(entry["body"]) for entry in self.server.requests]


class ModelSupervisorTest(_GatewayBackedTestCase):
    """Rule 6: one wasted repair buys a second opinion, and only then."""

    def stuck(self, signature: str = "stuck") -> types.SimpleNamespace:
        return observation(
            green=False,
            tsc_ok=False,
            tsc_errors=["src/app-config.ts(12,5): error TS2322: x"],
            signature=signature,
        )

    def test_a_valid_model_decision_is_used_and_names_the_target_file(self):
        (self.app_dir / "src" / "app-config.ts").write_text(
            "export const MARKER_CONFIG = 1;\n", encoding="utf-8"
        )
        self.script_json(
            [
                {
                    "action": "repair_tests",
                    "brief": "Query the Lend button by its exact label.",
                    "rationale": "the test uses the wrong label",
                }
            ]
        )
        sup = self.make(client=self.make_client())

        self.assertEqual(sup.decide(self.stuck()).source, "policy")
        decision = sup.decide(self.stuck())

        self.assertEqual((decision.action, decision.role, decision.source), ("repair", "repairer", "model"))
        self.assertIn(TESTS, decision.brief_hint)
        self.assertIn("Query the Lend button", decision.brief_hint)
        self.assertIn("the test uses the wrong label", decision.rationale)
        self.assertEqual(sup.model_calls, 1)
        self.assertEqual(sup.repairs, 2, "a model repair counts toward the cap")

        # The prompt carries the journeys, the file heads and the history.
        body = self.request_bodies()[0]
        prompt = body["messages"][1]["content"]
        self.assertIn("lends a book", prompt)
        self.assertIn("MARKER_CONFIG", prompt)
        self.assertIn("repair repairer (policy)", prompt)
        self.assertEqual(body["max_tokens"], supervisor.MODEL_MAX_TOKENS)

    def test_a_second_identical_decision_turns_thinking_on_for_the_next_call(self):
        reply = {"action": "repair_config", "brief": "Rename the field.", "rationale": "same again"}
        self.script_json([reply, reply, reply])
        sup = self.make(client=self.make_client(), repair_cap=99, no_progress_cap=99)

        sup.decide(self.stuck())  # policy repair
        first = sup.decide(self.stuck())  # model call 1
        second = sup.decide(self.stuck())  # model call 2 -- identical action
        third = sup.decide(self.stuck())  # model call 3 -- thinking on

        self.assertEqual([first.source, second.source, third.source], ["model", "model", "model-thinking"])
        bodies = self.request_bodies()
        self.assertEqual(len(bodies), 3)
        self.assertFalse(bodies[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(bodies[1]["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(bodies[2]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(bodies[2]["thinking_token_budget"], supervisor.MODEL_THINKING_TOKEN_BUDGET)
        self.assertNotIn("thinking_token_budget", bodies[0])
        self.assertEqual((sup.model_calls, sup.model_thinking_calls), (3, 1))

    def test_a_model_stop_is_honoured(self):
        self.script_json(
            [{"action": "stop", "brief": "", "rationale": "the spec cannot be satisfied"}]
        )
        sup = self.make(client=self.make_client())

        sup.decide(self.stuck())
        decision = sup.decide(self.stuck())
        self.assertEqual((decision.action, decision.source), ("stop", "model"))
        self.assertIn("cannot be satisfied", decision.rationale)

    def test_an_invalid_model_reply_falls_through_to_the_policy(self):
        self.script_json([{"action": "reboot_the_universe", "brief": "", "rationale": ""}])
        sup = self.make(client=self.make_client())

        sup.decide(self.stuck())
        decision = sup.decide(self.stuck())

        self.assertEqual((decision.action, decision.source), ("repair", "policy"))
        self.assertIn("TS2322", decision.brief_hint)
        self.assertEqual(sup.model_calls, 1)

    def test_a_failed_model_call_falls_through_to_the_policy(self):
        self.server.script([ScriptedResponse(status=503, body={}) for _ in range(4)])
        sup = self.make(client=self.make_client())

        sup.decide(self.stuck())
        decision = sup.decide(self.stuck())

        self.assertEqual((decision.action, decision.source), ("repair", "policy"))

    def test_the_caps_are_checked_before_the_model_is_asked(self):
        self.script_json([{"action": "repair_config", "brief": "b", "rationale": "r"}])
        capped = self.make(client=self.make_client(), repair_cap=1)
        capped.decide(self.stuck())
        self.assertEqual(capped.decide(self.stuck()).action, "stop")

        stalled = self.make(client=self.make_client(), no_progress_cap=1)
        stalled.decide(self.stuck())
        self.assertEqual(stalled.decide(self.stuck()).action, "stop")

        self.assertEqual(self.request_bodies(), [], "no model call once a cap has fired")

    def test_no_model_call_while_the_policy_still_has_an_answer(self):
        self.script_json([{"action": "repair_config", "brief": "b", "rationale": "r"}])
        sup = self.make(client=self.make_client())
        sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["e"], signature="one"))
        sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["f"], signature="two"))
        self.assertEqual(self.request_bodies(), [])


class ReviewerTest(_GatewayBackedTestCase):
    """§6's Reviewer: off by default, one call, a high finding buys one repair."""

    def green_built(self) -> types.SimpleNamespace:
        return observation(build_ran=True, build_ok=True)

    def test_disabled_by_default_no_call_and_done(self):
        self.script_json([{"findings": []}])
        sup = self.make(client=self.make_client())
        self.assertEqual(sup.decide(self.green_built()).action, "done")
        self.assertEqual(self.request_bodies(), [])
        self.assertFalse(sup.review_ran)
        self.assertFalse((self.harness_dir / "review.json").exists())

    def test_a_high_finding_becomes_one_repair_and_writes_review_json(self):
        self.script_json(
            [
                {
                    "findings": [
                        {
                            "severity": "high",
                            "file": CONFIG,
                            "problem": "The Lend action has no input label",
                            "fix": "Add input.label 'Borrower name'",
                        },
                        {"severity": "low", "file": TESTS, "problem": "verbose", "fix": "trim"},
                    ]
                }
            ]
        )
        sup = self.make(client=self.make_client())
        with mock.patch.dict("os.environ", {"HARNESS_REVIEWER": "1"}):
            decision = sup.decide(self.green_built())

        self.assertEqual((decision.action, decision.role, decision.source), ("repair", "repairer", "model"))
        self.assertIn("The Lend action has no input label", decision.brief_hint)
        self.assertNotIn("verbose", decision.brief_hint, "only high findings drive the repair")
        self.assertEqual(sup.repairs, 1, "the review repair counts toward the cap")

        written = json.loads((self.harness_dir / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(len(written["findings"]), 2)
        self.assertEqual(written["findings"][0]["severity"], "high")

    def test_no_high_finding_is_done_and_the_review_runs_once(self):
        self.script_json(
            [
                {"findings": [{"severity": "medium", "file": TESTS, "problem": "p", "fix": "f"}]},
                {"findings": [{"severity": "high", "file": TESTS, "problem": "late", "fix": "f"}]},
            ]
        )
        sup = self.make(client=self.make_client())
        with mock.patch.dict("os.environ", {"HARNESS_REVIEWER": "1"}):
            first = sup.decide(self.green_built())
            second = sup.decide(self.green_built())

        self.assertEqual((first.action, second.action), ("done", "done"))
        self.assertEqual(len(self.request_bodies()), 1, "the reviewer runs at most once")
        self.assertTrue((self.harness_dir / "review.json").exists())

    def test_the_reviewer_bundles_the_journeys_and_both_files(self):
        (self.app_dir / "src" / "app-config.ts").write_text("// MARKER_CONFIG\n", encoding="utf-8")
        (self.app_dir / "src" / "journeys.test.tsx").write_text("// MARKER_TESTS\n", encoding="utf-8")
        self.script_json([{"findings": []}])
        reviewer = supervisor.Reviewer(
            client=self.make_client(),
            spec=SPEC,
            plan=PLAN,
            app_dir=self.app_dir,
            harness_dir=self.harness_dir,
        )
        findings = reviewer.review()

        self.assertEqual(findings, [])
        prompt = self.request_bodies()[0]["messages"][1]["content"]
        self.assertIn("MARKER_CONFIG", prompt)
        self.assertIn("MARKER_TESTS", prompt)
        self.assertIn("lends a book", prompt)
        self.assertEqual(
            json.loads((self.harness_dir / "review.json").read_text(encoding="utf-8")),
            {"findings": []},
        )

    def test_a_failed_review_yields_no_findings_and_still_writes_the_file(self):
        self.server.script([ScriptedResponse(status=503, body={}) for _ in range(4)])
        reviewer = supervisor.Reviewer(
            client=self.make_client(), spec=SPEC, plan=PLAN, harness_dir=self.harness_dir
        )
        self.assertEqual(reviewer.review(), [])
        self.assertEqual(
            json.loads((self.harness_dir / "review.json").read_text(encoding="utf-8")),
            {"findings": []},
        )

    def test_reviewer_enabled_reads_the_env_flag(self):
        with mock.patch.dict("os.environ", {"HARNESS_REVIEWER": "1"}):
            self.assertTrue(supervisor.reviewer_enabled())
        with mock.patch.dict("os.environ", {"HARNESS_REVIEWER": ""}):
            self.assertFalse(supervisor.reviewer_enabled())


class LoopSequenceTest(_SupervisorTestCase):
    """The whole §7 loop, the way ``__main__`` drives it, ends in ``done``."""

    def test_a_realistic_repair_sequence_terminates(self):
        sup = self.make()
        rounds = [
            observation(green=False, tsc_ok=False, tsc_errors=["a(1,1): error TS2322: x"], signature="s1"),
            observation(green=False, vitest=failing_vitest(), signature="s2"),
            observation(signature="s3"),
            observation(build_ran=True, build_ok=True, signature="s3"),
        ]
        actions = []
        for round_observation in rounds:
            decision = sup.decide(round_observation)
            actions.append(decision.action)
            if decision.action in ("repair", "rerun"):
                sup.record(decision, mission_result(role=decision.role))

        self.assertEqual(actions, ["repair", "repair", "build", "done"])
        self.assertEqual((sup.repairs, sup.no_progress), (2, 0))
        summary = sup.summary()
        self.assertEqual(len(summary["decisions"]), 4)
        self.assertEqual(len(summary["observations"]), 4)
        self.assertEqual(summary["final_action"], "done")


class NeighbourInterfaceTest(_SupervisorTestCase):
    """The seams with the modules written in parallel (M2's observe, M3's missions).

    Skipped rather than failed when a neighbour has not landed yet: the policy
    itself is proven above against the field names the design fixes.
    """

    def test_a_real_observation_dataclass_and_its_as_dict_decide_identically(self):
        try:
            from harness.observe import CONFIG_FILE, TESTS_FILE, Observation
        except ImportError as exc:  # pragma: no cover - only before M2 lands
            self.skipTest("harness.observe unavailable: {0}".format(exc))

        self.assertEqual((CONFIG_FILE, TESTS_FILE), (CONFIG, TESTS))
        real = Observation(
            tsc_ran=True,
            tsc_ok=False,
            tsc_errors=["src/app-config.ts(12,5): error TS2322: Type 'string'"],
            files={
                CONFIG: {"exists": True, "lines": 62, "changed_from_seed": True},
                TESTS: {"exists": True, "lines": 118, "changed_from_seed": True},
            },
            signature="real-signature",
        )
        from_object = self.make().decide(real)
        from_dict = self.make().decide(real.as_dict())

        self.assertEqual(from_object.action, "repair")
        self.assertIn("TS2322", from_object.brief_hint)
        self.assertEqual(from_object.as_dict(), from_dict.as_dict())

    def test_a_real_green_observation_asks_for_the_build(self):
        try:
            from harness.observe import Observation
        except ImportError as exc:  # pragma: no cover - only before M2 lands
            self.skipTest("harness.observe unavailable: {0}".format(exc))

        real = Observation(
            tsc_ran=True,
            tsc_ok=True,
            vitest={"green": True, "total": 2, "passed": 2, "failed": 0, "names": [], "failures": []},
            files={
                CONFIG: {"exists": True, "lines": 62, "changed_from_seed": True},
                TESTS: {"exists": True, "lines": 118, "changed_from_seed": True},
            },
            green=True,
        )
        self.assertEqual(self.make().decide(real).action, "build")

    def test_the_repairer_prediction_matches_the_missions_module(self):
        try:
            from harness.missions import REPAIRER_PREDICTED_OUTPUT_TOKENS
        except ImportError as exc:  # pragma: no cover - only before M3 lands
            self.skipTest("harness.missions unavailable: {0}".format(exc))
        self.assertEqual(
            REPAIRER_PREDICTED_OUTPUT_TOKENS, supervisor.REPAIRER_PREDICTED_OUTPUT_TOKENS
        )

    def test_record_digests_a_real_mission_result(self):
        try:
            from harness.missions import MissionResult
        except ImportError as exc:  # pragma: no cover - only before M3 lands
            self.skipTest("harness.missions unavailable: {0}".format(exc))

        sup = self.make()
        decision = sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["e"]))
        sup.record(
            decision,
            MissionResult(
                role="repairer",
                label="3-repairer",
                session_dir=self.root / "sessions" / "3-repairer",
                settled=True,
                success=True,
                output_tokens=640,
                wall_s=9.5,
            ),
        )
        mission = sup.summary()["decisions"][0]["mission"]
        self.assertEqual(mission["label"], "3-repairer")
        self.assertEqual(mission["output_tokens"], 640)
        self.assertTrue(mission["success"])
        json.dumps(sup.summary())


def mission_result(**overrides: Any) -> types.SimpleNamespace:
    """A stand-in for ``harness.missions.MissionResult`` (§5's field names)."""
    fields: Dict[str, Any] = {
        "role": "repairer",
        "label": "3-repairer",
        "session_dir": pathlib.Path("/tmp/does-not-matter"),
        "settled": True,
        "success": True,
        "interrupted": False,
        "timed_out": False,
        "error": None,
        "stop_reason": "stop",
        "output_tokens": 812,
        "wall_s": 12.3456,
        "resume_attempts": 0,
        "skipped_reason": None,
        "text": "done",
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class SummaryTest(_SupervisorTestCase):
    def test_summary_carries_every_decision_its_result_and_the_counters(self):
        sup = self.make(coverage_repair=True)
        red = observation(green=False, tsc_ok=False, tsc_errors=["e"], signature="one")

        first = sup.decide(red)
        sup.record(first, mission_result(output_tokens=812))
        second = sup.decide(observation(build_ran=True, build_ok=True, signature="two"))
        sup.record(second, None)

        summary = sup.summary()
        self.assertEqual([entry["action"] for entry in summary["decisions"]], ["repair", "done"])
        self.assertEqual(summary["decisions"][0]["role"], "repairer")
        self.assertEqual(summary["decisions"][0]["source"], "policy")
        self.assertEqual(summary["decisions"][0]["signature"], "one")
        self.assertEqual(summary["decisions"][0]["mission"]["output_tokens"], 812)
        self.assertEqual(summary["decisions"][0]["mission"]["label"], "3-repairer")
        self.assertEqual(summary["decisions"][0]["mission"]["wall_s"], 12.346)
        self.assertIsNone(summary["decisions"][1]["mission"])

        self.assertEqual(summary["repairs"], 1)
        self.assertEqual(summary["repair_cap"], 3)
        self.assertEqual(summary["reruns"], {"builder": 0, "tester": 0})
        self.assertEqual(summary["no_progress"], 0)
        self.assertEqual(summary["no_progress_cap"], 2)
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["model_thinking_calls"], 0)
        self.assertTrue(summary["coverage_repair"])
        self.assertEqual(summary["review"], {"enabled": False, "ran": False, "findings": []})
        self.assertEqual(summary["final_action"], "done")
        self.assertTrue(summary["final_rationale"])

        self.assertEqual(len(summary["observations"]), 2)
        self.assertEqual(summary["observations"][0]["signature"], "one")
        self.assertEqual(summary["observations"][1]["build_ok"], True)

        # supervisor.json is written straight from this dict.
        json.dumps(summary)

    def test_record_without_a_matching_decision_appends_an_entry(self):
        sup = self.make()
        orphan = supervisor.Decision("repair", "repairer", "h", "r", "policy")
        sup.record(orphan, mission_result(success=False, error="boom"))
        summary = sup.summary()
        self.assertEqual(len(summary["decisions"]), 1)
        self.assertEqual(summary["decisions"][0]["mission"]["error"], "boom")
        self.assertFalse(summary["decisions"][0]["mission"]["success"])

    def test_record_accepts_a_mission_result_shaped_dict(self):
        sup = self.make()
        decision = sup.decide(observation(green=False, tsc_ok=False, tsc_errors=["e"]))
        sup.record(decision, dict(vars(mission_result(output_tokens=7, session_dir=""))))
        self.assertEqual(sup.summary()["decisions"][0]["mission"]["output_tokens"], 7)

    def test_decision_as_dict_round_trips(self):
        decision = supervisor.Decision("repair", "repairer", "hint", "why", "policy")
        self.assertEqual(
            decision.as_dict(),
            {
                "action": "repair",
                "role": "repairer",
                "brief_hint": "hint",
                "rationale": "why",
                "source": "policy",
            },
        )

    def test_a_broken_observation_stops_instead_of_raising(self):
        class Exploding:
            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("no observation here")

        sup = self.make()
        decision = sup.decide(Exploding())
        self.assertEqual(decision.action, "stop")
        self.assertIn("supervisor error", decision.rationale)


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
