"""The strings a mission may query, and the spec primitives both briefs share.

Split out of :mod:`harness.plan` when the cheat sheet outgrew it: the Architect
renders *what to build*, this module renders *what the built app will say*, and
both the Tester's brief and every Repairer's brief are that same block (they
have to be -- a Repairer told a different string from the Tester would fix the
config to match a test that queries something else).

Every line here exists because a measured run got it wrong: the list is sorted,
the chip carries its count, the badge shows a value rather than a placeholder,
a validation message renders twice, ``removeRecord`` confirms its own dialog
and ``reload()`` resets the filter. None of that is in the spec, and a Tester
that reads only the spec writes a red test with an invisible cause.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

#: A prefix collision is rare and one line each is enough to steer around it;
#: a spec that produced a dozen would be spending the brief's budget on them.
MAX_SHADOWED_NOTES = 2


# ---------------------------------------------------------------------------
# spec primitives (shared with harness.plan)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _dicts(value: Any) -> List[Dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> List[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _join(values: Any) -> str:
    return ", ".join(_strings(values) if isinstance(values, list) else [str(values)])


def _label_of(spec: Dict[str, Any], name: str) -> str:
    for field in _dicts(spec.get("fields")):
        if _text(field.get("name")) == name:
            return _text(field.get("label"))
    return name


def _row_actions(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The actions that act on the one record the user picked."""
    return [a for a in _dicts(spec.get("actions")) if _text(a.get("scope")).lower() != "all"]


