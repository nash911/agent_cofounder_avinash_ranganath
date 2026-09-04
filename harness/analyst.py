"""The Analyst: one ``json_schema`` gateway call from the idea text to ``spec.json``.

Phase 3's version of C4. The Phase-2 Analyst extracted four identity strings;
this one extracts the whole application contract -- fields with their exact
labels and options, filters, badges, stats, actions with rules in plain
English, one journey per observable behaviour, omitted patterns, assumptions
-- because the Builder and the Tester never see each other's file and this
spec is the only thing that keeps their visible strings identical.

Measured (real Berget call, GLM-5.2, thinking off, ``json_schema`` strict,
2026-09-03 23:40, ``scratchpad/p3/probe1``): 444 input / 1,303 output tokens,
42 s for the public idea. ``MAX_TOKENS`` follows that measurement with roughly
2x headroom for a wordier idea.

Two invariants survive from Phase 2:

- **Never blocks the build.** Any failure -- network, parse, disk, a spec too
  thin to build from -- is logged to stderr and swallowed; the caller falls
  back to the single-session path, which needs no spec.
- **Nothing but ``spec.json`` is written**, and only when the spec is usable:
  a half-empty spec is worse than none, because missions mode would build the
  whole application from it.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from .gateway import GatewayClient
from .log import log, warn

#: The scaffold's field kinds (``app-template/src/lib/config-types.ts``).
FIELD_KINDS = ("text", "longtext", "number", "select", "boolean", "date")

#: ``Tone`` in the scaffold. The probe's schema offers "success", which the
#: scaffold does not have -- normalisation maps it to "good" so the Builder
#: can never write a tone that fails ``tsc``.
TONES = ("neutral", "info", "good", "warn", "alert")
_TONE_ALIASES = {
    "success": "good", "good": "good", "info": "info", "neutral": "neutral",
    "warn": "warn", "warning": "warn", "alert": "alert", "danger": "alert", "error": "alert",
}

# ---------------------------------------------------------------------------
# the request: schema + system prompt (both validated against the real model)
# ---------------------------------------------------------------------------


def _s(description: str = "") -> Dict[str, Any]:
    """A schema string. The description is the whole steering mechanism here."""
    return {"type": "string", "description": description} if description else {"type": "string"}


def _b(description: str = "") -> Dict[str, Any]:
    return {"type": "boolean", "description": description} if description else {"type": "boolean"}


def _enum(*values: str) -> Dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _arr(items: Dict[str, Any], description: str = "") -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "array", "items": items}
    if description:
        schema["description"] = description
    return schema


def _obj(properties: Dict[str, Any]) -> Dict[str, Any]:
    """A strict object: ``additionalProperties: false`` and *every* key required.

    Strict ``json_schema`` mode forbids optional keys, so "none" is expressed
    in the value instead -- ``""``, ``[]``, ``false`` -- and each description
    says which. The probe was 4/4 valid under exactly this shape.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


_FIELD = _obj({
    "name": _s("camelCase identifier"),
    "label": _s("Visible form label, Title Case"),
    "kind": _enum(*FIELD_KINDS),
    "required": _b(),
    "options": _arr(_s(), "select only: the fixed choices, Title Case; [] otherwise"),
    "integer": _b("number only: true when only whole numbers make sense (a count), false for a rating, price or measurement"),
    "unit": _s("number only, e.g. 'left'; '' otherwise"),
    "in_form": _b("false when only an action sets this value (a borrower, an owner)"),
    "message": _s("validation message when required/invalid, e.g. 'Title is required.'; '' if not required"),
})

#: A value the app *computes* from other fields rather than storing. Without
#: this primitive the model has nowhere to put "days between two dates" or
#: "quantity times price" but an input field the user is then asked to type --
#: measured 2026-09-04 on a holdout set, where every test that expected the
#: computed value failed against a field the form left at 0.
_DERIVED = _obj({
    "name": _s("camelCase identifier, different from every field name"),
    "label": _s("Visible label in the row, Title Case"),
    "rule": _s("plain-English computation over field names, e.g. 'days between Start and End', "
               "'Quantity times Price', 'days until Due'"),
    "unit": _s("e.g. '£' or 'days'; '' when none"),
})

