"""Unit tests for :mod:`harness.budget` -- pure, no subprocess involved."""

from __future__ import annotations

import time
import unittest

from harness.budget import BudgetController


class PredictionTest(unittest.TestCase):
    def test_predict_seconds_uses_the_given_rate(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=25.0)
        self.assertAlmostEqual(controller.predict_seconds(1000), 40.0)

    def test_predict_seconds_zero_tokens_is_zero(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=25.0)
        self.assertEqual(controller.predict_seconds(0), 0.0)

    def test_negative_or_missing_tokens_are_treated_as_zero(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=25.0)
        self.assertEqual(controller.predict_seconds(-5), 0.0)

    def test_zero_or_negative_initial_rate_falls_back_to_thirty(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=0.0)
        self.assertAlmostEqual(controller.predict_seconds(300), 10.0)


class LiveRateTest(unittest.TestCase):
    def test_observe_usage_updates_tokens_per_s(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=30.0)
        controller.observe_usage(600, 20.0)  # 30 tok/s, matches the seed rate
        self.assertAlmostEqual(controller.tokens_per_s, 30.0, places=3)
        controller.observe_usage(1000, 10.0)  # 100 tok/s this call
        # Rolling average over the two calls: (600+1000) / (20+10) = 53.33...
        self.assertAlmostEqual(controller.tokens_per_s, 1600 / 30, places=3)

    def test_window_is_bounded_to_the_most_recent_calls(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=30.0)
        for _ in range(3):
            controller.observe_usage(300, 10.0)  # 30 tok/s
        controller.observe_usage(3000, 10.0)  # a single 300 tok/s outlier
        for _ in range(10):
            controller.observe_usage(300, 10.0)  # back to 30 tok/s, floods the outlier out
        self.assertAlmostEqual(controller.tokens_per_s, 30.0, places=3)

    def test_zero_output_or_zero_elapsed_calls_do_not_move_the_rate(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=30.0)
        controller.observe_usage(0, 5.0)
        controller.observe_usage(500, 0.0)
        self.assertAlmostEqual(controller.tokens_per_s, 30.0, places=3)

    def test_cumulative_and_peak_output_track_every_call(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0)
        controller.observe_usage(400, 10.0)
        controller.observe_usage(0, 0.0)  # an error turn: still counts toward cumulative
        controller.observe_usage(600, 15.0)
        self.assertEqual(controller.cumulative_output, 1000)
        self.assertEqual(controller.peak_output, 1000)

    def test_negative_elapsed_or_output_is_clamped_to_zero(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0)
        controller.observe_usage(-100, -5.0)
        self.assertEqual(controller.cumulative_output, 0)
        self.assertEqual(controller.peak_output, 0)


class CanStartTest(unittest.TestCase):
    def test_starts_when_time_and_ceiling_both_allow_it(self):
        controller = BudgetController(
            deadline_monotonic=time.monotonic() + 500.0, tokens_per_s=30.0,
            output_ceiling=18000, finish_margin_s=50.0,
        )
        ok, reason = controller.can_start(12000)  # predicted 400s + 50s margin = 450s < 500s
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")

    def test_refuses_when_predicted_finish_plus_margin_exceeds_remaining(self):
        controller = BudgetController(
            deadline_monotonic=time.monotonic() + 60.0, tokens_per_s=30.0,
            output_ceiling=18000, finish_margin_s=50.0,
        )
        ok, reason = controller.can_start(12000)
        self.assertFalse(ok)
        self.assertIn("predicted", reason)
        self.assertIn("remaining", reason)

    def test_finish_margin_alone_can_refuse_a_fast_prediction(self):
        controller = BudgetController(
            deadline_monotonic=time.monotonic() + 5.0, tokens_per_s=1_000_000.0,
            finish_margin_s=50.0,
        )
        ok, reason = controller.can_start(100)  # predicted_s ~0, but margin alone exceeds remaining
        self.assertFalse(ok)

    def test_refuses_past_the_output_ceiling_by_default(self):
        controller = BudgetController(
            deadline_monotonic=time.monotonic() + 10_000.0, tokens_per_s=1_000_000.0,
            output_ceiling=1000, finish_margin_s=0.0,
        )
        controller.observe_usage(900, 0.001)
        ok, reason = controller.can_start(200)  # 900 + 200 = 1100 > 1000
        self.assertFalse(ok)
        self.assertIn("ceiling", reason)

    def test_accept_partial_bypasses_the_ceiling_but_not_the_deadline(self):
        controller = BudgetController(
            deadline_monotonic=time.monotonic() + 10_000.0, tokens_per_s=1_000_000.0,
            output_ceiling=1000, finish_margin_s=0.0,
        )
        controller.observe_usage(900, 0.001)
        ok, _ = controller.can_start(200, accept_partial=True)
        self.assertTrue(ok)

        tight = BudgetController(
            deadline_monotonic=time.monotonic() + 1.0, tokens_per_s=1.0, finish_margin_s=50.0,
        )
        ok, reason = tight.can_start(100, accept_partial=True)
        self.assertFalse(ok, "accept_partial must not bypass the wall-clock deadline")

    def test_exactly_at_the_ceiling_is_allowed(self):
        controller = BudgetController(
            deadline_monotonic=time.monotonic() + 10_000.0, tokens_per_s=1_000_000.0,
            output_ceiling=1000, finish_margin_s=0.0,
        )
        controller.observe_usage(800, 0.001)
        ok, _ = controller.can_start(200)  # 800 + 200 == 1000, not over
        self.assertTrue(ok)


