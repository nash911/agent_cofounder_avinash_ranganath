"""The Architect: the build plan and every mission brief, with no model call.

In the scaffold world the plan is fully determined by the spec -- one config
file, one test file, one ``it`` per journey -- so a planning call would buy
nothing but latency and points (a documented deviation from BUILD_PLAN rev 6,
which gave the Architect its own model call). Everything here is a pure
function of ``harness/spec.json``.

The briefs are the whole user message of a mission session. The shared prefix
(system prompt + ``AGENTS.md``) is byte-identical across missions so the
provider's prefix cache pays for it once; these briefs are the only part that
differs, which is why they are kept under ``MAX_BRIEF_CHARS`` (~2,000 tokens)
and carry the *strings* rather than instructions to go and read files: a
mission that reads three scaffold files costs ~5k input tokens (Phase 2 run
``python-k``, calls 2-3) to learn what the spec already knows.

The Builder and the Tester never see each other's file. The only thing that
keeps ``getByLabelText("Title")`` in the test aligned with ``label: "Title"``
in the config is that both briefs render the same strings out of the same
spec.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# The cheat sheet and the spec primitives both briefs share. Imported under
# their private names so the rest of this module reads exactly as before.
from .specstrings import (
    _bulk_actions,
    _compute_rule,
    _copy,
    _dicts,
    _join,
    _label_of,
    _needs_dates,
    _removes,
    _row_actions,
    _strings,
    _text,
    _visible_strings,
)

#: The two files a mission may write, and everything it may not touch.
CONFIG_FILE = "src/app-config.ts"
TESTS_FILE = "src/journeys.test.tsx"
LEAVE_ALONE = [
    "src/lib/", "src/components/", "src/App.tsx", "src/test/", "src/styles.css",
    "vitest.config.ts", "package.json",
]

#: ~2,400 tokens at ~4 chars/token. Briefs are re-rendered more compactly (and
#: finally clipped) rather than allowed to grow: a mission brief that costs
#: more than the file it asks for has lost the argument. Measured 2026-09-04
#: over ten holdout specs: the Tester's half runs 7.0-7.9k with the cheat sheet
#: complete, so 8k left no room for one more rule, and the clip fell on the
#: rules at the tail rather than the journeys.
MAX_BRIEF_CHARS = 9500

#: ``report.partial.json`` reads better with a handful of features than with
#: one line per journey; the runner does not score their number.
MAX_FEATURES = 12

#: Helpers the Tester may use (``app-template/src/test/helpers.tsx``). Naming
#: them is what keeps the Tester from inventing queries the scaffold cannot
#: answer -- it never reads the file.
TEST_HELPERS = (
    "renderApp, addRecord, editRecord, removeRecord, runAction, runBulkAction, chooseFilter, "
    "search, reload, corruptStorage, row, rowTitles, expectRow, expectNoRow, stat, "
    "confirmDialog, fill"
)

#: Two capabilities the scaffold renders whatever the spec says: ``FilterBar``
#: draws a search box unless ``search: false`` (which nothing here sets), and
#: ``_config_outline`` always sorts by the title field. An "omitted" line about
#: either would contradict the app the grader can open.
_ALWAYS_PRESENT = re.compile(r"\bsearch|\bsort", re.IGNORECASE)


# ---------------------------------------------------------------------------
# derive_plan
# ---------------------------------------------------------------------------


def derive_plan(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The whole build, derived from the spec. Pure; never raises."""
    spec = spec if isinstance(spec, dict) else {}
    fields = _dicts(spec.get("fields"))
    journeys = _dicts(spec.get("journeys"))
    actions = _dicts(spec.get("actions"))

    return {
        "files": {"config": CONFIG_FILE, "tests": TESTS_FILE},
        "leave_alone": list(LEAVE_ALONE),
        "tests": [
            {
                "title": _text(journey.get("title")),
                "kind": _text(journey.get("kind")) or "explicit",
                "steps": _text(journey.get("steps")),
                "expect": _text(journey.get("expect")),
            }
            for journey in journeys
            if _text(journey.get("title"))
        ],
        "implemented_features": _features(journeys, actions),
        "assumptions": _assumptions(spec),
        "labels": {
            "fields": {_text(f.get("name")): _text(f.get("label")) for f in fields},
            "options": {
                _text(f.get("name")): [_text(o) for o in _strings(f.get("options"))]
                for f in fields
                if _strings(f.get("options"))
            },
            "filters": [_text(f.get("label")) for f in _dicts(spec.get("filters")) if _text(f.get("label"))],
            "badges": [_text(b.get("text")) for b in _dicts(spec.get("badges")) if _text(b.get("text"))],
            "stats": [_text(s.get("label")) for s in _dicts(spec.get("stats")) if _text(s.get("label"))],
            "actions": [_text(a.get("label")) for a in actions if _text(a.get("label"))],
            "inputs": [_text(a.get("input_label")) for a in actions if _text(a.get("input_label"))],
            "messages": [_text(f.get("message")) for f in fields if _text(f.get("message"))],
            "copy": _copy(spec),
        },
    }