_CONSTANT = _obj({
    "name": _s("UPPER_SNAKE, e.g. LOW_THRESHOLD"),
    "value": {"type": "number"},
    "comment": _s("one line: the vague phrase in the idea and the value chosen"),
})

_FILTER = _obj({
    "kind": _enum("field", "state"),
    "field": _s("kind=field: the select field to narrow by; '' for state"),
    "id": _s("kind=state: short id; '' for field"),
    "label": _s("kind=state: the filter chip text; kind=field: the 'all' chip text, e.g. 'All kinds'"),
    "rule": _s("kind=state: plain-English predicate over field names, e.g. 'borrower is not empty'; '' for field"),
    "empty_text": _s("kind=state: shown when nothing matches; '' for field"),
})

_BADGE = _obj({
    "id": _s(),
    "rule": _s("plain-English predicate over field names"),
    "tone": _enum("alert", "info", "success", "neutral"),
    "text": _s("what the user reads, with {fieldName} placeholders, e.g. 'Borrowed by {borrower}'"),
})

_STAT = _obj({
    "id": _s(),
    "label": _s("e.g. 'Lent out'"),
    "rule": _s("'count of all rows' | 'count of rows where <predicate>' | 'sum of <field>'"),
    "unit": _s("the unit the figure is read in, e.g. '£'; '' when it is a plain count"),
    "emphasis": _b("true for the one headline figure the idea asks for"),
})

_ACTION = _obj({
    "id": _s(),
    "label": _s("button text, imperative, e.g. 'Lend'"),
    "scope": _enum("row", "all"),
    "available_rule": _s("plain-English predicate; '' when always available"),
    "effect": _s("plain-English field changes, e.g. 'set borrower to the input', 'clear borrower', 'decrease quantity by 1'; describe the field change only -- never mention a prompt, dialog or popup"),
    "input_label": _s("label of the one value the user types; '' when none"),
    "input_required": _b(),
    "confirm_text": _s("warning to confirm before an irreversible action; '' when none"),
    "toast": _s("with {fieldName} placeholders; '' when none"),
})

_JOURNEY = _obj({
    "title": _s("vitest title, lowercase verb phrase, e.g. 'adds a book and shows it in the list'"),
    "kind": _enum("explicit", "implied"),
    "steps": _s("the user actions, using field labels, option values and action labels verbatim"),
    "expect": _s("what the user sees afterwards, verbatim visible text where possible"),
})

#: The probe's schema minus ``implemented_features`` -- the Architect derives
#: that from the journeys and actions for free (``harness.plan.derive_plan``),
#: so paying output tokens for it would be buying the same list twice.
SCHEMA: Dict[str, Any] = _obj({
    "app_name": _s("Title shown in the header, 2-4 words."),
    "tagline": _s("At most 12 words."),
    "summary": _s("One paragraph: what the app does for its user."),
    "noun": _s("The record's singular noun, lowercase, e.g. 'book'."),
    "noun_plural": _s(),
    "fields": _arr(_FIELD),
    "title_field": _s("field name shown as each row's title"),
    "subtitle_fields": _arr(_s()),
    "meta_fields": _arr(_s()),
    "derived": _arr(_DERIVED, "values computed from other fields; [] when none"),
    "constants": _arr(_CONSTANT),
    "filters": _arr(_FILTER),
    "badges": _arr(_BADGE),
    "stats": _arr(_STAT),
    "actions": _arr(_ACTION),
    "journeys": _arr(_JOURNEY),
    "omitted_patterns": _arr(_obj({"pattern": _s(), "reason": _s()})),
    "assumptions": _arr(_s(), "each ambiguity and the decision made, one sentence each"),
})

