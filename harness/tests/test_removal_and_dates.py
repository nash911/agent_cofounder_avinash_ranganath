"""Two shapes the last holdout round showed the briefs could not express.

1. An action that DELETES records. A patch names the fields that change, and a
   deletion changes none -- so "clear the freezer" had no spelling. Measured
   2026-09-04: the Builder invented a ``_deleted`` field (tsc), then zeroed a
   count instead, and two repair rounds went on it. Now an ``apply`` may return
   ``null``, and every brief says so wherever an effect reads as a removal.

2. A date only an action sets. "Last watered" was ``in_form=false``, so no test
   could ever give a plant a PAST date, and every rule that counted days since
   it was untestable; the Tester guessed and six tests failed the same way three
   times. Now every date is on the form and starts at today, and both briefs
   say what a day count on a fresh record reads.

The assertions never name a domain: they use neutral specs and walk the
rendered briefs for the wording each agent must have seen.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from harness import analyst, plan as plan_mod
from harness.specstrings import _removes, _visible_strings


def _spec(**overrides: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        "app_name": "Things", "noun": "thing", "noun_plural": "things",
        "storage_key": "things.v1", "title_field": "name",
        "fields": [
            {"name": "name", "label": "Name", "kind": "text", "required": True},
            {"name": "count", "label": "Count", "kind": "number", "unit": "left"},
            {"name": "seen", "label": "Last seen", "kind": "date", "in_form": False},
        ],
        "filters": [{"kind": "state", "id": "old", "label": "Old",
                     "rule": "days since Last seen > 7", "empty_text": "Nothing old"}],
        "actions": [],
        "journeys": [{"title": "adds a thing", "kind": "explicit", "steps": "add", "expect": "row"}],
    }
    raw.update(overrides)
    return analyst.normalize_spec(raw)


def _field(spec: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(f for f in spec["fields"] if f["name"] == name)


REMOVING = [
    {"id": "clear", "label": "Clear everything", "scope": "all", "effect": "delete all rows",
     "confirm_text": "Remove every thing?"},
    {"id": "eat", "label": "Eaten", "scope": "row", "effect": "delete the row", "toast": "Gone"},
]
PATCHING = [
    {"id": "reset", "label": "Reset all", "scope": "all", "effect": "set Count to 0"},
    {"id": "clearName", "label": "Anonymise", "scope": "row", "effect": "clear Name"},
]


class RemovalWordingTest(unittest.TestCase):
    def test_removal_is_recognised_by_its_verb_not_its_domain(self):
        for effect in ("delete all rows", "delete the row", "remove every sold item",
                       "removes the record from the list", "discard it", "drop the row",
                       "clear the list", "clear everything", "Clear all"):
            self.assertTrue(_removes(effect), effect)

    def test_changing_a_field_is_never_a_removal(self):
        for effect in ("clear borrower", "set Count to 0", "clear Last seen",
                       "decrease quantity by 1", "set holder to the input", "",
                       "remove the borrower", "remove one from Count", "drop the price to 0",
                       "remove Sam from holder", "discard the note text"):
            self.assertFalse(_removes(effect), effect)

    def test_the_outline_tells_the_builder_to_return_null(self):
        spec = _spec(actions=REMOVING)
        outline = plan_mod._config_outline(spec, plan_mod.derive_plan(spec))
        for entry in outline["actions"] + outline["bulkActions"]:
            self.assertIn("return null", entry["apply"], entry)
        spec = _spec(actions=PATCHING)
        outline = plan_mod._config_outline(spec, plan_mod.derive_plan(spec))
        for entry in outline["actions"] + outline["bulkActions"]:
            self.assertNotIn("null", entry["apply"], entry)

    def test_the_builder_rule_appears_only_when_something_removes(self):
        spec = _spec(actions=REMOVING)
        brief = plan_mod.builder_brief(spec, plan_mod.derive_plan(spec))
        self.assertIn("`apply: () => null`", brief)
        self.assertIn("_deleted", brief)
        spec = _spec(actions=PATCHING)
        brief = plan_mod.builder_brief(spec, plan_mod.derive_plan(spec))
        self.assertNotIn("_deleted", brief)

    def test_the_tester_asserts_absence_after_a_removal(self):
        spec = _spec(actions=REMOVING)
        sheet = _visible_strings(spec, plan_mod.derive_plan(spec))
        bulk = next(line for line in sheet.splitlines() if "Clear everything" in line)
        self.assertIn("GONE", bulk)
        self.assertIn("expectNoRow", bulk)
        self.assertIn("N the number removed", bulk)
        rowline = next(line for line in sheet.splitlines() if "Row button \"Eaten\"" in line)
        self.assertIn("expectNoRow", rowline)
        spec = _spec(actions=PATCHING)
        sheet = _visible_strings(spec, plan_mod.derive_plan(spec))
        bulk = next(line for line in sheet.splitlines() if "Reset all" in line)
        self.assertNotIn("GONE", bulk)
        self.assertIn("every row has changed", bulk)


class DatesOnTheFormTest(unittest.TestCase):
    def test_a_date_is_on_the_form_even_when_the_analyst_hid_it(self):
        spec = _spec()
        self.assertTrue(_field(spec, "seen")["in_form"])

    def test_a_non_date_hidden_field_stays_hidden(self):
        spec = _spec(fields=[
            {"name": "name", "label": "Name", "kind": "text", "required": True},
            {"name": "holder", "label": "Held by", "kind": "text", "in_form": False},
        ])
        self.assertFalse(_field(spec, "holder")["in_form"])

    def test_the_outline_dates_a_new_record_today(self):
        spec = _spec()
        outline = plan_mod._config_outline(spec, plan_mod.derive_plan(spec))
        seen = next(f for f in outline["fields"] if f["name"] == "seen")
        self.assertEqual(seen.get("initial"), "today")
        self.assertNotIn("inForm", seen)

    def test_both_briefs_agree_on_what_a_fresh_record_reads(self):
        spec = _spec()
        plan = plan_mod.derive_plan(spec)
        builder = plan_mod.builder_brief(spec, plan)
        self.assertIn("`initial: \"today\"`", builder)
        self.assertIn("never treat an empty date", builder)
        sheet = _visible_strings(spec, plan)
        line = next(l for l in sheet.splitlines() if l.startswith("- Date fields (Last seen)"))
        self.assertIn("iso(0)", line)
        self.assertIn("0 days old", line)
        self.assertIn("unless its rule already says so at 0", line)
        self.assertIn("\"Last seen\": iso(-9)", line)
        self.assertNotIn("Never on the form", sheet)

    def test_no_date_line_without_a_date_field(self):
        spec = _spec(fields=[{"name": "name", "label": "Name", "kind": "text", "required": True}],
                     filters=[])
        sheet = _visible_strings(spec, plan_mod.derive_plan(spec))
        self.assertNotIn("Date fields (", sheet)


class FilterChipsTest(unittest.TestCase):
    def test_a_field_filter_carries_its_empty_text_into_the_outline(self):
        spec = _spec(fields=[
            {"name": "name", "label": "Name", "kind": "text", "required": True},
            {"name": "room", "label": "Room", "kind": "text"},
            {"name": "kind", "label": "Kind", "kind": "select", "options": ["Big", "Small"]},
        ], filters=[
            {"kind": "field", "field": "room", "label": "All", "empty_text": "No things in this room."},
            {"kind": "field", "field": "kind", "label": "All kinds"},
        ])
        outline = plan_mod._config_outline(spec, plan_mod.derive_plan(spec))
        room, kind = outline["filters"]
        self.assertEqual(room.get("emptyText"), "No things in this room.")
        self.assertNotIn("emptyText", kind)
        sheet = _visible_strings(spec, plan_mod.derive_plan(spec))
        room_line = next(l for l in sheet.splitlines() if l.startswith("- Filter chips for \"Room\""))
        self.assertIn("a value no row has gets no chip", room_line)
        self.assertIn("\"Nothing matches this view\" and \"No things in this room.\"", room_line)
        kind_line = next(l for l in sheet.splitlines() if l.startswith("- Filter chips for \"Kind\""))
        self.assertIn("one chip per option above", kind_line)
        self.assertIn("\"No things in this view.\"", kind_line)

    def test_a_state_filter_names_its_empty_text(self):
        spec = _spec()
        sheet = _visible_strings(spec, plan_mod.derive_plan(spec))
        line = next(l for l in sheet.splitlines() if l.startswith("- Filter chip \"Old\""))
        self.assertIn("\"Nothing matches this view\" and \"Nothing old\"", line)


class TextFieldFilterTest(unittest.TestCase):
    def test_a_field_filter_over_a_text_field_survives_with_its_empty_text(self):
        spec = _spec(fields=[
            {"name": "name", "label": "Name", "kind": "text", "required": True},
            {"name": "room", "label": "Room", "kind": "text"},
            {"name": "count", "label": "Count", "kind": "number"},
        ], filters=[
            {"kind": "field", "field": "room", "label": "All rooms", "empty_text": "No things here."},
            {"kind": "field", "field": "count", "label": "All"},
        ])
        self.assertEqual(
            [(f["kind"], f["field"], f["empty_text"]) for f in spec["filters"]],
            [("field", "room", "No things here.")],
        )


class AbsentWordRuleTest(unittest.TestCase):
    def test_every_cheat_sheet_forbids_asserting_a_word_absent(self):
        for actions in (REMOVING, PATCHING, []):
            spec = _spec(actions=actions)
            sheet = _visible_strings(spec, plan_mod.derive_plan(spec))
            self.assertIn("Never assert that a word is ABSENT", sheet)
            self.assertIn("`not.toContain`", sheet)


if __name__ == "__main__":
    unittest.main()