def _features(journeys: List[Dict[str, Any]], actions: List[Dict[str, Any]]) -> List[str]:
    """One short feature line per journey, plus any action no journey names."""
    features: List[str] = []
    for journey in journeys:
        title = _sentence(_text(journey.get("title")))
        if title and title not in features:
            features.append(title)
    lowered = " ".join(features).lower()
    for action in actions:
        label = _text(action.get("label"))
        if label and label.lower() not in lowered:
            features.append("{0} action".format(label))
    return features[:MAX_FEATURES]


def _assumptions(spec: Dict[str, Any]) -> List[str]:
    """The spec's assumptions, plus one line per omitted pattern and constant.

    The scorer reads ``assumptions`` for evidence that every ambiguity was
    decided deliberately; an omitted pattern with its reason is exactly that,
    and so is a threshold the idea left vague.
    """
    lines: List[str] = list(_strings(spec.get("assumptions")))
    lines.extend(_number_field_assumptions(spec))
    dropped = False
    for omitted in _dicts(spec.get("omitted_patterns")):
        pattern = _text(omitted.get("pattern"))
        if not pattern:
            continue
        if _ALWAYS_PRESENT.search(pattern):
            # The scaffold renders a search box unconditionally and this plan
            # always sorts by the title field, so claiming either was left out
            # contradicts the running app -- the one place a grader can check
            # an assumption.
            dropped = True
            continue
        reason = _text(omitted.get("reason")) or "not implied by the idea"
        lines.append("Omitted {0}: {1}".format(pattern, reason))
    if dropped:
        lines.append(
            "Search and alphabetical sorting are provided by the scaffold and are always "
            "available."
        )
    for constant in _dicts(spec.get("constants")):
        name = _text(constant.get("name"))
        if not name:
            continue
        comment = _text(constant.get("comment"))
        lines.append(
            "{0} = {1}{2}".format(name, constant.get("value"), ": " + comment if comment else "")
        )
    return [line for index, line in enumerate(lines) if line not in lines[:index]]


def _number_field_assumptions(spec: Dict[str, Any]) -> List[str]:
    """The number-field decisions ``_field_outline`` makes on the spec's behalf.

    ``min: 0`` and ``initial: 0`` are invented here, and the scaffold enforces
    them at validation time ("<Label> must be at least 0."). A product decision
    the idea did not state belongs in ``assumptions`` whether or not it is the
    right one.
    """
    lines: List[str] = []
    for field in _dicts(spec.get("fields")):
        if _text(field.get("kind")) != "number":
            continue
        label = _text(field.get("label"))
        if not label:
            continue
        lines.append(
            "{0} is {1}, starts at 0 and cannot be negative.".format(
                label,
                "a whole number" if field.get("integer", True) else "allowed decimals",
            )
        )
    return lines


# ---------------------------------------------------------------------------
# the briefs
# ---------------------------------------------------------------------------