#: The probe's prompt, verbatim. Every sentence earns its tokens: the mapping
#: rules are what turn "which ones are out" into a state filter rather than a
#: free-text note, and the journey rule is what stops the model pruning the
#: implied behaviours the scorer looks for.
SYSTEM_PROMPT = """You turn a non-technical product idea into a precise specification for a single-record-type browser app rendered from one configuration: fields, filters, badges, stats and actions over one list of records. Extract only what the idea states or implies; add nothing else.
Mapping rules: each attribute named -> one field (a quantity is number, a fixed set of choices is select with Title Case options; a value only set by an action, like a borrower, is a field with in_form=false). 'which ones are X now' -> a state filter. 'how many are X' -> a stat (emphasis on the headline one). 'one type at a time' -> a field filter on the select. Anything that should stand out -> a badge whose text is what the user reads. Any verb other than add/edit/delete -> an action. Any vague threshold ('a couple', 'running low', 'overdue') -> a constant plus one assumption.
Computed values: a value worked out from other fields -- a difference of two dates, the days until or since a date, a quantity times a price -- is a derived entry, never an input field the user types. Write its rule over the field names ('days between Start and End', 'Quantity times Price'), and phrase every date rule as 'days until/since/between' -- in a derived, filter or badge rule alike.
Scope: an effect applied to every record at once (a reset, a clear-all, an archive-all) is one action with scope 'all'; anything done to the record the user picked is scope 'row'.
Units: a currency symbol is a unit ('£'), on the number field and on every stat or derived value that reads as money; a counted thing keeps its word unit ('pts', 'days').
Journeys: one per observable behaviour the idea states or implies, in the order a user would meet them; always include add, edit, delete, each filter, each stat, each derived value, each action, refresh persistence, and rejecting an empty required field. Never omit an implied journey merely to simplify. Patterns the idea does not imply go in omitted_patterns with the reason, not in journeys.
Style: journey titles are lowercase verb phrases ('lends a book and shows the borrower'); steps and expectations quote the field labels, option values, badge texts and action labels exactly as you named them; an action's effect names only the field change ('set borrower to the input'), never a prompt or dialog; a stat's rule is 'count of all rows', 'count of rows where <predicate>' or 'sum of <field>'; a badge that announces a value shows it ('Lent to {borrower}') and its text differs from every stat label and filter label.
Keep every string short. No commentary."""

#: 1,303 output tokens measured on the public idea; 2,500 covers a wordier one
#: without letting a runaway generation eat the Builder's wall clock.
MAX_TOKENS = 2500


def build_system_prompt(journeys_md: str = "") -> str:
    """``SYSTEM_PROMPT`` plus the public coverage checklist, when supplied.

    ``contract-public/journeys.md`` is guidance about *which behaviours to
    cover*, which is an Analyst concern: mission sessions never see that file
    (they get a mission, not the contract), so the checklist only reaches the
    build through the journeys the Analyst writes down.
    """
    checklist = _coverage_checklist(journeys_md)
    if not checklist:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + "\nCoverage checklist -- add a journey for each item the idea states or implies:\n"
        + checklist
    )


def _coverage_checklist(journeys_md: str) -> str:
    """The numbered "Behaviors to implement and test when implied" list, or ""."""
    if not isinstance(journeys_md, str) or not journeys_md.strip():
        return ""
    lines: List[str] = []
    in_section = False
    for raw in journeys_md.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if lines:
                break  # the list is over; later headings are run/report guidance
            in_section = "behavior" in line.lower() or "behaviour" in line.lower()
            continue
        if in_section and re.match(r"^\d+[.)]\s+", line):
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# normalisation: the model's JSON -> something the Architect can build from
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _words(text: str) -> List[str]:
    """Split an identifier or phrase into words, camelCase boundaries included."""
    words: List[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", text or ""):
        if part:
            words.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part))
    return words


