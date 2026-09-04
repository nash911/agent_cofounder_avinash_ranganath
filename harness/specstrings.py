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
    for entry in filters:
        if _text(entry.get("kind")) == "field":
            lines.append(
                "- Filter chips for \"{0}\": \"{1}\" plus one chip per option above".format(
                    _label_of(spec, _text(entry.get("field"))), _text(entry.get("label"))
                )
            )
        else:
            lines.append(
                "- Filter chip \"{0}\" ({1})".format(_text(entry.get("label")), _text(entry.get("rule")))
            )
    lines.extend(_filter_notes(spec, filters))
    for badge in _dicts(spec.get("badges")):
        text = _text(badge.get("text"))
        lines.append(
            "- Badge text \"{0}\" when {1}{2}".format(
                text, _text(badge.get("rule")), _badge_example(text)
            )
        )
    for stat in _dicts(spec.get("stats")):
        lines.append(
            "- Stat tile \"{0}\" = {1} — read it with `stat(\"{0}\")`, which returns a string".format(
                _text(stat.get("label")), _compute_rule(_text(stat.get("rule")))
            )
        )
    for action in _dicts(spec.get("actions")):
        parts = ["- Row button \"{0}\"".format(_text(action.get("label")))]
        for template, value in (
            (", asks for \"{0}\"", _text(action.get("input_label"))),
            (", confirms with \"{0}\"", _text(action.get("confirm_text"))),
            (", toast \"{0}\"", _text(action.get("toast"))),
            (" — shown only when {0}", _text(action.get("available_rule"))),
        ):
            if value:
                parts.append(template.format(value))
        lines.append("".join(parts))
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
    return "\n".join(lines)


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
