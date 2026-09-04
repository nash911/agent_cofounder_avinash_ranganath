"""Unit tests for :mod:`harness.plan` -- the Architect and the mission briefs.

The briefs are the contract between two agents that never see each other's
file, so the tests are mostly about *strings*: every label, option, badge text
and journey title in the spec has to reach both briefs verbatim, and neither
brief may grow past the ~2,000-token budget that keeps the shared prefix worth
caching. ``harness/tests/fixtures/spec-books.json`` is a real Analyst output
(Berget, GLM-5.2, 2026-09-03) so the assertions are about real model prose,
not about prose invented to pass them.
"""

from __future__ import annotations

import copy
import json
import pathlib
import unittest
from typing import Any, Dict, List

from harness import analyst, plan as plan_mod

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def books_spec() -> Dict[str, Any]:
    """The probe's spec, normalised -- exactly what ``spec.json`` holds."""
    return analyst.normalize_spec(json.loads((FIXTURES / "spec-books.json").read_text(encoding="utf-8")))


def pantry_spec() -> Dict[str, Any]:
    """A second shape: a constant, a number field, a field filter, a confirm action."""
    return analyst.normalize_spec({
        "app_name": "Pantry Watch", "tagline": "Never run out", "summary": "Track supplies.",
        "noun": "item", "noun_plural": "items",
        "fields": [
            {"name": "name", "label": "Name", "kind": "text", "required": True, "options": [],
             "unit": "", "in_form": True, "message": "Name is required."},
            {"name": "category", "label": "Category", "kind": "select", "required": False,
             "options": ["Dry Goods", "Fresh"], "unit": "", "in_form": True, "message": ""},
            {"name": "quantity", "label": "Quantity", "kind": "number", "required": True,
             "options": [], "unit": "left", "in_form": True, "message": "Quantity is required."},
        ],
        "title_field": "name", "subtitle_fields": ["category"], "meta_fields": ["quantity"],
        "constants": [{"name": "LOW_THRESHOLD", "value": 2,
                       "comment": "'running low' is 2 or fewer"}],
        "filters": [
            {"kind": "field", "field": "category", "id": "", "label": "All categories",
             "rule": "", "empty_text": ""},
            {"kind": "state", "field": "", "id": "low", "label": "Running low",
             "rule": "quantity is at most LOW_THRESHOLD", "empty_text": "Nothing is running low."},
        ],
        "badges": [{"id": "low", "rule": "quantity is at most LOW_THRESHOLD", "tone": "alert",
                    "text": "Running low - {quantity} left"}],
        "stats": [{"id": "low", "label": "Running low", "rule": "count of rows where quantity is at most LOW_THRESHOLD",
                   "emphasis": True}],
        "actions": [{"id": "useOne", "label": "Use one", "available_rule": "quantity is above 0",
                     "effect": "decrease quantity by 1", "input_label": "", "input_required": False,
                     "confirm_text": "Use one now?", "toast": "Used one {name}."}],
        "journeys": [{"title": "adds an item", "kind": "explicit", "steps": "fill the form",
                      "expect": "the item is listed"}],
        "omitted_patterns": [], "assumptions": ["Low means 2 or fewer"],
    })