def _bulk_actions(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The actions whose effect applies to every record at once."""
    return [a for a in _dicts(spec.get("actions")) if _text(a.get("scope")).lower() == "all"]


#: An effect that takes records off the list rather than changing them. "clear
#: borrower" clears a field; "clear the list" / "delete all rows" / "remove every
#: sold item" delete rows -- and the scaffold's one way to say so is an `apply`
#: that returns `null`. Measured 2026-09-04 (a holdout case): a "Clear freezer"
#: meant delete-all, the Builder invented a `_deleted` field (tsc error), then
#: zeroed a count instead, and two repair rounds went on a shape the config
#: could not express.
#: The verb alone is not enough: "remove the borrower" clears a field. It is a
#: deletion when what follows the verb is the record itself or all of them.
_REMOVAL = re.compile(
    r"\b(delete|remove|discard|drop)s?\s+(the\s+|this\s+|that\s+)?"
    r"(row|record|entry|item|it|this|them|all|every\w*|each|from\b)"
    r"|\bclears? (all|every\w*|the (whole )?list)\b",
    re.IGNORECASE,
)


def _removes(effect: str) -> bool:
    """Whether an action's effect deletes rows instead of patching them."""
    return bool(_REMOVAL.search(_text(effect)))


def _formatted_number(value: str, unit: str) -> str:
    """One number as the app prints it -- the scaffold's own rule, in Python.

    A currency symbol (or any single non-alphanumeric mark) reads before the
    value with no space, everything else after it with one. Measured
    2026-09-04: the config rendered "40 £" while the test written from the same
    spec expected "£40", and neither file was wrong about the spec -- only
    about each other. Rendering the example here makes the two agree.
    """
    unit = _text(unit)
    if not unit:
        return value
    if len(unit) == 1 and not unit.isalnum():
        return unit + value
    return "{0} {1}".format(value, unit)


def _compute_rule(rule: str) -> str:
    """A stat rule as an unambiguous instruction.

    The Analyst often writes a stat's rule as the bare predicate it counts
    ("borrower is not empty") rather than the "count of rows where ..." form
    the schema asks for; both briefs must say the same, countable thing or the
    Builder writes a boolean where the tile expects a number.
    """
    rule = _text(rule)
    if not rule:
        return "count of all rows"
    if re.match(r"(count|sum|total|average|number|how many)\b", rule, re.IGNORECASE):
        return rule
    return "count of rows where {0}".format(rule)


def _copy(spec: Dict[str, Any]) -> Dict[str, str]:
    """The scaffold's ``copy`` block. Derived, so both briefs say the same words."""
    noun = _text(spec.get("noun")) or "record"
    plural = _text(spec.get("noun_plural")) or (noun + "s")
    return {
        "title": _text(spec.get("app_name")) or "Records",
        "tagline": _text(spec.get("tagline")),
        "noun": noun,
        "nounPlural": plural,
        "addLabel": "Add {0}".format(noun),
        "emptyTitle": "No {0} yet".format(plural),
        "emptyBody": "Add your first {0} with the form.".format(noun),
    }


# ---------------------------------------------------------------------------
# the cheat sheet
# ---------------------------------------------------------------------------


def _visible_strings(spec: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """Every string the Builder is being told to render, as a query cheat sheet."""
    labels = plan.get("labels") if isinstance(plan.get("labels"), dict) else {}
    copy = labels.get("copy") if isinstance(labels.get("copy"), dict) else _copy(spec)
    fields = _dicts(spec.get("fields"))
    lines: List[str] = []

    form = [_text(f.get("label")) for f in fields if f.get("in_form", True) and _text(f.get("label"))]
    hidden = [_text(f.get("label")) for f in fields if not f.get("in_form", True) and _text(f.get("label"))]
    lines.append("- Form labels (what `addRecord`/`editRecord` fill): {0}".format(_join(form)))
    # The empty-required journey must blank a TEXT field: `fill` selects a
    # select's value, so `""` throws "Value \"\" not found in options" before
    # validation runs (measured 2026-09-04, a holdout case). normalize_spec
    # guarantees the title field is a required text field, so one always exists.
    req_text = next(
        (_text(f.get("label")) for f in fields
         if f.get("required") and f.get("kind") in ("text", "longtext")
         and f.get("in_form", True) and _text(f.get("label"))),
        "",
    )
    if req_text:
        lines.append(
            "- To test an empty required field, blank the required TEXT field \"{0}\" "
            "(`addRecord(user, {{ \"{0}\": \"\" }})`) and assert \"{0}: {0} is required.\". "
            "Never pass \"\" for a select, date or number field -- omit the key to keep its "
            "initial value.".format(req_text)
        )
    if hidden:
        lines.append(
            "- Never on the form, only an action sets them: {0}".format(_join(hidden))
        )
    for field in fields:
        options = _strings(field.get("options"))
        if options:
            lines.append(
                "- \"{0}\" options: {1}".format(_text(field.get("label")), _join(options))
            )
    for field in fields:
        if _text(field.get("message")):
            # The message is rendered twice -- as the field's own `role="alert"`
            # and inside the form's problem summary as "<Label>: <message>" --
            # so a regex query matches both and testing-library throws.
            lines.append(
                "- Validation message for \"{0}\": \"{1}\" — it also appears in the form's "
                "problem summary as \"{0}: {1}\", so assert that exact string, never a "
                "regex".format(_text(field.get("label")), _text(field.get("message")))
            )
    filters = _dicts(spec.get("filters"))
    plural = copy.get("nounPlural", "records")
    for entry in filters:
        if _text(entry.get("kind")) == "field":
            field_name = _text(entry.get("field"))
            field = next((f for f in fields if _text(f.get("name")) == field_name), {})
            # A select has a chip per option; any other field has a chip only
            # for a value some row actually holds -- a value nobody entered
            # has no chip, so `chooseFilter` on it throws (measured
            # 2026-09-04, a holdout case: a test chose a room no plant had).
            chips = (
                "plus one chip per option above" if _strings(field.get("options"))
                else "plus one chip per distinct value the rows hold -- a value no row has gets no chip"
            )
            lines.append(
                "- Filter chips for \"{0}\": \"{1}\" {2}; a chip that matches no row shows "
                "\"Nothing matches this view\" and \"{3}\"".format(
                    _label_of(spec, field_name), _text(entry.get("label")), chips,
                    _text(entry.get("empty_text")) or "No {0} in this view.".format(plural),
                )
            )
        else:
            line = "- Filter chip \"{0}\" ({1})".format(_text(entry.get("label")), _text(entry.get("rule")))
            if _text(entry.get("empty_text")):
                line += "; with no match the list shows \"Nothing matches this view\" and \"{0}\"".format(
                    _text(entry.get("empty_text"))
                )
            lines.append(line)
    lines.extend(_filter_notes(spec, filters))
    for badge in _dicts(spec.get("badges")):
        text = _text(badge.get("text"))
        lines.append(
            "- Badge text \"{0}\" when {1}{2}".format(
                text, _text(badge.get("rule")), _badge_example(text)
            )
        )
    # A computed value is not a field, so nothing above mentions it -- and a
    # Tester that has not been told it exists writes the row assertion without
    # it, or worse, tries to type it into the form.
    for entry in _dicts(spec.get("derived")):
        label = _text(entry.get("label"))
        example = _formatted_number("40", _text(entry.get("unit")))
        lines.append(
            "- Derived value \"{0}\" = {1} — the app computes it from the other fields: never "
            "on the form, never in `addRecord`. The row shows it as \"{0} <value>\" (with a "
            "value of 40 the row reads \"{0} {2}\"), so assert it with "
            "`expectRow(title, \"{0} {2}\")` for the value your own data implies.".format(
                label, _text(entry.get("rule")), example
            )
        )
    for stat in _dicts(spec.get("stats")):
        line = "- Stat tile \"{0}\" = {1} — read it with `stat(\"{0}\")`, which returns a string".format(
            _text(stat.get("label")), _compute_rule(_text(stat.get("rule")))
        )
        unit = _text(stat.get("unit"))
        if unit:
            line += " rendered with its unit, e.g. \"{0}\" for a total of 60".format(
                _formatted_number("60", unit)
            )
        lines.append(line)
    for action in _row_actions(spec):
        parts = ["- Row button \"{0}\"".format(_text(action.get("label")))]
        for template, value in (
            (", asks for \"{0}\"", _text(action.get("input_label"))),
            (", confirms with \"{0}\"", _text(action.get("confirm_text"))),
            (", toast \"{0}\"", _text(action.get("toast"))),
            (" — shown only when {0}", _text(action.get("available_rule"))),
        ):
            if value:
                parts.append(template.format(value))
        if _removes(_text(action.get("effect"))):
            parts.append(
                " — it deletes the record: assert `expectNoRow(title)` afterwards, never a "
                "changed value on the row"
            )
        lines.append("".join(parts))
    # A bulk button is not in the row, so `runAction` (which needs a row title)
    # cannot reach it; its own helper takes only the label.
    for action in _bulk_actions(spec):
        label = _text(action.get("label"))
        line = (
            "- Bulk button \"{0}\" applies to every record -- call "
            "`await runBulkAction(user, \"{0}\")`".format(label)
        )
        if _text(action.get("confirm_text")):
            line += (
                ", which confirms \"{0}\" itself, so never call `confirmDialog` after "
                "it".format(_text(action.get("confirm_text")))
            )
        if _removes(_text(action.get("effect"))):
            line += (
                "; afterwards every record it applies to is GONE from the list -- assert "
                "`expectNoRow(title)` for each -- and the toast reads \"{0} applied to N "
                "records\" with N the number removed.".format(label)
            )
        else:
            line += (
                "; afterwards every row has changed, and the toast reads \"{0} applied to N "
                "records\" with N the number of records.".format(label)
            )
        lines.append(line)
    lines.append(
        "- Header \"{0}\", add button \"{1}\", empty state \"{2}\" / \"{3}\"".format(
            copy.get("title", ""), copy.get("addLabel", ""),
            copy.get("emptyTitle", ""), copy.get("emptyBody", ""),
        )
    )
    # `_config_outline` hard-codes `sort: {field: titleField, direction: "asc"}`,
    # so the natural "add Zeta, add Alpha, expect [Zeta, Alpha]" assertion fails
    # on a list the Tester never chose to sort.
    lines.append(
        "- Rows are sorted by \"{0}\" ascending, so `rowTitles()` returns them "
        "alphabetically, not in the order you added them.".format(
            _label_of(spec, _text(spec.get("title_field")))
        )
    )
    # Measured 2026-09-04 (a holdout case): the config printed "40 £" and the
    # test asserted "£40". Both files read the same spec; neither was told how
    # a unit renders, so the run failed on a string nobody chose.
    lines.append(
        "- A number is always printed with its unit the same way: a currency symbol comes "
        "before the value with no space (\"£40\"), any other unit after it with a space "
        "(\"40 pts\") — in a row, in a derived value and in a stat tile (\"£60\"). A boolean "
        "value reads \"Yes\" or \"No\", never \"true\"/\"false\"."
    )
    if _needs_dates(spec):
        lines.append(
            "- Date values are \"yyyy-mm-dd\" strings. Anything relative to today must be "
            "computed in the test, never hard-coded: "
            "`const iso = (offsetDays: number) => new Date(Date.now() + offsetDays * 86400000)"
            ".toISOString().slice(0, 10);` then `iso(0)` is today, `iso(3)` is three days "
            "ahead and `iso(-3)` three days ago."
        )
        # Measured 2026-09-04 (a holdout case): every date rule read a date the
        # form did not offer, so no test could put a record into the "due"
        # state and the Tester asserted it on a record added a moment ago.
        date_labels = [
            _text(f.get("label")) for f in fields
            if _text(f.get("kind")) == "date" and _text(f.get("label"))
        ]
        if date_labels:
            lines.append(
                "- Date fields ({0}) are on the form and start at today (`iso(0)`) when "
                "`addRecord` omits them, so a record added with the default is 0 days old -- not "
                "\"ago\", overdue or stale unless its rule already says so at 0. Every rule that "
                "counts days since/until "
                "a date reads that stored date, so to put a record into such a state pass the "
                "date in `addRecord` (`\"{1}\": iso(-9)` for nine days ago, `iso(9)` for nine "
                "days ahead) and assert the state the rule then implies for that exact "
                "number.".format(_join(date_labels), date_labels[0])
            )
    # Three scaffold behaviours the journey wording actively pushes the other
    # way: journeys say "delete; confirm" (there is no confirmation), and
    # "after a refresh, filters unchanged" (`reload` remounts the app, so the
    # filter and the search box reset).
    lines.append(
        "- Scaffold behaviour you never configure: delete is one click plus a "
        "\"<title> deleted.\" toast with an \"Undo\" button — `removeRecord` clicks Delete and "
        "confirms any dialog itself, so never call `confirmDialog` after it; `reload()` keeps "
        "the stored records but resets the search box and the active filter; corrupt storage "
        "shows a \"could not be read\" notice."
    )
    # Measured 2026-09-04 (a holdout case): a test added a record and asserted
    # `expectNoRow` on it in the same breath, which can only ever fail.
    lines.append(
        "- `expectNoRow(title)` asserts a record is ABSENT. Never call it for a record you "
        "just added — assert `expectRow(title, ...)` or `rowTitles()` instead. It belongs "
        "after a delete, or when a filter or a search hides the row."
    )
    # Measured 2026-09-04 (run python-mission-a): the badge, the stat tile and
    # a filter chip all read "Lent out", and `screen.getByText("Lent out")`
    # threw "multiple elements" -- one whole repair round for a query the
    # helpers already scope correctly.
    lines.append(
        "- Never query a badge, stat or chip string with `screen.getByText`/`getAllByText`: "
        "the same words can sit on a badge, a stat tile and a chip at once. Assert a badge "
        "with `expectRow(title, text)`, a stat with `stat(label)`, a chip with "
        "`await chooseFilter(user, label)`, a toast or validation message with "
        "`screen.getByText(exact string)`."
    )
    # Measured 2026-09-04 (a third-party case): `not.toContain("Sold")` on a row
    # whose "Mark sold" button and "Sold" chip were still on the page.
    lines.append(
        "- Never assert that a word is ABSENT from a row or the page (`not.toContain`, "
        "`queryByText(...)` being null): the same word is a chip name, a button label or a "
        "field label too, so it is still there. Assert the new state positively -- "
        "`expectRow(title, <the text the rule now shows>)`, `rowTitles()` after a filter, "
        "`stat(label)` -- and use `expectNoRow` only for a record that is deleted or "
        "filtered out."
    )
    return "\n".join(lines)


def _needs_dates(spec: Dict[str, Any]) -> bool:
    """Whether any rule in this spec is relative to today.

    A date field is the obvious case; a rule that counts days is the other one
    (a filter for "the next N days" is relative even when the Analyst wrote the
    field as text), and both make a hard-coded calendar date in the test a
    failure that only shows up later.
    """
    if any(_text(field.get("kind")) == "date" for field in _dicts(spec.get("fields"))):
        return True
    rules = [_text(entry.get("rule")) for entry in _dicts(spec.get("derived"))]
    rules.extend(_text(entry.get("rule")) for entry in _dicts(spec.get("filters")))
    rules.extend(_text(entry.get("rule")) for entry in _dicts(spec.get("badges")))
    return any(re.search(r"\bdays?\b", rule, re.IGNORECASE) for rule in rules)


def _badge_example(text: str) -> str:
    """The placeholder clause of a badge line: ``{name}`` is not what shows.

    A badge's text reaches the user with the row's value substituted, and a
    Tester that asserts the literal ``"Out: {borrower}"`` gets an opaque
    "Unable to find text" that reads like a string mismatch in the config.
    """
    match = re.search(r"\{(\w+)\}", text or "")
    if not match:
        return ""
    name = match.group(1)
    # Every placeholder is substituted, not just the named one: a half-literal
    # example ("Out: Sam ({days})") is a string the app never shows either.
    example = re.sub(r"\{\w+\}", "Sam", text or "")
    return " — substitute the row's own value for each {{name}}: with {0} = \"Sam\" the row " \
           "reads \"{1}\"".format(name, example)


def _filter_notes(spec: Dict[str, Any], filters: List[Dict[str, Any]]) -> List[str]:
    """How a chip is actually named, and which label clears every filter.

    ``FilterBar`` renders each chip as ``"<label> (<count>)"`` and
    ``chooseFilter`` matches a string by ``startsWith`` but a regex by
    ``test``, so an anchored regex (``/^Out$/``) -- the natural way to avoid
    matching "Out of print" -- never matches anything. ``filterOptions`` also
    always prepends an "all" chip the spec never mentions.
    """
    if not filters:
        return []
    all_label = next(
        (_text(f.get("label")) for f in filters if _text(f.get("kind")) == "field"), ""
    ) or "All"
    example = next(
        (_text(f.get("label")) for f in filters if _text(f.get("kind")) != "field" and _text(f.get("label"))),
        all_label,
    )
    notes = [
        "- Every filter chip reads \"<label> (count)\", e.g. \"{0} (2)\"; `chooseFilter` "
        "matches by prefix, so pass the plain label and never an anchored regex. \"{1}\" is "
        "the chip that clears every filter.".format(example, all_label)
    ]
    # `chooseFilter` takes the first chip whose name starts with the label, over
    # `filterOptions`' order (all, field options, state filters), so a shorter
    # label silently selects the longer chip instead of failing loudly.
    for label in _shadowed_labels(spec, filters, all_label)[:MAX_SHADOWED_NOTES]:
        notes.append(
            "- \"{0}\" is the start of another chip's name: select it as \"{0} (\" so the "
            "prefix match cannot land on the wrong chip.".format(label)
        )
    return notes


def _shadowed_labels(
    spec: Dict[str, Any], filters: List[Dict[str, Any]], all_label: str
) -> List[str]:
    """Chip labels that are a strict prefix of another chip's label."""
    names = [all_label] + [_text(f.get("label")) for f in filters if _text(f.get("label"))]
    for field in _dicts(spec.get("fields")):
        names.extend(_strings(field.get("options")))
    unique = [name for index, name in enumerate(names) if name and name not in names[:index]]
    return [
        name
        for name in unique
        if any(other != name and other.startswith(name) for other in unique)
    ]