def builder_brief(spec: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """The Builder's whole mission: the config as data, plus how to render it."""
    spec = spec if isinstance(spec, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    outline = json.dumps(_config_outline(spec, plan), indent=1, ensure_ascii=False)
    constants = _dicts(spec.get("constants"))
    derived = _dicts(spec.get("derived"))
    # A day count is hand-rolled wrongly whether it is a derived value or a
    # predicate ("within the next N days"), so the helper import is offered
    # whenever the config could need one. `_needs_dates` is the Tester's own
    # test (a date field, or any rule that counts days -- a date the Analyst
    # wrote as text still needs the helpers), so the two briefs cannot
    # disagree about whether this idea is date-relative.
    dated = bool(derived) or _needs_dates(spec)

    rules = [
        "One `write` of the whole file. Nothing else -- no other file, no command, no reading.",
        "Shape: `import { defineApp } from \"./lib/config-types.js\";` then any exported "
        "constants, then `export const appConfig = defineApp({ ... });`. Never annotate the "
        "export (`: AppConfig` destroys inference) and never hoist `fields` into its own const.",
        # Measured 2026-09-04 (a holdout case, the only gate failure): the model
        # wrote `fields: [...] as const` and tsc failed with TS2719 ("Two
        # different types with this name"); two repair rounds could not recover.
        "Never write `as const` anywhere -- not on `fields`, not on any array or object. "
        "`defineApp` already infers every literal type; an `as const` creates a second, "
        "unrelated type and `tsc` fails with TS2719.",
        # `rule` is not a key the outline emits -- a state filter's predicate
        # arrives as `match` -- and naming a key that is not there invites the
        # Builder to write the English straight into `match:` as a string.
        # Measured 2026-09-04 (a holdout case): the model left `match` as the
        # English string and tsc failed TS2322 (string not assignable to a
        # predicate). The value must be a function, never a string.
        "Every `match`/`when`/`available`/`compute`/`apply`/`text`/`toast` value shown above "
        "as English is a placeholder for a FUNCTION you write, never a string: translate "
        "\"borrower is not empty\" to a predicate/patch over `row.<name>`, `(row) => row.borrower !== \"\"`. A string left in any "
        "of these positions fails `tsc`.",
        "`{name}` placeholders in badge and toast text are template literals: "
        "`` (row) => `Out: ${row.borrower}` ``.",
        "An action's `apply` is `(row, input) => ({ ... })`: it returns only the fields that "
        "change, and `input` is the string the user typed into the action's own `input` dialog "
        "(`\"\"` when the action has none). Never call `prompt()`, `confirm()` or `alert()` -- "
        "the scaffold renders the dialog for you and jsdom has none of them."
        "Prefer a single expression for `apply`. If you compute a `select` field's next "
        "value in a block body, the result must be one of that field's exact options, never a "
        "plain `string`, or `tsc` fails with TS2719.",
        "Every `required` field keeps its `message` exactly as written -- a test asserts on it.",
        # Measured 2026-09-04 (a holdout case): an instant one-click action was
        # given an input dialog and a confirmation the spec never asked for, and
        # every test that simply clicked the button failed.
        "Never add `input` or `confirm` to an action unless it appears in the data above: an "
        "action with neither is one click that applies immediately.",
        # Measured 2026-09-04 (a holdout case): the config printed "40 £" while
        # the test written from the same spec expected "£40".
        "A `unit` is rendered by the scaffold, never by you: a currency symbol prints before "
        "the value (`£40`), any other unit after it (`40 pts`) -- field, derived value and "
        "stat alike. Give the `unit`; return a bare number from every `compute`.",
    ]
    if derived:
        rules.append(
            "Each `derived` entry is computed, never stored and never on the form: write "
            "`compute: (row) => ...` over `row.<name>` of the fields above. It renders in the "
            "row's meta list; do not add a field for it."
        )
    if dated:
        rules.append(
            "Date fields are `\"yyyy-mm-dd\"` strings: do every day count with the scaffold's "
            "helpers, `import { daysBetween, daysUntil, daysSince, today } from \"./lib/dates.js\";` "
            "-- in a `compute`, a `when`, a `match` and an `available` alike. Never subtract date "
            "strings or hand-roll `new Date()` arithmetic. Every `date` field stays on the form "
            "with `initial: \"today\"` exactly as shown above, so a new record is dated today "
            "and a day count on it starts at 0; never treat an empty date as a special "
            "\"never\" state (`daysSince(\"\")` is 0, the same as today)."
        )
    if _bulk_actions(spec):
        rules.append(
            "Every `bulkActions` entry applies to EVERY record at once and belongs in the "
            "`bulkActions` array (`apply: (row) => ({ ... })`, no `input`); writing it as an "
            "`actions` entry changes only the row the user clicked."
        )
    if any(_removes(_text(a.get("effect"))) for a in _dicts(spec.get("actions"))):
        rules.append(
            "An `apply` that returns `null` DELETES that row -- the only way an action removes "
            "records, in `actions` and `bulkActions` alike: `apply: () => null`. A patch may "
            "name only the fields above, so never invent a field such as `_deleted` and never "
            "zero a count to stand in for a removal."
        )
    if constants:
        rules.append(
            "Each constant is an exported `const` above `appConfig` with its comment as a "
            "`/** ... */` docblock, and is used by every filter, badge and summary that "
            "mentions it -- never inline the number twice."
        )
    rules.append(
        # Measured 2026-09-04 (python-mission-b): the Builder read helpers.tsx before
        # writing -- one wasted call plus 3.3k input tokens -- and both Testers edited
        # their own file after the write. Say it last, so it is the freshest rule.
        "Your first and only tool call is the `write`. Never call `read`: `src/test/helpers.tsx`, "
        "`src/lib/*` and the worked example are already in this prompt. End your turn "
        "immediately after the write: no re-read, no edit of your own file, no summary."
    )

    body = [
        "## Mission: write `{0}`".format(CONFIG_FILE),
        "",
        "Rewrite the file completely. It is the only file you touch; the rest of the "
        "application already renders from it.",
        "",
        "### The application, as data",
        "",
        "```json",
        outline,
        "```",
        "",
        "### Rules",
        "",
    ]
    body.extend("- " + rule for rule in rules)
    return _fit("\n".join(body))


def _config_outline(spec: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """The spec rendered in the scaffold's own key names, ready to transcribe."""
    labels = plan.get("labels") if isinstance(plan.get("labels"), dict) else {}
    copy = labels.get("copy") if isinstance(labels.get("copy"), dict) else _copy(spec)
    fields = _dicts(spec.get("fields"))

    outline: Dict[str, Any] = {
        "storageKey": _text(spec.get("storage_key")) or "records.v1",
        "copy": copy,
        "fields": [_field_outline(field) for field in fields],
        "titleField": _text(spec.get("title_field")),
        "sort": {"field": _text(spec.get("title_field")), "direction": "asc"},
    }
    # Omitted rather than empty: an empty array reads as "there is one of these
    # and it is blank", and the Builder then writes `filters: []`. For the two
    # field lists that is not cosmetic -- `RecordList.metaNames` is
    # `if (config.metaFields) return config.metaFields;`, and `[]` is truthy,
    # so an empty array suppresses the row's whole meta line instead of falling
    # back to every listable field. Measured: a row rendered as "Dune" alone,
    # with Author and Type invisible and every journey asserting them red.
    optional = [
        ("subtitleFields", _strings(spec.get("subtitle_fields"))),
        ("metaFields", _strings(spec.get("meta_fields"))),
        ("derived", [_derived_outline(d) for d in _dicts(spec.get("derived"))]),
        ("constants", [{"name": _text(c.get("name")), "value": c.get("value"),
                        "comment": _text(c.get("comment"))} for c in _dicts(spec.get("constants"))]),
        ("filters", [_filter_outline(f) for f in _dicts(spec.get("filters"))]),
        ("badges", [{"id": _text(b.get("id")), "when": _text(b.get("rule")),
                     "tone": _text(b.get("tone")), "text": _text(b.get("text"))}
                    for b in _dicts(spec.get("badges"))]),
        ("summary", [_stat_outline(s) for s in _dicts(spec.get("stats"))]),
        ("actions", [_action_outline(a) for a in _row_actions(spec)]),
        # A bulk action under `actions` is a per-row button: it changes the one
        # record the user clicked and leaves every other row alone, which is
        # exactly the failure the scope primitive exists to prevent.
        ("bulkActions", [_bulk_action_outline(a) for a in _bulk_actions(spec)]),
    ]
    for key, value in optional:
        if value:
            outline[key] = value
    return outline


def _field_outline(field: Dict[str, Any]) -> Dict[str, Any]:
    kind = _text(field.get("kind")) or "text"
    entry: Dict[str, Any] = {"kind": kind, "name": _text(field.get("name")),
                             "label": _text(field.get("label"))}
    if field.get("required"):
        entry["required"] = True
        entry["message"] = _text(field.get("message"))
    if kind == "select":
        entry["options"] = _strings(field.get("options"))
        if entry["options"]:
            entry["initial"] = entry["options"][0]
    if kind == "number":
        # The scaffold's number field defaults to a free float with no initial
        # value. Starting at 0 and refusing negatives is safe for every idea so
        # far; whether only whole numbers make sense is the Analyst's call (a
        # rating or a price is not a count), and `_assumptions` records it.
        entry.update({"min": 0, "initial": 0})
        if field.get("integer", True):
            entry["integer"] = True
        if _text(field.get("unit")):
            entry["unit"] = _text(field.get("unit"))
    if kind == "date":
        # Dated the day it is added, so a rule that counts days since it reads
        # 0 for a new record and a test dates a record into any state through
        # the form. An empty date would be a "never" state the Builder and the
        # Tester each resolve differently (measured 2026-09-04, a holdout case).
        entry["initial"] = "today"
    if not field.get("in_form", True):
        entry["inForm"] = False
    return entry


def _derived_outline(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One computed value in the scaffold's key names.

    ``compute`` carries the English exactly as the field outline carries a
    label: it is the one key whose value the Builder has to turn into a
    function, and naming it the same as a stat's keeps that rule single.
    """
    outline: Dict[str, Any] = {
        "name": _text(entry.get("name")),
        "label": _text(entry.get("label")),
        "compute": _text(entry.get("rule")),
    }
    if _text(entry.get("unit")):
        outline["unit"] = _text(entry.get("unit"))
    return outline


def _stat_outline(entry: Dict[str, Any]) -> Dict[str, Any]:
    outline: Dict[str, Any] = {
        "id": _text(entry.get("id")),
        "label": _text(entry.get("label")),
        "compute": _compute_rule(_text(entry.get("rule"))),
    }
    if _text(entry.get("unit")):
        outline["unit"] = _text(entry.get("unit"))
    outline["emphasis"] = bool(entry.get("emphasis"))
    return outline


def _filter_outline(entry: Dict[str, Any]) -> Dict[str, Any]:
    if _text(entry.get("kind")) == "field":
        outline = {"kind": "field", "field": _text(entry.get("field")), "allLabel": _text(entry.get("label"))}
        if _text(entry.get("empty_text")):
            outline["emptyText"] = _text(entry.get("empty_text"))
        return outline
    return {
        "kind": "state",
        "id": _text(entry.get("id")),
        "label": _text(entry.get("label")),
        "match": _text(entry.get("rule")),
        "emptyText": _text(entry.get("empty_text")),
    }


def _action_outline(entry: Dict[str, Any]) -> Dict[str, Any]:
    outline: Dict[str, Any] = {"id": _text(entry.get("id")), "label": _text(entry.get("label"))}
    if _text(entry.get("available_rule")):
        outline["available"] = _text(entry.get("available_rule"))
    if _text(entry.get("input_label")):
        outline["input"] = {
            "label": _text(entry.get("input_label")),
            "required": bool(entry.get("input_required")),
        }
    if _text(entry.get("confirm_text")):
        outline["confirm"] = _text(entry.get("confirm_text"))
    outline["apply"] = _apply_text(_text(entry.get("effect")))
    if _text(entry.get("toast")):
        outline["toast"] = _text(entry.get("toast"))
    return outline


def _apply_text(effect: str) -> str:
    """The English of an ``apply``, with the one shape a patch cannot say.

    A patch names fields that change; a deletion changes none. The scaffold's
    word for it is an ``apply`` that returns ``null``, and the outline says so
    right where the Builder is reading the effect.
    """
    if _removes(effect):
        return "{0} -- return null, which deletes the row".format(effect)
    return effect


def _bulk_action_outline(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One toolbar button applied to every record.

    No ``input`` key exists here on purpose: a bulk apply takes the row alone,
    so an input the spec did not ask for has nowhere to go -- and an action
    that asks the user to type once and then rewrites every record is not what
    any idea means by "start over".
    """
    outline: Dict[str, Any] = {"id": _text(entry.get("id")), "label": _text(entry.get("label"))}
    if _text(entry.get("available_rule")):
        outline["available"] = _text(entry.get("available_rule"))
    if _text(entry.get("confirm_text")):
        outline["confirm"] = _text(entry.get("confirm_text"))
    outline["apply"] = _apply_text(_text(entry.get("effect")))
    return outline


def tester_brief(spec: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """The Tester's whole mission: the journeys, and every string they may query."""
    spec = spec if isinstance(spec, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    tests = [t for t in _dicts(plan.get("tests")) if _text(t.get("title"))]

    journeys = []
    for index, test in enumerate(tests, start=1):
        line = '{0}. "{1}"'.format(index, _text(test.get("title")))
        detail = " — do: {0}".format(_text(test.get("steps"))) if _text(test.get("steps")) else ""
        expect = " — expect: {0}".format(_text(test.get("expect"))) if _text(test.get("expect")) else ""
        journeys.append(line + detail + expect)

    rules = [
        "One `write` of `{0}`, the whole file at once. Write no other file.".format(TESTS_FILE),
        "`import { describe, expect, it } from \"vitest\";` and the helpers from "
        "`\"./test/helpers.js\"` (the file sits one directory above them). "
        "Never import or render `App` -- `renderApp()` does it.",
        "Helpers, and nothing else: {0}.".format(TEST_HELPERS),
        "One `it` per journey, in the order above, with the title copied verbatim -- the "
        "harness matches the runner's report against those exact titles.",
        "Query only by the visible strings listed above; a label you invent is a failing test.",
        "Every helper except `renderApp`, `reload`, `corruptStorage`, `row`, `rowTitles`, "
        "`expectRow`, `expectNoRow` and `stat` is async: `await` it.",
        "At most 150 lines: about 6 lines per journey, no comments, no helper functions of "
        "your own, no shared state between tests.",
        "Do not read any file and do not run any command -- the harness runs vitest for you.",
        "Your first and only tool call is the `write`. Write the file once and stop: never re-read "
        "or edit your own file afterwards -- the harness runs the tests and a separate repair "
        "mission fixes any failure. End your turn immediately after the write: no summary.",
    ]

    body = [
        "## Mission: write `{0}`".format(TESTS_FILE),
        "",
        "`{0}` already renders the whole application from one declaration; another agent is "
        "writing it from the same specification you are reading. Test the user's journeys "
        "through it.".format(CONFIG_FILE),
        "",
        "### Journeys, one `it` each",
        "",
    ]
    body.extend(journeys)
    body.extend(["", "### The exact strings the application shows", "", _visible_strings(spec, plan), ""])
    body.append("### Rules")
    body.append("")
    body.extend("- " + rule for rule in rules)
    return _fit("\n".join(body))


#: The two closing rules the single-file briefs end with; the combined brief
#: replaces them with one ordering rule.
_BUILDER_CLOSING = (
    "- Your first and only tool call is the `write`. Never call `read`: `src/test/helpers.tsx`, "
    "`src/lib/*` and the worked example are already in this prompt. End your turn "
    "immediately after the write: no re-read, no edit of your own file, no summary."
)
_TESTER_CLOSING = (
    "- Your first and only tool call is the `write`. Write the file once and stop: never re-read "
    "or edit your own file afterwards -- the harness runs the tests and a separate repair "
    "mission fixes any failure. End your turn immediately after the write: no summary."
)
#: Both halves whole, with room to spare. Measured 2026-09-04 over ten holdout
#: specs: the untrimmed combined brief ran 14.3-16.2k, and at 12k `_fit` went
#: through every cut down to the tail clip -- which threw away the Tester's
#: rules, every journey's expectation and the closing order. A Tester told the
#: journey titles alone guessed the expectations, and one case failed the same
#: six tests three rounds running. ~1.2k more input tokens per run is cheaper
#: than one repair round (5-8k).
MAX_COMBINED_BRIEF_CHARS = 18000


def combined_brief(spec: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """Both missions in one prompt, config first (``HARNESS_SESSION_MODE=combined``).

    Measured 2026-09-04: a single session that received the Builder and Tester
    briefs as two consecutive prompts cost 14.6k points against 21.6k for two
    parallel sessions -- the second prompt's cold/partial prefix and the
    Tester's own closing turn were most of the gap. Folding the two into one
    prompt removes one more closing turn and one brief's worth of input; the
    Tester side loses nothing it had in single mode, since the config was
    already in its context there too.
    """
    builder = builder_brief(spec, plan)
    tester = tester_brief(spec, plan)
    builder = builder.replace(
        "## Mission: write `{0}`".format(CONFIG_FILE),
        "## Mission: write `{0}`, then `{1}`\n\n### Part 1 -- `{0}`".format(CONFIG_FILE, TESTS_FILE),
        1,
    ).replace(_BUILDER_CLOSING + "\n", "").replace(_BUILDER_CLOSING, "")
    tester = tester.replace(
        "## Mission: write `{0}`".format(TESTS_FILE), "### Part 2 -- `{0}`".format(TESTS_FILE), 1
    ).replace(
        "another agent is writing it from the same specification you are reading",
        "you wrote it in Part 1 from this same specification",
        1,
    ).replace(_TESTER_CLOSING + "\n", "").replace(_TESTER_CLOSING, "")
    order = [
        "### Order",
        "",
        "- Exactly two tool calls, in this order: `write` `{0}`, then `write` `{1}`. "
        "Nothing else -- no `read`, no command, no other file.".format(CONFIG_FILE, TESTS_FILE),
        "- Never edit either file afterwards: the harness runs tsc and vitest and a separate "
        "repair mission fixes any failure.",
        "- End your turn immediately after the second write: no summary, no explanation.",
    ]
    return _fit(builder.rstrip() + "\n\n" + tester.rstrip() + "\n\n" + "\n".join(order), limit=MAX_COMBINED_BRIEF_CHARS)


# ---------------------------------------------------------------------------
# repair / rerun
# ---------------------------------------------------------------------------

#: A testing-library failure whose real cause is a string that differs between
#: the config and the test -- the one failure mode two blind agents produce.
_STRING_MISMATCH = re.compile(
    r"unable to find|multiple elements|no record named|no filter named|no control labelled|"
    r"no stat labelled|toHaveTextContent|toEqual",
    re.IGNORECASE,
)


def repair_brief(
    observation: Dict[str, Any],
    plan: Dict[str, Any],
    spec: Dict[str, Any],
    *,
    attempt: int,
    hint: str = "",
    cap: int = 3,
) -> str:
    """One precise repair mission from one :class:`harness.observe.Observation`.

    ``observation`` is the observation's ``as_dict()``. The brief carries the
    failure verbatim (a paraphrase sends the Repairer looking for the wrong
    thing), the contract it must not break, and the smallest procedure that
    can fix it.
    """
    observation = observation if isinstance(observation, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    spec = spec if isinstance(spec, dict) else {}

    failures, culprit = _failure_report(observation)
    body = [
        "## Mission: repair the build (attempt {0} of {1})".format(attempt, cap),
        "",
        "### What failed",
        "",
    ]
    body.extend(failures)
    if _text(hint):
        body.extend(["", "### Focus", "", _text(hint)])
    body.extend([
        "",
        "### The contract you must not break",
        "",
        "`{0}` must render these exact strings, and `{1}` must query the same ones:".format(
            CONFIG_FILE, TESTS_FILE
        ),
        "",
        _visible_strings(spec, plan),
        "",
        "### Likely culprit",
        "",
        culprit,
        "",
        "### How to fix it",
        "",
        "- Read only the file you are going to edit, then make the smallest edit that fixes "
        "the failure above. Change nothing else -- no refactors, no renames, no new files.",
        "- Never edit {0}.".format(_join(plan.get("leave_alone") or LEAVE_ALONE)),
        "- Do not run any command: the harness runs `tsc` and `vitest` after your turn.",
        "- End your turn immediately after the edit, with no summary.",
    ])
    return _fit("\n".join(body))


def _failure_report(observation: Dict[str, Any]) -> Tuple[List[str], str]:
    """``(lines, culprit)`` for the first failure that actually blocks the build."""
    over_limit = _strings(observation.get("over_limit"))
    if over_limit:
        return (
            ["These files are over the 150-line limit: {0}.".format(_join(over_limit)),
             "Shorten them -- fewer lines per test, no comments, no duplicated setup."],
            "The named file itself; the limit is a hard rule of the contract.",
        )

    tsc_errors = _strings(observation.get("tsc_errors"))
    if tsc_errors and not observation.get("tsc_ok", True):
        files = _files_in(tsc_errors)
        base = "{0} — the file each error names. Fix the types; do not silence them with `any` " \
               "or `@ts-ignore`.".format(_join(files) if files else "The file each error names")
        # Two tsc errors have a specific, counter-intuitive cause the model
        # cannot infer from the message alone (measured 2026-09-04, on two
        # holdout cases). Name the exact fix so a repair does not loop.
        blob = " ".join(tsc_errors)
        if "TS2719" in blob or "Two different types with this name" in blob:
            base = (
                "The inference-identity landmine. `defineApp` must infer the whole object "
                "literal, and every callback must keep the row's exact type. Fix, in order: "
                "(1) remove any `as const` and any type annotation (`: AppConfig`, "
                "`: FieldDef[]`), and un-hoist any `fields` const back inline; "
                "(2) if the error points at an action's `apply`/`compute` that computes a "
                "`select` value, the returned value was widened to `string` -- make it one of "
                "that field's exact options (e.g. annotate the local or the return). "
                "Do NOT add `any` or `@ts-ignore`."
            )
        elif "TS2322" in blob and ("=> " in blob or "row:" in blob or "Row" in blob):
            base = (
                "A `match`/`when`/`available`/`compute`/`apply` value was left as a plain "
                "string; it must be a function. Replace the string with a predicate over the "
                "row, e.g. `\"borrower is not empty\"` -> `(row) => row.borrower !== \"\"`."
            )
        return (
            ["`tsc --noEmit` failed:", "", "```"] + tsc_errors[:30] + ["```"],
            base,
        )

    vitest = observation.get("vitest") if isinstance(observation.get("vitest"), dict) else {}
    failures = [f for f in _dicts(vitest.get("failures")) if _text(f.get("name"))]
    if failures:
        lines = ["{0} test(s) failed:".format(len(failures)), ""]
        blob = ""
        for failure in failures[:8]:
            message = _text(failure.get("message"))[:600]
            blob += message
            lines.append("- **{0}**".format(_text(failure.get("name"))))
            if message:
                lines.extend(["  ```", "  " + message.replace("\n", "\n  "), "  ```"])
        culprit = (
            "A string that differs between the two files: the config's label/option/badge/"
            "stat text, or the test's query for it. Compare both against the contract above "
            "before changing any logic."
            if _STRING_MISMATCH.search(blob)
            else "The behaviour under test: either the config's rule is wrong or the test "
            "expects something the specification does not say."
        )
        return lines, culprit

    if observation.get("build_ran") and observation.get("build_ok") is False:
        tail = _text(observation.get("build_tail"))
        return (
            ["`vite build` failed:", "", "```", tail, "```"],
            "The file the build output names, usually an import or a type it cannot resolve.",
        )

    return (
        ["The observation reports no green build; see the harness log for detail."],
        "Re-read both files against the contract below.",
    )


def rerun_brief(role: str, plan: Dict[str, Any], spec: Dict[str, Any], reason: str) -> str:
    """Re-issue a Builder/Tester mission whose file never arrived.

    The mission is unchanged -- a rerun happens because the session ended
    without writing, not because the brief was wrong -- so it is the same
    brief behind one line saying what went missing.
    """
    body = builder_brief(spec, plan) if role == "builder" else tester_brief(spec, plan)
    head = "The previous attempt did not produce the file ({0}). Do it now, exactly as below.".format(
        _text(reason) or "no file was written"
    )
    return _fit(head + "\n\n" + body)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _sentence(text: str) -> str:
    text = _text(text).rstrip(".")
    return text[:1].upper() + text[1:] if text else ""


def _files_in(lines: List[str]) -> List[str]:
    files: List[str] = []
    for line in lines:
        match = re.match(r"\s*([\w./-]+\.tsx?)\(", line)
        if match and match.group(1) not in files:
            files.append(match.group(1))
    return files


def _fit(text: str, limit: int = MAX_BRIEF_CHARS) -> str:
    """Guarantee the brief stays inside the prefix-cache-friendly budget.

    A brief only overruns when the spec itself is huge (a dozen journeys with
    long steps); clipping the tail is worse than nothing, so the journey
    detail goes first and only then the tail.

    The two halves of a journey line are not worth the same: ``— do:`` is the
    walk, ``— expect:`` is the assertion the Tester has to write. Both live on
    one line, so a single ``[^\\n]*`` takes the expectation with the steps --
    a brief one character over budget lost every assertion in the spec. Drop
    the steps alone first, and only fall back to the blunter cuts.
    """
    if len(text) <= limit:
        return text
    trimmed = re.sub(r" — do: .*?(?= — expect: )", "", text)
    if len(trimmed) <= limit:
        return trimmed
    trimmed = re.sub(r" — do: [^\n]*", "", text)
    if len(trimmed) <= limit:
        return trimmed
    trimmed = re.sub(r" — expect: [^\n]*", "", trimmed)
    if len(trimmed) <= limit:
        return trimmed
    return trimmed[: limit - 3].rstrip() + "..."
