"""Three application shapes the one-record config had no primitive for.

Each fixture here is a real Analyst spec from a failing third-party run,
rewritten in neutral vocabulary so that only its *shape* survives:

- ``hard-derived``  -- a per-row value computed from two date fields;
- ``hard-bulk``     -- an action whose effect applies to every record at once;
- ``hard-currency`` -- a number, a computed value and a stat carrying a
  currency unit, plus an instant action with neither input nor confirmation.

The shapes are what generalise: any idea that counts days between two dates,
resets every record, or prices anything lands on one of them. So the
assertions never name a domain -- they walk the fixture's own entries and
check that the primitive reaches both briefs, and the control case checks that
a spec *without* a shape gets none of its machinery.
"""

from __future__ import annotations

import json
import pathlib
import unittest
from typing import Any, Dict

from harness import analyst, plan as plan_mod

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> Dict[str, Any]:
    """One fixture, normalised -- exactly what ``spec.json`` would hold."""
    return analyst.normalize_spec(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


class Rendered:
    """A fixture with everything the two agents are handed for it."""

    def __init__(self, name: str) -> None:
        self.spec = load(name)
        self.plan = plan_mod.derive_plan(self.spec)
        self.outline = plan_mod._config_outline(self.spec, self.plan)
        self.builder = plan_mod.builder_brief(self.spec, self.plan)
        self.tester = plan_mod.tester_brief(self.spec, self.plan)


class EveryShapeIsUsableTest(unittest.TestCase):
    def test_each_fixture_still_builds_and_fits_the_budgets(self):
        for name in ("hard-derived.json", "hard-bulk.json", "hard-currency.json"):
            with self.subTest(name):
                rendered = Rendered(name)
                self.assertTrue(analyst.spec_is_usable(rendered.spec))
                self.assertLessEqual(len(rendered.builder), plan_mod.MAX_BRIEF_CHARS)
                self.assertLessEqual(len(rendered.tester), plan_mod.MAX_BRIEF_CHARS)
                self.assertLessEqual(
                    len(plan_mod.combined_brief(rendered.spec, rendered.plan)),
                    plan_mod.MAX_COMBINED_BRIEF_CHARS,
                )

    def test_a_computed_value_is_never_also_a_stored_field(self):
        # The whole point of the primitive: the form must not ask the user to
        # type the answer the app is supposed to work out.
        for name in ("hard-derived.json", "hard-bulk.json", "hard-currency.json"):
            with self.subTest(name):
                rendered = Rendered(name)
                names = [field["name"] for field in rendered.spec["fields"]]
                for entry in rendered.spec["derived"]:
                    self.assertNotIn(entry["name"], names)
                    self.assertTrue(entry["rule"])


class ComputedFromTwoDatesTest(unittest.TestCase):
    """A value the app works out from two date fields."""

    def setUp(self) -> None:
        self.r = Rendered("hard-derived.json")

    def test_the_computation_reaches_the_builder_as_a_derived_entry(self):
        self.assertTrue(self.r.spec["derived"])
        outline = {entry["name"]: entry for entry in self.r.outline["derived"]}
        for entry in self.r.spec["derived"]:
            self.assertEqual(outline[entry["name"]]["compute"], entry["rule"])
            self.assertIn(entry["rule"], self.r.builder)

    def test_the_builder_gets_the_day_helpers_rather_than_date_arithmetic(self):
        self.assertIn(
            'import { daysBetween, daysUntil, daysSince, today } from "./lib/dates.js";',
            self.r.builder,
        )
        self.assertIn("Never subtract date strings", self.r.builder)

    def test_the_tester_is_told_where_the_value_shows_and_how_to_date_a_record(self):
        for entry in self.r.spec["derived"]:
            self.assertIn('Derived value "{0}"'.format(entry["label"]), self.r.tester)
            self.assertIn('expectRow(title, "{0} 40 {1}")'.format(entry["label"], entry["unit"]),
                          self.r.tester)
            self.assertIn("never in `addRecord`", self.r.tester)
        self.assertIn("const iso = (offsetDays: number)", self.r.tester)
        self.assertIn("iso(-3)", self.r.tester)

    def test_a_unit_of_words_follows_the_number(self):
        # "days" is not a currency mark, so it reads after the value.
        self.assertIn('"60 days"', self.r.tester)


class AppliesToEveryRecordTest(unittest.TestCase):
    """An action whose effect is the whole collection, not one row."""

    def setUp(self) -> None:
        self.r = Rendered("hard-bulk.json")

    def test_the_bulk_action_leaves_the_row_actions_alone(self):
        bulk = [a for a in self.r.spec["actions"] if a["scope"] == "all"]
        rows = [a for a in self.r.spec["actions"] if a["scope"] == "row"]
        self.assertTrue(bulk and rows)
        self.assertEqual([a["label"] for a in self.r.outline["bulkActions"]],
                         [a["label"] for a in bulk])
        self.assertEqual([a["label"] for a in self.r.outline["actions"]],
                         [a["label"] for a in rows])

    def test_the_builder_is_told_a_row_action_would_change_one_record(self):
        self.assertIn("applies to EVERY record at once", self.r.builder)
        self.assertIn("changes only the row the user clicked", self.r.builder)

    def test_the_tester_is_given_the_helper_that_can_reach_the_button(self):
        for action in self.r.outline["bulkActions"]:
            self.assertIn('await runBulkAction(user, "{0}")'.format(action["label"]),
                          self.r.tester)
            self.assertIn('"{0} applied to N records"'.format(action["label"]), self.r.tester)
        self.assertIn("runBulkAction", plan_mod.TEST_HELPERS)

    def test_an_instant_action_keeps_no_dialog_it_was_never_given(self):
        self.assertFalse([a for a in self.r.outline["actions"] if "input" in a])
        self.assertNotIn('"input"', self.r.builder)


class CurrencyUnitTest(unittest.TestCase):
    """A money value, on a field, on a computed value and on a stat tile."""

    def setUp(self) -> None:
        self.r = Rendered("hard-currency.json")

    def test_the_unit_travels_on_every_kind_of_number(self):
        units = [field.get("unit") for field in self.r.outline["fields"]]
        self.assertIn("£", units)
        self.assertIn("£", [entry.get("unit") for entry in self.r.outline["derived"]])
        self.assertIn("£", [stat.get("unit") for stat in self.r.outline["summary"]])

    def test_both_briefs_agree_on_which_side_of_the_number_it_goes(self):
        self.assertIn("currency symbol prints before the value (`£40`)", self.r.builder)
        self.assertIn('a currency symbol comes before the value with no space ("£40")',
                      self.r.tester)
        self.assertIn('"40 pts"', self.r.tester)

    def test_the_tester_is_shown_the_row_and_the_tile_as_the_app_prints_them(self):
        self.assertIn('the row reads "Value £40"', self.r.tester)
        self.assertIn('expectRow(title, "Value £40")', self.r.tester)
        self.assertIn('"£60"', self.r.tester)


class NoShapeNoMachineryTest(unittest.TestCase):
    """A spec without these shapes is rendered exactly as it was before.

    This is what keeps the three mechanisms from becoming three more things
    every Builder has to read: each one is emitted from the spec's shape, so a
    spec that has none of them pays nothing for them.
    """

    def setUp(self) -> None:
        self.spec = load("spec-books.json")
        self.plan = plan_mod.derive_plan(self.spec)

    def test_neither_key_nor_rule_appears(self):
        outline = plan_mod._config_outline(self.spec, self.plan)
        builder = plan_mod.builder_brief(self.spec, self.plan)
        tester = plan_mod.tester_brief(self.spec, self.plan)
        self.assertNotIn("derived", outline)
        self.assertNotIn("bulkActions", outline)
        self.assertNotIn("./lib/dates.js", builder)
        self.assertNotIn("Derived value", tester)
        self.assertNotIn("runBulkAction(user", tester)
        self.assertNotIn("const iso", tester)

    def test_the_two_rules_that_are_worth_their_tokens_everywhere_stay(self):
        # An invented dialog and a misplaced unit cost a gate each, and neither
        # failure needs a special shape in the spec to happen again.
        builder = plan_mod.builder_brief(self.spec, self.plan)
        tester = plan_mod.tester_brief(self.spec, self.plan)
        self.assertIn("Never add `input` or `confirm` to an action unless it appears", builder)
        self.assertIn("currency symbol prints before the value", builder)
        self.assertIn('A boolean value reads "Yes" or "No"', tester)
        self.assertIn("Never call it for a record you just added", tester)


if __name__ == "__main__":
    unittest.main()