def computed_spec() -> Dict[str, Any]:
    """A third shape: a computed value, a currency unit, and a bulk action.

    None of the three can be expressed by a field, a stat rule or a row action,
    which is why each has its own key; this spec is the smallest one that
    carries all three at once.
    """
    return analyst.normalize_spec({
        "app_name": "Value Board", "tagline": "What each record is worth",
        "summary": "Track records and their value.", "noun": "record", "noun_plural": "records",
        "fields": [
            {"name": "name", "label": "Name", "kind": "text", "required": True, "options": [],
             "unit": "", "in_form": True, "message": "Name is required."},
            {"name": "checked", "label": "Last Checked", "kind": "date", "required": False,
             "options": [], "unit": "", "in_form": True, "message": ""},
            {"name": "quantity", "label": "Quantity", "kind": "number", "required": True,
             "options": [], "unit": "", "in_form": True, "message": "Quantity is required."},
            {"name": "price", "label": "Price", "kind": "number", "required": True,
             "integer": False, "options": [], "unit": "£", "in_form": True,
             "message": "Price is required."},
        ],
        "title_field": "name", "subtitle_fields": ["price"], "meta_fields": ["quantity"],
        "derived": [
            {"name": "value", "label": "Value", "rule": "Quantity times Price", "unit": "£"},
            {"name": "age", "label": "Age", "rule": "days since Last Checked", "unit": "days"},
        ],
        "constants": [], "filters": [], "badges": [],
        "stats": [{"id": "total", "label": "Total value", "rule": "sum of Value", "unit": "£",
                   "emphasis": True},
                  {"id": "count", "label": "Records", "rule": "count of all rows", "unit": "",
                   "emphasis": False}],
        "actions": [
            {"id": "check", "label": "Check", "scope": "row", "available_rule": "",
             "effect": "set Last Checked to today", "input_label": "", "input_required": False,
             "confirm_text": "", "toast": "Checked"},
            {"id": "clearAll", "label": "Clear all", "scope": "all", "available_rule": "",
             "effect": "clear Last Checked", "input_label": "", "input_required": False,
             "confirm_text": "Clear every record?", "toast": ""},
        ],
        "journeys": [{"title": "adds a record", "kind": "explicit", "steps": "fill the form",
                      "expect": "the record is listed"}],
        "omitted_patterns": [], "assumptions": [],
    })


def huge_spec(journeys: int = 26) -> Dict[str, Any]:
    """A spec big enough to overrun the brief budget if nothing trimmed it."""
    spec = books_spec()
    spec["journeys"] = [
        {"title": "journey number {0} that the user walks through".format(index),
         "kind": "implied", "steps": "step detail " * 30, "expect": "expected text " * 30}
        for index in range(journeys)
    ]
    return spec


class DerivePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = books_spec()
        self.plan = plan_mod.derive_plan(self.spec)

    def test_the_two_files_and_the_untouchables(self):
        self.assertEqual(self.plan["files"], {"config": "src/app-config.ts",
                                              "tests": "src/journeys.test.tsx"})
        for path in ("src/lib/", "src/components/", "src/App.tsx", "src/test/", "src/styles.css",
                     "vitest.config.ts", "package.json"):
            self.assertIn(path, self.plan["leave_alone"])

    def test_one_test_per_journey_with_the_title_kept_verbatim(self):
        titles = [test["title"] for test in self.plan["tests"]]
        self.assertEqual(titles, [journey["title"] for journey in self.spec["journeys"]])
        self.assertEqual(len(titles), 10)
        self.assertEqual(self.plan["tests"][0]["kind"], "explicit")
        self.assertIn("Open app", self.plan["tests"][0]["steps"])

    def test_implemented_features_come_from_the_journeys_and_are_capped(self):
        features = self.plan["implemented_features"]
        self.assertLessEqual(len(features), plan_mod.MAX_FEATURES)
        self.assertIn("Add a book", features)
        self.assertIn("Lend a book", features)
        self.assertEqual(len(features), len(set(features)))
        self.assertTrue(all(feature[:1] == feature[:1].upper() for feature in features))
        # 26 journeys must not become a 26-line feature list in the report.
        self.assertLessEqual(
            len(plan_mod.derive_plan(huge_spec())["implemented_features"]), plan_mod.MAX_FEATURES
        )

    def test_an_action_no_journey_mentions_still_becomes_a_feature(self):
        spec = books_spec()
        spec["journeys"] = [{"title": "adds a book", "kind": "explicit", "steps": "", "expect": ""}]
        features = plan_mod.derive_plan(spec)["implemented_features"]
        self.assertIn("Lend action", features)
        self.assertIn("Return action", features)

    def test_assumptions_absorb_the_omitted_patterns_and_the_constants(self):
        assumptions = self.plan["assumptions"]
        self.assertIn("A Home filter is implied alongside the Out filter", assumptions)
        self.assertIn("Omitted Multiple users or sharing: Stated as just me on my own computer",
                      assumptions)
        with_constant = plan_mod.derive_plan(pantry_spec())["assumptions"]
        self.assertIn("LOW_THRESHOLD = 2: 'running low' is 2 or fewer", with_constant)

    def test_a_capability_the_scaffold_always_renders_is_never_claimed_as_omitted(self):
        # The scaffold draws a search box unconditionally and `_config_outline`
        # always sorts by the title field, so "Omitted Sorting" would contradict
        # the app a grader can open. The probe's spec omits both.
        assumptions = self.plan["assumptions"]
        self.assertNotIn("Omitted Sorting: Not mentioned", assumptions)
        self.assertFalse([line for line in assumptions if line.startswith("Omitted Search")])
        self.assertIn(
            "Search and alphabetical sorting are provided by the scaffold and are always "
            "available.",
            assumptions,
        )

    def test_the_number_field_decisions_are_recorded_as_assumptions(self):
        # `_field_outline` invents `min: 0`, `initial: 0` and (by default)
        # `integer: true`; the scaffold enforces all three at validation time,
        # so a decision the idea never made is written down.
        assumptions = plan_mod.derive_plan(pantry_spec())["assumptions"]
        self.assertIn("Quantity is a whole number, starts at 0 and cannot be negative.",
                      assumptions)
        spec = pantry_spec()
        for field in spec["fields"]:
            if field["name"] == "quantity":
                field["integer"] = False
        relaxed = plan_mod.derive_plan(spec)["assumptions"]
        self.assertIn("Quantity is allowed decimals, starts at 0 and cannot be negative.",
                      relaxed)

    def test_labels_carry_every_visible_string_by_category(self):
        labels = self.plan["labels"]
        self.assertEqual(labels["fields"], {"title": "Title", "author": "Author",
                                            "type": "Type", "borrower": "Borrower"})
        self.assertEqual(labels["options"], {"type": ["Novel", "Cookbook", "Reference"]})
        self.assertEqual(labels["filters"], ["Out", "Home"])
        self.assertEqual(labels["badges"], ["Out: {borrower}"])
        self.assertEqual(labels["stats"], ["Lent out"])
        self.assertEqual(labels["actions"], ["Lend", "Return"])
        self.assertEqual(labels["inputs"], ["Borrower name"])
        self.assertEqual(labels["messages"], ["Title is required"])
        self.assertEqual(labels["copy"], {
            "title": "Home Library", "tagline": "Track books and who has them", "noun": "book",
            "nounPlural": "books", "addLabel": "Add book", "emptyTitle": "No books yet",
            "emptyBody": "Add your first book with the form.",
        })

    def test_derive_plan_does_not_mutate_the_spec_and_survives_nonsense(self):
        before = copy.deepcopy(self.spec)
        plan_mod.derive_plan(self.spec)
        self.assertEqual(self.spec, before)
        for value in (None, {}, {"fields": "no", "journeys": 3}):
            empty = plan_mod.derive_plan(value)
            self.assertEqual(empty["tests"], [])
            self.assertEqual(empty["implemented_features"], [])
            self.assertEqual(empty["labels"]["fields"], {})


class BuilderBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = books_spec()
        self.brief = plan_mod.builder_brief(self.spec, plan_mod.derive_plan(self.spec))

    def test_it_fits_the_budget(self):
        # ~2,000 tokens is the cap; the public idea measured ~750, plus the two
        # rendering rules every config needs (no invented dialog, unit before or
        # after the number) -- both bought with a gate failure apiece.
        self.assertLessEqual(len(self.brief), plan_mod.MAX_BRIEF_CHARS)
        self.assertLess(len(self.brief), 4500)

    def test_it_names_the_one_file_to_write(self):
        self.assertIn("## Mission: write `src/app-config.ts`", self.brief)
        self.assertNotIn("src/journeys.test.tsx", self.brief)

    def test_every_field_label_option_and_action_label_is_in_it(self):
        for field in self.spec["fields"]:
            self.assertIn('"{0}"'.format(field["label"]), self.brief)
            self.assertIn('"{0}"'.format(field["name"]), self.brief)
            for option in field["options"]:
                self.assertIn('"{0}"'.format(option), self.brief)
        for action in self.spec["actions"]:
            self.assertIn('"{0}"'.format(action["label"]), self.brief)
            self.assertIn(action["effect"], self.brief)
        for badge in self.spec["badges"]:
            self.assertIn(badge["text"], self.brief)
        for entry in self.spec["filters"] + self.spec["stats"]:
            self.assertIn(entry["label"], self.brief)

    def test_it_carries_the_storage_key_copy_title_field_and_sort(self):
        self.assertIn('"storageKey": "books.v1"', self.brief)
        self.assertIn('"titleField": "title"', self.brief)
        self.assertIn('"direction": "asc"', self.brief)
        self.assertIn("Add book", self.brief)
        self.assertIn("No books yet", self.brief)
        self.assertIn('"inForm": false', self.brief)  # borrower is action-set only
        self.assertIn('"message": "Title is required"', self.brief)

    def test_every_constant_reaches_the_brief_with_its_comment(self):
        spec = pantry_spec()
        brief = plan_mod.builder_brief(spec, plan_mod.derive_plan(spec))
        for constant in spec["constants"]:
            self.assertIn(constant["name"], brief)
            self.assertIn(str(constant["value"]), brief)
            self.assertIn(constant["comment"], brief)
        self.assertIn("exported `const`", brief)
        # The number field's unit and the field filter's chip label survive too.
        self.assertIn('"unit": "left"', brief)
        self.assertIn("All categories", brief)
        self.assertIn("Use one", brief)
        self.assertIn("Use one now?", brief)

    def test_the_rules_that_keep_the_mission_to_one_write(self):
        for rule in ("One `write` of the whole file", "no command", "Never annotate",
                     "never hoist `fields`", "row.<name>", "End your turn immediately"):
            self.assertIn(rule, self.brief)

    def test_a_stat_rule_written_as_a_bare_predicate_becomes_a_count(self):
        # The model often writes "borrower is not empty" where the schema asks
        # for "count of rows where ..."; a boolean in a stat tile is a type error.
        self.assertIn('"compute": "count of rows where borrower is not empty"', self.brief)