def _camel(text: str) -> str:
    words = _words(text)
    if not words:
        return ""
    name = words[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in words[1:])
    return "f" + name if name[0].isdigit() else name


def _upper_snake(text: str) -> str:
    return "_".join(w.upper() for w in _words(text))


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")


def _unique(name: str, used: List[str]) -> str:
    """``name``, or ``name2``/``name3``... when it is already taken. Mutates ``used``."""
    if name not in used:
        used.append(name)
        return name
    for suffix in range(2, 100):
        candidate = "{0}{1}".format(name, suffix)
        if candidate not in used:
            used.append(candidate)
            return candidate
    return ""


def _normalized_title(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def normalize_spec(obj: Any) -> Dict[str, Any]:
    """Repair the model's spec into one the Architect can render mechanically.

    Pure and total: never raises, never calls out, ignores unknown keys. Every
    rule here exists because the alternative is a ``tsc`` error or a silently
    wrong application -- a duplicate field name overwrites a column, a
    ``select`` with one option is a text box the tests cannot choose in, a
    ``titleField`` naming a field that does not exist does not compile.
    """
    src: Dict[str, Any] = obj if isinstance(obj, dict) else {}

    noun = _text(src.get("noun")).lower() or "record"
    noun_plural = _text(src.get("noun_plural")).lower() or (noun + "s")

    fields, names = _normalize_fields(src.get("fields"))
    title_field = _pick_title_field(_camel(_text(src.get("title_field"))), fields)
    _force_title_testable(fields, title_field)
    others = [name for name in names if name != title_field]

    return {
        "app_name": _text(src.get("app_name")),
        "tagline": _text(src.get("tagline")),
        "summary": _text(src.get("summary")),
        "noun": noun,
        "noun_plural": noun_plural,
        "storage_key": (_slug(noun_plural) or "records") + ".v1",
        "fields": fields,
        "title_field": title_field,
        "subtitle_fields": _keep_known(src.get("subtitle_fields"), others),
        "meta_fields": _keep_known(src.get("meta_fields"), others),
        "derived": _normalize_derived(src.get("derived"), names),
        "constants": _normalize_constants(src.get("constants")),
        "filters": _normalize_filters(src.get("filters"), fields),
        "badges": _normalize_badges(src.get("badges")),
        "stats": _normalize_stats(src.get("stats")),
        "actions": _normalize_actions(src.get("actions")),
        "journeys": _normalize_journeys(src.get("journeys")),
        "omitted_patterns": _normalize_omitted(src.get("omitted_patterns")),
        "assumptions": _unique_strings(src.get("assumptions")),
    }


def _normalize_fields(raw_fields: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    fields: List[Dict[str, Any]] = []
    used: List[str] = []
    for raw in _list(raw_fields):
        if not isinstance(raw, dict):
            continue
        name = _camel(_text(raw.get("name")))
        label = _text(raw.get("label"))
        if not name or not label:
            continue  # a nameless or unlabelled field can be neither rendered nor queried
        kind = _text(raw.get("kind")).lower()
        if kind not in FIELD_KINDS:
            kind = "text"
        options = _unique_strings(raw.get("options"))
        if kind == "select" and len(options) < 2:
            kind = "text"  # a one-option "choice" is a text box with extra steps
        if kind != "select":
            options = []
        required = bool(raw.get("required"))
        message = _text(raw.get("message"))
        if required and not message:
            # The "rejects an empty required field" journey asserts on this
            # exact string; blank leaves the Tester nothing to query for.
            message = "{0} is required.".format(label)
        fields.append({
            "name": _unique(name, used),
            "label": label,
            "kind": kind,
            "required": required,
            "options": options,
            # Only whole numbers by default: a count is what most ideas mean,
            # and the scaffold rejects a decimal with "<Label> must be a whole
            # number." -- a wrong default here is a red test with an invisible
            # cause, so the Analyst gets to say otherwise.
            "integer": bool(raw.get("integer", True)) if kind == "number" else False,
            "unit": _text(raw.get("unit")) if kind == "number" else "",
            "in_form": bool(raw.get("in_form", True)),
            "message": message if required else "",
        })
    return fields, used


def _force_title_testable(fields: List[Dict[str, Any]], title_field: str) -> None:
    """Guarantee the "reject an empty required field" journey is testable.

    That journey leaves a required field blank through the form. A blank only
    works on a `text`/`longtext` field: a `select` always holds a valid option
    (blanking one throws in testing-library before validation runs), and a
    blank `date`/`number` is a fiddlier assertion. Measured 2026-09-04
    (a holdout case): when the first required field was a `select` and no
    required text field existed, the Tester blanked the select and the run
    burned a whole repair round. The title field is always shown and is the
    natural "name": make it a required text field so a text target always
    exists.
    """
    for field in fields:
        if field.get("name") != title_field:
            continue
        if field.get("kind") not in ("text", "longtext"):
            field["kind"] = "text"
            field["options"] = []
            field["unit"] = ""
            field["integer"] = False
        if not field.get("required"):
            field["required"] = True
        if not _text(field.get("message")):
            field["message"] = "{0} is required.".format(field.get("label") or "This field")
        field["in_form"] = True
        return


def _pick_title_field(candidate: str, fields: List[Dict[str, Any]]) -> str:
    names = [field["name"] for field in fields]
    if candidate in names:
        return candidate
    for field in fields:
        if field["kind"] == "text":
            return field["name"]
    return names[0] if names else ""


def _keep_known(raw: Any, known: List[str]) -> List[str]:
    kept: List[str] = []
    for value in _list(raw):
        name = _camel(_text(value))
        if name in known and name not in kept:
            kept.append(name)
    return kept


def _unique_strings(raw: Any) -> List[str]:
    kept: List[str] = []
    for value in _list(raw):
        text = _text(value)
        if text and text not in kept:
            kept.append(text)
    return kept


def _normalize_derived(raw_derived: Any, field_names: List[str]) -> List[Dict[str, str]]:
    """Computed values, kept strictly apart from the stored fields.

    A derived value is never stored and never on the form, so a name that
    collides with a field's would put two different meanings on one key of the
    row -- the config would read the stored column where the app renders the
    computation. Dropping the collider is the only repair that cannot lie: the
    value the idea asked for stays available under the field it came from.
    A derived entry with no rule is nothing to compute, so it goes too.
    """
    derived: List[Dict[str, str]] = []
    used: List[str] = list(field_names)  # so a de-duplicated name cannot land on a field either
    for raw in _list(raw_derived):
        if not isinstance(raw, dict):
            continue
        name = _camel(_text(raw.get("name")))
        label = _text(raw.get("label"))
        rule = _text(raw.get("rule"))
        if not name or not label or not rule or name in field_names:
            continue
        derived.append({
            "name": _unique(name, used),
            "label": label,
            "rule": rule,
            "unit": _text(raw.get("unit")),
        })
    return derived


def _normalize_constants(raw_constants: Any) -> List[Dict[str, Any]]:
    constants: List[Dict[str, Any]] = []
    used: List[str] = []
    for raw in _list(raw_constants):
        if not isinstance(raw, dict):
            continue
        name = _upper_snake(_text(raw.get("name")))
        value = raw.get("value")
        # ``bool`` is an ``int`` in Python; a boolean threshold is meaningless.
        if not name or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if name[0].isdigit():
            name = "N_" + name
        constants.append(
            {"name": _unique(name, used), "value": value, "comment": _text(raw.get("comment"))}
        )
    return constants


def _normalize_filters(raw_filters: Any, fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selects = [field["name"] for field in fields if field["kind"] == "select"]
    filters: List[Dict[str, Any]] = []
    used_ids: List[str] = []
    used_fields: List[str] = []
    for raw in _list(raw_filters):
        if not isinstance(raw, dict):
            continue
        field = _camel(_text(raw.get("field")))
        kind = _text(raw.get("kind")).lower()
        if kind not in ("field", "state"):
            kind = "field" if field in selects else "state"
        if kind == "field":
            # A field filter renders that select's own options as chips; over a
            # non-select (or twice over the same one) there is nothing to draw.
            if field not in selects or field in used_fields:
                continue
            used_fields.append(field)
            filters.append({
                "kind": "field", "field": field, "id": "",
                "label": _text(raw.get("label")) or "All", "rule": "", "empty_text": "",
            })
            continue
        identifier = _camel(_text(raw.get("id")))
        label = _text(raw.get("label"))
        rule = _text(raw.get("rule"))
        if not identifier or not label or not rule:
            continue  # a state filter without a predicate cannot be written
        filters.append({
            "kind": "state", "field": "", "id": _unique(identifier, used_ids), "label": label,
            "rule": rule, "empty_text": _text(raw.get("empty_text")),
        })
    return filters


def _normalize_badges(raw_badges: Any) -> List[Dict[str, Any]]:
    badges: List[Dict[str, Any]] = []
    used: List[str] = []
    for raw in _list(raw_badges):
        if not isinstance(raw, dict):
            continue
        identifier = _camel(_text(raw.get("id")))
        rule = _text(raw.get("rule"))
        text = _text(raw.get("text"))
        if not identifier or not rule or not text:
            continue
        badges.append({
            "id": _unique(identifier, used),
            "rule": rule,
            "tone": _TONE_ALIASES.get(_text(raw.get("tone")).lower(), "neutral"),
            "text": text,
        })
    return badges


def _normalize_stats(raw_stats: Any) -> List[Dict[str, Any]]:
    stats: List[Dict[str, Any]] = []
    used: List[str] = []
    emphasised = False
    for raw in _list(raw_stats):
        if not isinstance(raw, dict):
            continue
        identifier = _camel(_text(raw.get("id")))
        label = _text(raw.get("label"))
        rule = _text(raw.get("rule"))
        if not identifier or not label or not rule:
            continue
        # "The one headline figure": a second emphasis is no emphasis at all.
        emphasis = bool(raw.get("emphasis")) and not emphasised
        emphasised = emphasised or emphasis
        stats.append({
            "id": _unique(identifier, used),
            "label": label,
            "rule": rule,
            # A money figure reads "£60", not "60": the tile formats the value
            # with its unit exactly as a number field does.
            "unit": _text(raw.get("unit")),
            "emphasis": emphasis,
        })
    return stats


def _normalize_actions(raw_actions: Any) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    used: List[str] = []
    for raw in _list(raw_actions):
        if not isinstance(raw, dict):
            continue
        identifier = _camel(_text(raw.get("id")))
        label = _text(raw.get("label"))
        effect = _text(raw.get("effect"))
        if not identifier or not label or not effect:
            continue
        input_label = _text(raw.get("input_label"))
        # "row" is the safe default: a per-row button applied to one record is a
        # smaller wrong answer than a bulk button that rewrites every record.
        scope = _text(raw.get("scope")).lower()
        actions.append({
            "id": _unique(identifier, used),
            "label": label,
            "scope": scope if scope in ("row", "all") else "row",
            "available_rule": _text(raw.get("available_rule")),
            "effect": effect,
            "input_label": input_label,
            "input_required": bool(raw.get("input_required")) if input_label else False,
            "confirm_text": _text(raw.get("confirm_text")),
            "toast": _text(raw.get("toast")),
        })
    return actions


def _normalize_journeys(raw_journeys: Any) -> List[Dict[str, Any]]:
    journeys: List[Dict[str, Any]] = []
    seen: List[str] = []
    for raw in _list(raw_journeys):
        if not isinstance(raw, dict):
            continue
        title = _text(raw.get("title"))
        key = _normalized_title(title)
        if not key or key in seen:
            continue  # two tests with the same title are one test to the runner
        seen.append(key)
        kind = _text(raw.get("kind")).lower()
        journeys.append({
            "title": title,
            "kind": kind if kind in ("explicit", "implied") else "explicit",
            "steps": _text(raw.get("steps")),
            "expect": _text(raw.get("expect")),
        })
    return journeys


def _normalize_omitted(raw_omitted: Any) -> List[Dict[str, str]]:
    omitted: List[Dict[str, str]] = []
    for raw in _list(raw_omitted):
        if not isinstance(raw, dict):
            continue
        pattern = _text(raw.get("pattern"))
        if pattern:
            omitted.append({"pattern": pattern, "reason": _text(raw.get("reason"))})
    return omitted


def unusable_reasons(spec: Any) -> List[str]:
    """Why ``spec`` cannot drive a build -- empty when it can."""
    if not isinstance(spec, dict):
        return ["not an object"]
    reasons: List[str] = []
    fields = spec.get("fields") or []
    if not fields:
        reasons.append("no usable fields")
    names = [f.get("name") for f in fields if isinstance(f, dict)]
    if not spec.get("title_field") or spec.get("title_field") not in names:
        reasons.append("no valid title_field")
    if not spec.get("journeys"):
        reasons.append("no journeys")
    if not spec.get("app_name"):
        reasons.append("no app_name")
    return reasons


def spec_is_usable(spec: Any) -> bool:
    """Whether missions mode can build an application from ``spec`` unaided."""
    return not unusable_reasons(spec)


# ---------------------------------------------------------------------------
# the call
# ---------------------------------------------------------------------------


def run_analyst(
    client: GatewayClient,
    idea_text: str,
    harness_dir: Union[str, pathlib.Path],
    deadline: Optional[float] = None,
    journeys_md: str = "",
) -> Optional[Dict[str, Any]]:
    """Writes ``<harness_dir>/spec.json`` and returns it, or ``None`` on any failure.

    ``deadline`` is an optional ``time.monotonic()`` instant bounding the whole
    call (including retries and backoff) -- see ``harness.__main__.run_analyst``
    for how it is derived from the harness's own wall-clock budget. ``None``
    means "no bound", matching :meth:`GatewayClient.chat`'s own default.

    ``journeys_md`` is ``contract-public/journeys.md``'s text when the caller
    has it; only its coverage checklist is used (:func:`build_system_prompt`).

    ``None`` is a complete answer rather than an error path: the caller falls
    back to the single-session mode, which needs no spec. The returned dict is
    the *normalised* spec -- exactly the bytes written to ``spec.json``.
    """
    try:
        messages = [
            {"role": "system", "content": build_system_prompt(journeys_md)},
            {"role": "user", "content": idea_text},
        ]
        obj, result = client.json_schema(
            messages,
            name="app_spec",
            schema=SCHEMA,
            label="analyst",
            max_tokens=MAX_TOKENS,
            deadline=deadline,
        )
        if obj is None:
            warn(
                "analyst · no usable spec ({0} attempts, status {1}, error {2})".format(
                    result.attempts, result.status, result.error
                )
            )
            return None
        if not isinstance(obj, dict):
            warn("analyst · schema response was not a JSON object: {0!r}".format(type(obj)))
            return None

        spec = normalize_spec(obj)
        reasons = unusable_reasons(spec)
        if reasons:
            warn("analyst · spec unusable ({0}); continuing without one".format(", ".join(reasons)))
            return None

        harness_path = pathlib.Path(harness_dir)
        harness_path.mkdir(parents=True, exist_ok=True)
        spec_path = harness_path / "spec.json"
        spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log(
            "analyst",
            "spec.json written ({0} field(s), {1} journey(s), {2} attempt(s))".format(
                len(spec["fields"]), len(spec["journeys"]), result.attempts
            ),
        )
        return spec
    except Exception as exc:  # noqa: BLE001 -- the Analyst must never block the build
        warn("analyst · unexpected failure: {0}: {1}".format(type(exc).__name__, exc))
        return None