class MissionTrackingTest(unittest.TestCase):
    def test_begin_and_end_mission_round_trip(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=30.0)
        index = controller.begin_mission("builder", 12000)
        self.assertEqual(controller.predictions[index]["label"], "builder")
        self.assertEqual(controller.predictions[index]["predicted_output"], 12000)
        self.assertAlmostEqual(controller.predictions[index]["predicted_s"], 400.0)
        self.assertEqual(controller.predictions[index]["actual_output"], 0)
        self.assertEqual(controller.predictions[index]["actual_s"], 0.0)

        controller.end_mission(index, 11500, 380.25)
        self.assertEqual(controller.predictions[index]["actual_output"], 11500)
        self.assertAlmostEqual(controller.predictions[index]["actual_s"], 380.25)

    def test_end_mission_on_an_out_of_range_index_is_a_no_op(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0)
        controller.end_mission(7, 100, 1.0)  # must not raise
        self.assertEqual(controller.predictions, [])

    def test_multiple_missions_each_get_their_own_entry(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=30.0)
        builder = controller.begin_mission("builder", 12000)
        resume = controller.begin_mission("resume-1", 3000)
        controller.end_mission(builder, 12000, 400.0)
        controller.end_mission(resume, 500, 20.0)
        self.assertEqual(len(controller.predictions), 2)
        self.assertEqual(controller.predictions[0]["label"], "builder")
        self.assertEqual(controller.predictions[1]["label"], "resume-1")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_shape(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0, tokens_per_s=30.0)
        controller.observe_usage(300, 10.0)
        idx = controller.begin_mission("builder", 12000)
        controller.end_mission(idx, 300, 10.0)

        snapshot = controller.snapshot()
        self.assertIn("elapsed_s", snapshot)
        self.assertIn("cumulative_output", snapshot)
        self.assertIn("peak_output", snapshot)
        self.assertIn("tokens_per_s", snapshot)
        self.assertIn("predictions", snapshot)
        self.assertEqual(snapshot["cumulative_output"], 300)
        self.assertEqual(snapshot["peak_output"], 300)
        self.assertEqual(len(snapshot["predictions"]), 1)
        self.assertEqual(snapshot["predictions"][0]["actual_output"], 300)
        self.assertGreaterEqual(snapshot["elapsed_s"], 0.0)

    def test_snapshot_predictions_are_copies_not_live_references(self):
        controller = BudgetController(deadline_monotonic=time.monotonic() + 1000.0)
        controller.begin_mission("builder", 1000)
        snapshot = controller.snapshot()
        snapshot["predictions"][0]["label"] = "mutated"
        self.assertEqual(controller.predictions[0]["label"], "builder")


if __name__ == "__main__":
    unittest.main()