class ComputedBulkAndUnitTest(unittest.TestCase):
    """The three primitives a one-record config had no key for.

    Each one is emitted only when the spec has that shape, so a spec without
    them reads exactly as it did before -- the mechanism is driven by the
    shape, never by the words a particular idea happens to use.
    """

    def setUp(self) -> None:
        self.spec = computed_spec()
        self.plan = plan_mod.derive_plan(self.spec)
        self.outline = plan_mod._config_outline(self.spec, self.plan)
        self.builder = plan_mod.builder_brief(self.spec, self.plan)
        self.tester = plan_mod.tester_brief(self.spec, self.plan)

    def test_a_computed_value_reaches_the_outline_as_its_own_entry(self):
        self.assertEqual(self.outline["derived"], [
            {"name": "value", "label": "Value", "compute": "Quantity times Price", "unit": "£"},
            {"name": "age", "label": "Age", "compute": "days since Last Checked", "unit": "days"},
        ])
        # And never as a field the form would ask the user to fill.
        self.assertNotIn("value", [field["name"] for field in self.outline["fields"]])

    def test_a_scope_all_action_is_a_bulk_action_and_nothing_else(self):
        self.assertEqual(self.outline["bulkActions"], [
            {"id": "clearAll", "label": "Clear all", "confirm": "Clear every record?",
             "apply": "clear Last Checked"},
        ])
        self.assertEqual([a["label"] for a in self.outline["actions"]], ["Check"])

    def test_a_stat_carries_its_unit(self):
        by_label = {stat["label"]: stat for stat in self.outline["summary"]}
        self.assertEqual(by_label["Total value"]["unit"], "£")
        self.assertNotIn("unit", by_label["Records"])

    def test_the_builder_is_told_how_to_compute_and_where_to_put_it(self):
        self.assertIn("compute: (row) => ...", self.builder)
        self.assertIn('import { daysBetween, daysUntil, daysSince, today } from "./lib/dates.js";',
                      self.builder)
        self.assertIn("applies to EVERY record at once", self.builder)
        self.assertIn("`bulkActions` array", self.builder)

    def test_the_builder_is_told_the_unit_rule_and_never_to_invent_a_dialog(self):
        self.assertIn("Never add `input` or `confirm` to an action unless it appears", self.builder)
        self.assertIn("currency symbol prints before the value (`£40`)", self.builder)
        self.assertIn("any other unit after it (`40 pts`)", self.builder)

    def test_an_action_with_no_input_label_never_gets_an_input_in_the_outline(self):
        # Measured 2026-09-04: an instant action arrived with a dialog and a
        # confirmation the spec never asked for, and every test that clicked
        # the button failed.
        self.assertFalse([a for a in self.outline["actions"] if "input" in a])
        self.assertNotIn('"input"', self.builder)
        self.assertNotIn('"confirm"', json.dumps(self.outline["actions"]))

    def test_the_tester_is_given_the_bulk_helper_and_the_rendered_examples(self):
        self.assertIn('await runBulkAction(user, "Clear all")', self.tester)
        self.assertIn("runBulkAction", plan_mod.TEST_HELPERS)
        self.assertIn('the row reads "Value £40"', self.tester)
        self.assertIn('expectRow(title, "Value £40")', self.tester)
        self.assertIn('"£60"', self.tester)
        self.assertIn('"40 pts"', self.tester)
        self.assertIn('reads "Yes" or "No"', self.tester)
        self.assertIn("never call it for a record you just added", self.tester.lower())
        self.assertIn("const iso = (offsetDays: number)", self.tester)

    def test_a_spec_without_these_shapes_is_rendered_exactly_as_before(self):
        spec = books_spec()
        outline = plan_mod._config_outline(spec, plan_mod.derive_plan(spec))
        builder = plan_mod.builder_brief(spec, plan_mod.derive_plan(spec))
        tester = plan_mod.tester_brief(spec, plan_mod.derive_plan(spec))
        self.assertNotIn("derived", outline)
        self.assertNotIn("bulkActions", outline)
        self.assertNotIn("./lib/dates.js", builder)
        self.assertNotIn("bulkActions", builder)
        self.assertNotIn("runBulkAction(user", tester)
        self.assertNotIn("const iso", tester)

    def test_both_briefs_still_fit_their_budget_with_all_three_shapes(self):
        self.assertLessEqual(len(self.builder), plan_mod.MAX_BRIEF_CHARS)
        self.assertLessEqual(len(self.tester), plan_mod.MAX_BRIEF_CHARS)
        self.assertLessEqual(len(plan_mod.combined_brief(self.spec, self.plan)),
                             plan_mod.MAX_COMBINED_BRIEF_CHARS)


class TesterBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = books_spec()
        self.plan = plan_mod.derive_plan(self.spec)
        self.brief = plan_mod.tester_brief(self.spec, self.plan)

    def test_it_fits_the_budget_and_names_the_one_file(self):
        self.assertLessEqual(len(self.brief), plan_mod.MAX_BRIEF_CHARS)
        self.assertIn("## Mission: write `src/journeys.test.tsx`", self.brief)

    def test_every_journey_title_appears_verbatim_and_in_order(self):
        position = -1
        for journey in self.spec["journeys"]:
            found = self.brief.find('"{0}"'.format(journey["title"]))
            self.assertGreater(found, position, journey["title"])
            position = found

    def test_every_visible_string_is_in_it(self):
        expected: List[str] = ["Home Library", "Add book", "No books yet",
                               "Add your first book with the form.", "Title is required"]
        for field in self.spec["fields"]:
            expected.append(field["label"])
            expected.extend(field["options"])
        for entry in self.spec["filters"] + self.spec["stats"]:
            expected.append(entry["label"])
        for badge in self.spec["badges"]:
            expected.append(badge["text"])
        for action in self.spec["actions"]:
            expected.extend([action["label"], action["input_label"], action["toast"]])
        for value in [item for item in expected if item]:
            self.assertIn(value, self.brief, value)

    def test_it_pins_the_helpers_the_import_path_and_the_size_limit(self):
        for helper in ("renderApp", "addRecord", "editRecord", "removeRecord", "runAction",
                       "chooseFilter", "search", "reload", "corruptStorage", "rowTitles",
                       "expectRow", "expectNoRow", "stat", "confirmDialog"):
            self.assertIn(helper, self.brief)
        self.assertIn('"./test/helpers.js"', self.brief)
        self.assertIn("150 lines", self.brief)
        self.assertIn("Never import or render `App`", self.brief)
        self.assertIn("do not run any command", self.brief)
        self.assertIn("End your turn immediately", self.brief)

    def test_a_field_filter_tells_the_tester_which_chips_exist(self):
        spec = pantry_spec()
        brief = plan_mod.tester_brief(spec, plan_mod.derive_plan(spec))
        self.assertIn("All categories", brief)
        self.assertIn("Dry Goods", brief)
        self.assertIn("Running low - {quantity} left", brief)
        self.assertIn("Use one now?", brief)
        self.assertIn("Quantity is required.", brief)

    def test_a_brief_just_over_budget_keeps_the_expectations(self):
        """The steps are the first thing to go -- the assertion is the last.

        Both halves of a journey line share one line, so a single line-wide
        cut took the expectation with the walk: a brief one character over
        budget arrived with no assertion in it at all. The Tester writes its
        `expect(...)` from the expectation, so it survives the first cut.
        """
        spec = huge_spec(journeys=6)
        # Grow the spec one journey at a time until the first cut fires. The
        # brief is capped, so the loop watches for the cut, not the length.
        for _ in range(40):
            brief = plan_mod.tester_brief(spec, plan_mod.derive_plan(spec))
            if " — do: " not in brief:
                break
            spec["journeys"].append(dict(
                spec["journeys"][0],
                title="journey number {0} that the user walks through".format(len(spec["journeys"])),
            ))
        else:  # pragma: no cover - the cut always fires long before 40
            self.fail("the brief never overran its budget")
        self.assertLessEqual(len(brief), plan_mod.MAX_BRIEF_CHARS)
        self.assertIn(" — expect: ", brief)
        for journey in spec["journeys"]:
            self.assertIn(journey["title"], brief)

    def test_a_days_rule_reaches_both_briefs_even_with_no_date_field(self):
        """One test for "this idea counts days", so the two briefs agree.

        The Analyst sometimes writes a date as a text field and keeps the
        calendar in the rule ("within the next N days"). The Tester already
        recognised that shape and got the ISO idiom; the Builder did not, and
        hand-rolled the arithmetic the helpers exist to prevent.
        """
        spec = books_spec()
        spec["fields"].append({
            "name": "seen", "label": "Seen", "kind": "text", "required": False,
            "options": [], "integer": False, "unit": "", "in_form": True, "message": "",
        })
        spec["filters"].append({
            "kind": "state", "field": "", "id": "recent", "label": "Recent",
            "rule": "rows where Seen is within the last 7 days", "empty_text": "None recent",
        })
        plan = plan_mod.derive_plan(spec)
        self.assertIn("./lib/dates.js", plan_mod.builder_brief(spec, plan))
        self.assertIn("const iso = (offsetDays", plan_mod.tester_brief(spec, plan))

    def test_an_oversized_spec_is_trimmed_but_keeps_every_title(self):
        spec = huge_spec()
        brief = plan_mod.tester_brief(spec, plan_mod.derive_plan(spec))
        self.assertLessEqual(len(brief), plan_mod.MAX_BRIEF_CHARS)
        for journey in spec["journeys"]:
            self.assertIn(journey["title"], brief)
        self.assertLessEqual(
            len(plan_mod.builder_brief(spec, plan_mod.derive_plan(spec))), plan_mod.MAX_BRIEF_CHARS
        )


class RepairBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = books_spec()
        self.plan = plan_mod.derive_plan(self.spec)

    def observation(self, **overrides: Any) -> Dict[str, Any]:
        observation = {
            "tsc_ran": True, "tsc_ok": True, "tsc_errors": [],
            "vitest": {"ran": True, "passed": 10, "failed": 0, "names": [], "failures": []},
            "build_ran": False, "build_ok": None, "build_tail": "",
            "files": {}, "over_limit": [], "coverage": {}, "signature": "abc", "green": True,
            "elapsed_s": 1.0,
        }
        observation.update(overrides)
        return observation

    def test_tsc_errors_are_quoted_verbatim_and_the_file_is_named(self):
        errors = ["src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
                  "src/app-config.ts(31,9): error TS2551: Property 'borower' does not exist."]
        brief = plan_mod.repair_brief(
            self.observation(tsc_ok=False, tsc_errors=errors), self.plan, self.spec, attempt=1
        )
        for line in errors:
            self.assertIn(line, brief)
        self.assertIn("`tsc --noEmit` failed", brief)
        self.assertIn("src/app-config.ts —", brief)
        self.assertIn("attempt 1 of 3", brief)
        self.assertIn("do not silence them", brief)
        self.assertLessEqual(len(brief), plan_mod.MAX_BRIEF_CHARS)

    def test_only_the_first_thirty_tsc_lines_travel(self):
        errors = ["src/app-config.ts({0},1): error TS2322: bad".format(n) for n in range(1, 61)]
        brief = plan_mod.repair_brief(
            self.observation(tsc_ok=False, tsc_errors=errors), self.plan, self.spec, attempt=2
        )
        self.assertIn(errors[29], brief)
        self.assertNotIn(errors[30], brief)

    def test_vitest_failures_carry_the_title_and_a_clipped_message(self):
        message = "Unable to find a label with the text of: Borrower name. " + "x" * 900
        observation = self.observation(green=False, vitest={
            "ran": True, "passed": 8, "failed": 1, "names": ["Add a book"],
            "failures": [{"name": "journeys > Lend a book", "message": message}],
        })
        brief = plan_mod.repair_brief(observation, self.plan, self.spec, attempt=2)
        self.assertIn("journeys > Lend a book", brief)
        self.assertIn("Unable to find a label with the text of: Borrower name.", brief)
        self.assertNotIn("x" * 601, brief)
        # A missing accessible name is a string mismatch, not a logic bug.
        self.assertIn("A string that differs between the two files", brief)
        self.assertIn("attempt 2 of 3", brief)
        self.assertLessEqual(len(brief), plan_mod.MAX_BRIEF_CHARS)

    def test_a_behavioural_failure_is_not_blamed_on_the_strings(self):
        observation = self.observation(green=False, vitest={
            "ran": True, "passed": 9, "failed": 1, "names": [],
            "failures": [{"name": "journeys > See how many are lent out",
                          "message": "expected 2 to be 1"}],
        })
        brief = plan_mod.repair_brief(observation, self.plan, self.spec, attempt=1)
        self.assertIn("The behaviour under test", brief)

    def test_an_over_limit_file_outranks_everything_else(self):
        brief = plan_mod.repair_brief(
            self.observation(green=False, over_limit=["src/journeys.test.tsx"], tsc_ok=False,
                             tsc_errors=["src/app-config.ts(1,1): error TS1005: ';' expected."]),
            self.plan, self.spec, attempt=3,
        )
        self.assertIn("over the 150-line limit", brief)
        self.assertIn("src/journeys.test.tsx", brief)
        self.assertNotIn("TS1005", brief)

    def test_a_failed_build_reports_its_tail(self):
        brief = plan_mod.repair_brief(
            self.observation(green=False, build_ran=True, build_ok=False,
                             build_tail="error during build:\nCould not resolve './missing.js'"),
            self.plan, self.spec, attempt=1,
        )
        self.assertIn("`vite build` failed", brief)
        self.assertIn("Could not resolve './missing.js'", brief)

    def test_the_hint_and_the_cap_are_rendered(self):
        brief = plan_mod.repair_brief(
            self.observation(green=False, tsc_ok=False, tsc_errors=["src/app-config.ts(1,1): error TS1005: x"]),
            self.plan, self.spec, attempt=2, hint="Add the missing `Home` filter chip.", cap=5,
        )
        self.assertIn("attempt 2 of 5", brief)
        self.assertIn("Add the missing `Home` filter chip.", brief)

    def test_it_always_restates_the_contract_and_the_procedure(self):
        brief = plan_mod.repair_brief(self.observation(green=False), self.plan, self.spec, attempt=1)
        self.assertIn("Out: {borrower}", brief)          # the spec's own strings
        self.assertIn("Lent out", brief)
        self.assertIn("smallest edit", brief)
        self.assertIn("Do not run any command", brief)
        self.assertIn("End your turn", brief)
        self.assertIn("src/lib/", brief)                 # the untouchables

    def test_nonsense_input_still_produces_a_brief(self):
        brief = plan_mod.repair_brief({}, {}, {}, attempt=1)
        self.assertIn("## Mission: repair the build", brief)
        self.assertLessEqual(len(brief), plan_mod.MAX_BRIEF_CHARS)


class RerunBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = books_spec()
        self.plan = plan_mod.derive_plan(self.spec)

    def test_a_builder_rerun_is_the_builder_mission_plus_the_reason(self):
        brief = plan_mod.rerun_brief("builder", self.plan, self.spec,
                                     "src/app-config.ts is unchanged from the seed")
        self.assertIn("src/app-config.ts is unchanged from the seed", brief)
        self.assertIn("## Mission: write `src/app-config.ts`", brief)
        self.assertLessEqual(len(brief), plan_mod.MAX_BRIEF_CHARS)

    def test_a_tester_rerun_asks_for_the_test_file(self):
        brief = plan_mod.rerun_brief("tester", self.plan, self.spec, "the file was never written")
        self.assertIn("## Mission: write `src/journeys.test.tsx`", brief)
        self.assertIn("the file was never written", brief)
        for journey in self.spec["journeys"]:
            self.assertIn(journey["title"], brief)


if __name__ == "__main__":
    unittest.main()


class CombinedBriefTest(unittest.TestCase):
    """``combined_brief``: both single-file briefs, one ordering rule, no contradiction."""

    def setUp(self):
        self.spec = analyst.normalize_spec(json.loads((FIXTURES / "spec-books.json").read_text(encoding="utf-8")))
        self.plan = plan_mod.derive_plan(self.spec)
        self.brief = plan_mod.combined_brief(self.spec, self.plan)

    def test_both_parts_in_order_with_one_closing_rule(self):
        part1 = self.brief.index("### Part 1 -- `src/app-config.ts`")
        part2 = self.brief.index("### Part 2 -- `src/journeys.test.tsx`")
        order = self.brief.index("### Order")
        self.assertLess(part1, part2)
        self.assertLess(part2, order)
        self.assertEqual(self.brief.count("Exactly two tool calls"), 1)
        self.assertNotIn("first and only tool call", self.brief)
        self.assertNotIn("another agent is writing it", self.brief)

    def test_it_keeps_every_string_the_single_briefs_carry(self):
        for needle in ('"storageKey"', "### Journeys, one `it` each", "### The exact strings the application shows"):
            self.assertIn(needle, self.brief)
        for journey in self.plan["tests"]:
            self.assertIn(journey["title"], self.brief)

    def test_it_fits_the_combined_cap(self):
        self.assertLessEqual(len(self.brief), plan_mod.MAX_COMBINED_BRIEF_CHARS)
        self.assertFalse(self.brief.endswith("..."))

