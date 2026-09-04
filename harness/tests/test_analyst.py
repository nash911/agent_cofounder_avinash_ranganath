"""Unit tests for :mod:`harness.analyst` -- schema, normalisation, one call.

Two halves. The first is pure: :func:`harness.analyst.normalize_spec` is where
every "the model wrote something the scaffold cannot compile" case is caught,
so each rule gets a test with the failure it prevents named in the docstring.
The second drives ``run_analyst`` against the scripted fake gateway on
``127.0.0.1`` (never a real model, never the network), and pins the contract
that matters most: the Analyst never blocks the build, and never leaves a
``spec.json`` behind that missions mode would then try to build from.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
from typing import Any, Dict
from unittest import mock

from harness import analyst, gateway, pirpc
from harness.tests import support
from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
#: The real Analyst output from the 2026-09-03 Berget probe (``scratchpad/p3/probe1``).
BOOKS_SPEC_PATH = FIXTURES / "spec-books.json"


def books_spec() -> Dict[str, Any]:
    return json.loads(BOOKS_SPEC_PATH.read_text(encoding="utf-8"))


def _field(**overrides: Any) -> Dict[str, Any]:
    field = {"name": "title", "label": "Title", "kind": "text", "required": False,
             "options": [], "unit": "", "in_form": True, "message": ""}
    field.update(overrides)
    return field


def _spec(**overrides: Any) -> Dict[str, Any]:
    """A minimal *usable* raw spec, before whatever the test breaks."""
    spec = {
        "app_name": "Home Library", "tagline": "t", "summary": "s",
        "noun": "book", "noun_plural": "books",
        "fields": [_field()], "title_field": "title",
        "subtitle_fields": [], "meta_fields": [], "constants": [], "filters": [],
        "badges": [], "stats": [], "actions": [],
        "journeys": [{"title": "adds a book", "kind": "explicit", "steps": "", "expect": ""}],
        "omitted_patterns": [], "assumptions": [],
    }
    spec.update(overrides)
    return spec


class FieldNormalisationTest(unittest.TestCase):
    def test_duplicate_names_are_suffixed_so_one_field_cannot_shadow_another(self):
        # Two fields called "name" is one column in the record: the second
        # silently overwrites the first everywhere.
        spec = analyst.normalize_spec(_spec(fields=[
            _field(name="name", label="Name"),
            _field(name="name", label="Owner"),
            _field(name="Name", label="Keeper"),
        ], title_field="name"))
        self.assertEqual([f["name"] for f in spec["fields"]], ["name", "name2", "name3"])
        self.assertEqual([f["label"] for f in spec["fields"]], ["Name", "Owner", "Keeper"])

    def test_names_are_camel_cased_from_whatever_shape_the_model_used(self):
        spec = analyst.normalize_spec(_spec(fields=[
            _field(name="due_date", label="Due Date", kind="date"),
            _field(name="Borrower Name", label="Borrower"),
            _field(name="1st owner", label="First Owner"),
        ], title_field="Borrower Name"))
        # A leading digit is not a legal identifier start; "f" is prefixed.
        self.assertEqual([f["name"] for f in spec["fields"]], ["dueDate", "borrowerName", "f1stOwner"])
        self.assertEqual(spec["title_field"], "borrowerName")

    def test_fields_without_a_name_or_a_label_are_dropped(self):
        # A field with no label has no accessible name; no test can fill it.
        spec = analyst.normalize_spec(_spec(fields=[
            _field(), _field(name="", label="Ghost"), _field(name="ghost2", label="  "), "nonsense",
        ]))
        self.assertEqual([f["name"] for f in spec["fields"]], ["title"])

    def test_unknown_kind_becomes_text(self):
        # "email"/"url"/"currency" are not scaffold kinds; `tsc` rejects them.
        spec = analyst.normalize_spec(_spec(fields=[_field(kind="email")]))
        self.assertEqual(spec["fields"][0]["kind"], "text")

    def test_select_with_fewer_than_two_options_becomes_text(self):
        # A one-option "choice" is a text box with extra steps -- and
        # `chooseFilter`/`selectOptions` cannot exercise it.
        spec = analyst.normalize_spec(_spec(fields=[
            _field(name="type", label="Type", kind="select", options=["Novel"]),
            _field(name="state", label="State", kind="select", options=[]),
            _field(name="kind", label="Kind", kind="select", options=["A", "B", "B", " A "]),
        ]))
        kinds = {f["name"]: (f["kind"], f["options"]) for f in spec["fields"]}
        self.assertEqual(kinds["type"], ("text", []))
        self.assertEqual(kinds["state"], ("text", []))
        self.assertEqual(kinds["kind"], ("select", ["A", "B"]))

    def test_required_fields_always_carry_a_message_and_optional_ones_never_do(self):
        # The "rejects an empty required field" journey asserts on this string.
        spec = analyst.normalize_spec(_spec(fields=[
            _field(name="title", label="Title", required=True, message=""),
            _field(name="author", label="Author", required=False, message="Author is required."),
        ]))
        self.assertEqual(spec["fields"][0]["message"], "Title is required.")
        self.assertEqual(spec["fields"][1]["message"], "")

    def test_unit_survives_only_on_numbers_and_in_form_defaults_to_true(self):
        spec = analyst.normalize_spec(_spec(fields=[
            _field(name="quantity", label="Quantity", kind="number", unit="left"),
            _field(name="note", label="Note", unit="left"),
        ]))
        self.assertEqual(spec["fields"][0]["unit"], "left")
        self.assertEqual(spec["fields"][1]["unit"], "")
        self.assertTrue(all(f["in_form"] for f in spec["fields"]))

    def test_in_form_false_is_preserved(self):
        # A non-title action-set field keeps in_form=False. (The title field is
        # forced onto the form so the empty-required journey can blank it; see
        # test_the_title_field_is_forced_to_a_required_text_field below.)
        spec = analyst.normalize_spec(_spec(fields=[
            _field(name="name", label="Name"),
            _field(name="borrower", label="Borrower", in_form=False),
        ]))
        borrower = next(f for f in spec["fields"] if f["name"] == "borrower")
        self.assertFalse(borrower["in_form"])

    def test_the_title_field_is_forced_to_a_required_text_field(self):
        # 2026-09-04 (carcare/jobhunt holdouts): the empty-required journey must
        # blank a required text field, and `as const`-free inference needs the
        # title to be simple. A select/date/optional title is coerced.
        spec = analyst.normalize_spec(_spec(
            fields=[
                _field(name="stage", label="Stage", kind="select",
                       options=["Applied", "Interviewing", "Offer"], required=True),
                _field(name="note", label="Note"),
            ],
            title_field="stage",
        ))
        title = next(f for f in spec["fields"] if f["name"] == spec["title_field"])
        self.assertEqual(title["kind"], "text")
        self.assertTrue(title["required"])
        self.assertTrue(title["in_form"])
        self.assertTrue(title["message"])


class TitleFieldTest(unittest.TestCase):
    def test_missing_title_field_falls_back_to_the_first_text_field(self):
        # `titleField` must name a real field or the config does not compile.
        spec = analyst.normalize_spec(_spec(
            fields=[_field(name="count", label="Count", kind="number"),
                    _field(name="label", label="Label")],
            title_field="",
        ))
        self.assertEqual(spec["title_field"], "label")

    def test_title_field_naming_a_field_that_does_not_exist_is_replaced(self):
        spec = analyst.normalize_spec(_spec(
            fields=[_field(name="name", label="Name")], title_field="nope"))
        self.assertEqual(spec["title_field"], "name")

    def test_without_any_text_field_the_first_field_wins(self):
        spec = analyst.normalize_spec(_spec(
            fields=[_field(name="count", label="Count", kind="number"),
                    _field(name="done", label="Done", kind="boolean")],
            title_field="missing",
        ))
        self.assertEqual(spec["title_field"], "count")

    def test_no_fields_leaves_the_title_field_empty_and_the_spec_unusable(self):
        spec = analyst.normalize_spec(_spec(fields=[]))
        self.assertEqual(spec["title_field"], "")
        self.assertFalse(analyst.spec_is_usable(spec))

    def test_subtitle_and_meta_keep_only_real_non_title_fields(self):
        # The scaffold prints the title once; repeating it in the subtitle is
        # noise, and an unknown name is a type error.
        spec = analyst.normalize_spec(_spec(
            fields=[_field(name="title", label="Title"), _field(name="author", label="Author")],
            title_field="title",
            subtitle_fields=["title", "author", "ghost", "author"],
            meta_fields=["Author"],
        ))
        self.assertEqual(spec["subtitle_fields"], ["author"])
        self.assertEqual(spec["meta_fields"], ["author"])


class ConstantsTest(unittest.TestCase):
    def test_names_become_upper_snake_and_unique_and_non_numbers_are_dropped(self):
        spec = analyst.normalize_spec(_spec(constants=[
            {"name": "lowThreshold", "value": 2, "comment": "a couple = 2"},
            {"name": "LOW_THRESHOLD", "value": 3, "comment": ""},
            {"name": "soon", "value": "7 days", "comment": ""},
            {"name": "flag", "value": True, "comment": ""},
            {"name": "", "value": 1, "comment": ""},
        ]))
        self.assertEqual([c["name"] for c in spec["constants"]], ["LOW_THRESHOLD", "LOW_THRESHOLD2"])
        self.assertEqual(spec["constants"][0]["value"], 2)


class FilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fields = [_field(name="title", label="Title"),
                       _field(name="type", label="Type", kind="select", options=["Novel", "Cookbook"]),
                       _field(name="borrower", label="Borrower"),
                       _field(name="pages", label="Pages", kind="number")]

    def normalize(self, filters):
        return analyst.normalize_spec(_spec(fields=self.fields, filters=filters))["filters"]

    def test_field_filter_over_a_number_or_a_repeat_is_dropped_but_a_text_one_stays(self):
        # The scaffold draws one chip per option of a select, or per distinct
        # value the rows hold for a text field; a number has no chips to draw.
        filters = self.normalize([
            {"kind": "field", "field": "borrower", "id": "", "label": "All", "rule": "", "empty_text": ""},
            {"kind": "field", "field": "pages", "id": "", "label": "All", "rule": "", "empty_text": ""},
            {"kind": "field", "field": "type", "id": "", "label": "All kinds", "rule": "", "empty_text": ""},
            {"kind": "field", "field": "type", "id": "", "label": "All again", "rule": "", "empty_text": ""},
        ])
        self.assertEqual(len(filters), 2)
        self.assertEqual(filters[0], {"kind": "field", "field": "borrower", "id": "",
                                      "label": "All", "rule": "", "empty_text": ""})
        self.assertEqual(filters[1], {"kind": "field", "field": "type", "id": "",
                                      "label": "All kinds", "rule": "", "empty_text": ""})

    def test_state_filter_needs_id_label_and_rule(self):
        filters = self.normalize([
            {"kind": "state", "field": "borrower", "id": "out", "label": "Out",
             "rule": "borrower is not empty", "empty_text": "Nothing is out"},
            {"kind": "state", "field": "", "id": "home", "label": "Home", "rule": "", "empty_text": ""},
            {"kind": "state", "field": "", "id": "", "label": "Nameless",
             "rule": "borrower is empty", "empty_text": ""},
        ])
        self.assertEqual([f["id"] for f in filters], ["out"])
        # A state filter narrows by a predicate, never by a stored field.
        self.assertEqual(filters[0]["field"], "")

    def test_unknown_kind_is_inferred_from_whether_the_field_is_a_select(self):
        filters = self.normalize([
            {"kind": "", "field": "type", "id": "", "label": "All kinds", "rule": "", "empty_text": ""},
            {"kind": "chip", "field": "", "id": "out", "label": "Out",
             "rule": "borrower is not empty", "empty_text": ""},
        ])
        self.assertEqual([f["kind"] for f in filters], ["field", "state"])

    def test_duplicate_state_ids_are_suffixed(self):
        filters = self.normalize([
            {"kind": "state", "field": "", "id": "out", "label": "Out", "rule": "a", "empty_text": ""},
            {"kind": "state", "field": "", "id": "out", "label": "Away", "rule": "b", "empty_text": ""},
        ])
        self.assertEqual([f["id"] for f in filters], ["out", "out2"])


class BadgeStatActionTest(unittest.TestCase):
    def test_badges_need_id_rule_and_text_and_tones_map_to_the_scaffolds(self):
        # "success" is in the probe's schema but not in the scaffold's `Tone`.
        spec = analyst.normalize_spec(_spec(badges=[
            {"id": "lent", "rule": "borrower is not empty", "tone": "success", "text": "Out: {borrower}"},
            {"id": "odd", "rule": "x", "tone": "rainbow", "text": "Odd"},
            {"id": "gone", "rule": "", "tone": "info", "text": "Gone"},
            {"id": "", "rule": "x", "tone": "info", "text": "Anonymous"},
            {"id": "quiet", "rule": "x", "tone": "info", "text": ""},
        ]))
        self.assertEqual([(b["id"], b["tone"]) for b in spec["badges"]], [("lent", "good"), ("odd", "neutral")])
        for badge in spec["badges"]:
            self.assertIn(badge["tone"], analyst.TONES)

    def test_only_the_first_stat_keeps_its_emphasis(self):
        # Two headline figures are no headline figure.
        spec = analyst.normalize_spec(_spec(stats=[
            {"id": "lent", "label": "Lent out", "rule": "count of rows where x", "emphasis": True},
            {"id": "total", "label": "Books", "rule": "count of all rows", "emphasis": True},
            {"id": "", "label": "Nameless", "rule": "count of all rows", "emphasis": False},
        ]))
        self.assertEqual([(s["id"], s["emphasis"]) for s in spec["stats"]],
                         [("lent", True), ("total", False)])

    def test_actions_need_id_label_and_effect_and_lose_input_flags_without_a_label(self):
        spec = analyst.normalize_spec(_spec(actions=[
            {"id": "lend", "label": "Lend", "available_rule": "", "effect": "set borrower",
             "input_label": "", "input_required": True, "confirm_text": "", "toast": ""},
            {"id": "noop", "label": "Noop", "available_rule": "", "effect": "",
             "input_label": "", "input_required": False, "confirm_text": "", "toast": ""},
        ]))
        self.assertEqual([a["id"] for a in spec["actions"]], ["lend"])
        self.assertFalse(spec["actions"][0]["input_required"])


class DerivedTest(unittest.TestCase):
    """The computed-value primitive: names, collisions, and the missing rule.

    A value the app works out from other fields (a day count, a quantity times
    a price) has to live somewhere other than ``fields``, or the Builder stores
    it and the form asks the user to type the answer.
    """

    def test_names_are_camel_cased_deduplicated_and_the_rule_survives(self):
        spec = analyst.normalize_spec(_spec(derived=[
            {"name": "day count", "label": "Day Count", "rule": "days between Start and End",
             "unit": "days"},
            {"name": "dayCount", "label": "Second", "rule": "days since Start", "unit": ""},
            "nonsense",
        ]))
        self.assertEqual([d["name"] for d in spec["derived"]], ["dayCount", "dayCount2"])
        self.assertEqual(spec["derived"][0]["rule"], "days between Start and End")
        self.assertEqual(spec["derived"][0]["unit"], "days")
        self.assertEqual(spec["derived"][1]["unit"], "")

    def test_a_derived_name_that_collides_with_a_field_is_dropped(self):
        # A derived value is never stored: sharing a key with a field would put
        # the stored column and the computation on one name.
        spec = analyst.normalize_spec(_spec(
            fields=[_field(name="title", label="Title"),
                    _field(name="total", label="Total", kind="number")],
            derived=[{"name": "total", "label": "Total", "rule": "Quantity times Price",
                      "unit": ""},
                     {"name": "value", "label": "Value", "rule": "Quantity times Price",
                      "unit": "£"}],
        ))
        self.assertEqual([d["name"] for d in spec["derived"]], ["value"])
        self.assertEqual(spec["derived"][0]["unit"], "£")

    def test_a_deduplicated_name_never_lands_on_a_field_either(self):
        spec = analyst.normalize_spec(_spec(
            fields=[_field(name="title", label="Title"), _field(name="span2", label="Span Two")],
            derived=[{"name": "span", "label": "Span", "rule": "days between A and B", "unit": ""},
                     {"name": "span", "label": "Span Again", "rule": "days since A", "unit": ""}],
        ))
        self.assertEqual([d["name"] for d in spec["derived"]], ["span", "span3"])

    def test_a_derived_entry_without_a_rule_or_a_label_is_dropped(self):
        # Nothing to compute and nothing to render: the Builder would invent both.
        spec = analyst.normalize_spec(_spec(derived=[
            {"name": "span", "label": "Span", "rule": "  ", "unit": ""},
            {"name": "gap", "label": "", "rule": "days since Start", "unit": ""},
            {"name": "", "label": "Nameless", "rule": "days since Start", "unit": ""},
        ]))
        self.assertEqual(spec["derived"], [])

    def test_a_spec_with_no_derived_key_normalises_to_an_empty_list(self):
        self.assertEqual(analyst.normalize_spec(_spec())["derived"], [])
        for garbage in (None, [], "spec", 7):
            self.assertEqual(analyst.normalize_spec(garbage)["derived"], [])


class ActionScopeAndStatUnitTest(unittest.TestCase):
    def test_scope_defaults_to_row_and_only_all_survives(self):
        # "row" is the safe default: a bulk button written as a row action
        # changes one record, a row action written as a bulk button changes all.
        spec = analyst.normalize_spec(_spec(actions=[
            {"id": "one", "label": "One", "effect": "set done to true"},
            {"id": "two", "label": "Two", "scope": "ALL", "effect": "set done to false"},
            {"id": "three", "label": "Three", "scope": "everything", "effect": "clear holder"},
            {"id": "four", "label": "Four", "scope": "row", "effect": "clear holder"},
        ]))
        self.assertEqual([a["scope"] for a in spec["actions"]], ["row", "all", "row", "row"])

    def test_a_stat_carries_its_unit_as_a_stripped_string(self):
        spec = analyst.normalize_spec(_spec(stats=[
            {"id": "value", "label": "Value", "rule": "sum of price", "unit": "  £ "},
            {"id": "count", "label": "Count", "rule": "count of all rows"},
            {"id": "odd", "label": "Odd", "rule": "count of all rows", "unit": 7},
        ]))
        self.assertEqual([s["unit"] for s in spec["stats"]], ["£", "", ""])


class OldSpecShapeTest(unittest.TestCase):
    """The 2026-09-03 fixture predates every key added here and must still build."""

    def test_the_probe_spec_normalises_with_the_new_keys_defaulted(self):
        spec = analyst.normalize_spec(books_spec())
        self.assertTrue(analyst.spec_is_usable(spec))
        self.assertEqual(spec["derived"], [])
        self.assertEqual([a["scope"] for a in spec["actions"]], ["row", "row"])
        self.assertEqual([s["unit"] for s in spec["stats"]], [""])
        # Still idempotent now that the defaults are written back into the spec.
        self.assertEqual(analyst.normalize_spec(spec), spec)


class JourneyTest(unittest.TestCase):
    def test_empty_journeys_stay_empty_and_make_the_spec_unusable(self):
        # Without journeys there is nothing to test and nothing to report.
        spec = analyst.normalize_spec(_spec(journeys=[]))
        self.assertEqual(spec["journeys"], [])
        self.assertIn("no journeys", analyst.unusable_reasons(spec))

    def test_journeys_are_deduped_by_normalised_title_and_kinds_are_repaired(self):
        spec = analyst.normalize_spec(_spec(journeys=[
            {"title": "Adds a book", "kind": "explicit", "steps": " fill  ", "expect": "shown"},
            {"title": "adds a book!", "kind": "implied", "steps": "", "expect": ""},
            {"title": "", "kind": "explicit", "steps": "", "expect": ""},
            {"title": "Deletes a book", "kind": "guess", "steps": "", "expect": ""},
        ]))
        self.assertEqual([j["title"] for j in spec["journeys"]], ["Adds a book", "Deletes a book"])
        self.assertEqual([j["kind"] for j in spec["journeys"]], ["explicit", "explicit"])
        self.assertEqual(spec["journeys"][0]["steps"], "fill")


class SpecShapeTest(unittest.TestCase):
    def test_storage_key_noun_and_defaults(self):
        spec = analyst.normalize_spec(_spec(noun="  Book  ", noun_plural="Board Games"))
        self.assertEqual(spec["noun"], "book")
        self.assertEqual(spec["storage_key"], "board-games.v1")
        fallback = analyst.normalize_spec(_spec(noun="", noun_plural=""))
        self.assertEqual((fallback["noun"], fallback["noun_plural"]), ("record", "records"))
        self.assertEqual(fallback["storage_key"], "records.v1")

    def test_garbage_in_gives_a_complete_empty_spec_rather_than_an_exception(self):
        for garbage in (None, [], "spec", 7, {"fields": "many", "journeys": {"a": 1}}):
            spec = analyst.normalize_spec(garbage)
            self.assertEqual(spec["fields"], [])
            self.assertEqual(spec["journeys"], [])
            self.assertFalse(analyst.spec_is_usable(spec))

    def test_normalising_the_real_probe_spec_is_stable_and_usable(self):
        once = analyst.normalize_spec(books_spec())
        self.assertTrue(analyst.spec_is_usable(once))
        # Idempotent: the harness re-reads spec.json between processes.
        self.assertEqual(analyst.normalize_spec(once), once)
        self.assertEqual(once["storage_key"], "books.v1")
        self.assertEqual(once["title_field"], "title")
        self.assertEqual(len(once["journeys"]), 10)
        # The v2 schema drops implemented_features; the Architect derives it.
        self.assertNotIn("implemented_features", once)


class SpecIsUsableTest(unittest.TestCase):
    def test_the_probe_spec_is_usable(self):
        self.assertTrue(analyst.spec_is_usable(analyst.normalize_spec(books_spec())))
        self.assertEqual(analyst.unusable_reasons(analyst.normalize_spec(books_spec())), [])

    def test_each_missing_requirement_is_named(self):
        cases = [
            (_spec(fields=[]), "no usable fields"),
            (_spec(journeys=[]), "no journeys"),
            (_spec(app_name="  "), "no app_name"),
        ]
        for raw, reason in cases:
            spec = analyst.normalize_spec(raw)
            self.assertFalse(analyst.spec_is_usable(spec), reason)
            self.assertIn(reason, analyst.unusable_reasons(spec))

    def test_a_title_field_pointing_nowhere_is_unusable(self):
        # normalize_spec repairs this, so only a hand-made spec can trip it --
        # which is exactly what a stale spec.json from another run would be.
        broken = dict(analyst.normalize_spec(_spec()), title_field="ghost")
        self.assertFalse(analyst.spec_is_usable(broken))
        self.assertIn("no valid title_field", analyst.unusable_reasons(broken))

    def test_non_dicts_are_never_usable(self):
        for value in (None, [], "spec", 0):
            self.assertFalse(analyst.spec_is_usable(value))


class SchemaAndPromptTest(unittest.TestCase):
    def _walk(self, node: Any) -> None:
        if isinstance(node, dict) and node.get("type") == "object":
            # Strict json_schema mode: every key required, nothing extra.
            self.assertFalse(node.get("additionalProperties", True))
            self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
            for child in node["properties"].values():
                self._walk(child)
        elif isinstance(node, dict) and node.get("type") == "array":
            self._walk(node["items"])

    def test_schema_is_strict_all_the_way_down(self):
        self._walk(analyst.SCHEMA)

    def test_schema_drops_implemented_features_and_keeps_the_rest_of_the_probe(self):
        properties = analyst.SCHEMA["properties"]
        self.assertNotIn("implemented_features", properties)
        for name in ("app_name", "fields", "title_field", "constants", "filters", "badges",
                     "stats", "actions", "journeys", "omitted_patterns", "assumptions"):
            self.assertIn(name, properties)
        self.assertEqual(properties["fields"]["items"]["properties"]["kind"]["enum"],
                         list(analyst.FIELD_KINDS))

    def test_the_schema_offers_the_three_shapes_the_holdout_set_had_no_word_for(self):
        properties = analyst.SCHEMA["properties"]
        derived = properties["derived"]["items"]
        self.assertEqual(sorted(derived["properties"]), ["label", "name", "rule", "unit"])
        self.assertEqual(sorted(derived["required"]), ["label", "name", "rule", "unit"])
        self.assertEqual(properties["actions"]["items"]["properties"]["scope"]["enum"],
                         ["row", "all"])
        self.assertIn("unit", properties["stats"]["items"]["properties"])
        self.assertIn("unit", properties["stats"]["items"]["required"])

    def test_the_system_prompt_names_each_new_primitive_in_general_terms(self):
        prompt = analyst.SYSTEM_PROMPT
        self.assertIn("derived entry, never an input field", prompt)
        self.assertIn("days until/since/between", prompt)
        self.assertIn("scope 'all'", prompt)
        self.assertIn("scope 'row'", prompt)
        self.assertIn("a currency symbol is a unit", prompt)
        # A computed value nobody looks at is a computed value nobody tested.
        self.assertIn("each derived value", prompt)

    def test_max_tokens_leaves_headroom_over_the_measured_1303(self):
        self.assertEqual(analyst.MAX_TOKENS, 2500)

    def test_the_coverage_checklist_is_appended_only_when_journeys_md_is_given(self):
        journeys_md = (support.REPO_ROOT / "contract-public" / "journeys.md").read_text(encoding="utf-8")
        self.assertEqual(analyst.build_system_prompt(""), analyst.SYSTEM_PROMPT)
        self.assertEqual(analyst.build_system_prompt("   "), analyst.SYSTEM_PROMPT)
        prompt = analyst.build_system_prompt(journeys_md)
        self.assertTrue(prompt.startswith(analyst.SYSTEM_PROMPT))
        self.assertIn("Coverage checklist", prompt)
        self.assertIn("1. Add the idea's complete primary record and show it in the collection.", prompt)
        self.assertIn("5. Preserve required data across a browser refresh.", prompt)
        # Run/reporting guidance is the runner's business, not the Analyst's.
        self.assertNotIn("localhost:3000", prompt)
        self.assertLess(len(prompt), 3300)


class RunAnalystTest(unittest.TestCase):
    """``run_analyst`` end to end against the scripted fake gateway."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.harness_dir = pathlib.Path(self._tmp.name) / "harness"
        self.server = FakeGatewayServer()
        self.server.start()
        # forward_record writes the synthetic message_end line to stdout for
        # real; catch it here rather than polluting the test runner's stdout.
        self.sink = io.BytesIO()
        self._stdout_patch = mock.patch.object(pirpc.sys, "stdout", mock.Mock(buffer=self.sink))
        self._stdout_patch.start()

    def tearDown(self) -> None:
        self._stdout_patch.stop()
        self.server.stop()
        self._tmp.cleanup()

    def client(self) -> gateway.GatewayClient:
        return gateway.GatewayClient(
            base_url=self.server.base_url, api_key="test-key", model="zai-org/GLM-5.2",
            provider="berget", harness_dir=self.harness_dir, backoff=(0.01, 0.01, 0.01, 0.01),
        )

    @property
    def spec_path(self) -> pathlib.Path:
        return self.harness_dir / "spec.json"

    def test_a_valid_spec_is_normalised_written_and_returned(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(
            content=json.dumps(books_spec())))])
        spec = analyst.run_analyst(self.client(), "an idea", self.harness_dir)

        self.assertIsNotNone(spec)
        self.assertTrue(analyst.spec_is_usable(spec))
        self.assertEqual(spec["app_name"], "Home Library")
        # What is written is what is returned: the *normalised* spec, so the
        # next process to read spec.json sees exactly what missions mode used.
        self.assertEqual(json.loads(self.spec_path.read_text(encoding="utf-8")), spec)
        self.assertEqual(spec["storage_key"], "books.v1")

    def test_the_request_carries_the_strict_schema_the_checklist_and_the_budget(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(
            content=json.dumps(books_spec())))])
        analyst.run_analyst(
            self.client(), "an idea", self.harness_dir,
            journeys_md="## Behaviors to implement and test when implied\n\n1. Add a record.\n",
        )
        body = self.server.requests[0]["body"]
        self.assertEqual(body["max_tokens"], analyst.MAX_TOKENS)
        self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
        schema = body["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["name"], "app_spec")
        self.assertEqual(schema["schema"], analyst.SCHEMA)
        self.assertIn("1. Add a record.", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1]["content"], "an idea")

    def test_a_spec_that_normalises_to_unusable_returns_none_and_writes_nothing(self):
        # The Phase-2 shape (app_name/tagline/summary/primary_entity) has no
        # fields and no journeys: usable as a report header, useless as a build
        # contract. Missions mode must fall back rather than build from it.
        self.server.script([ScriptedResponse(status=200, body=ok_response(content=json.dumps({
            "app_name": "Fake App", "tagline": "t", "summary": "s", "primary_entity": "item",
        })))])
        self.assertIsNone(analyst.run_analyst(self.client(), "an idea", self.harness_dir))
        self.assertFalse(self.spec_path.exists())

    def test_four_503s_return_none_and_write_nothing(self):
        self.server.script([ScriptedResponse(status=503, body={"error": "busy"})] * 4)
        self.assertIsNone(analyst.run_analyst(self.client(), "an idea", self.harness_dir))
        self.assertFalse(self.spec_path.exists())
        self.assertEqual(len(self.server.requests), 4)

    def test_a_json_array_instead_of_an_object_returns_none(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(content="[1, 2]"))] * 2)
        self.assertIsNone(analyst.run_analyst(self.client(), "an idea", self.harness_dir))
        self.assertFalse(self.spec_path.exists())

    def test_an_exploding_client_is_swallowed_the_analyst_never_blocks_the_build(self):
        class Exploding:
            def json_schema(self, *args: Any, **kwargs: Any):
                raise RuntimeError("gateway on fire")

        self.assertIsNone(analyst.run_analyst(Exploding(), "an idea", self.harness_dir))
        self.assertFalse(self.spec_path.exists())

    def test_an_unwritable_harness_dir_is_swallowed_too(self):
        self.server.script([ScriptedResponse(status=200, body=ok_response(
            content=json.dumps(books_spec())))])
        blocker = pathlib.Path(self._tmp.name) / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        self.assertIsNone(analyst.run_analyst(self.client(), "an idea", blocker / "harness"))

    def test_the_deadline_and_journeys_md_are_optional_keyword_arguments(self):
        # __main__ calls run_analyst(client, idea, dir, deadline=...); the new
        # journeys_md parameter must not disturb that call site.
        import inspect

        signature = inspect.signature(analyst.run_analyst)
        self.assertEqual(list(signature.parameters), ["client", "idea_text", "harness_dir",
                                                      "deadline", "journeys_md"])
        self.assertIsNone(signature.parameters["deadline"].default)
        self.assertEqual(signature.parameters["journeys_md"].default, "")


if __name__ == "__main__":
    unittest.main()
