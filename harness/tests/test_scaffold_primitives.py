"""The scaffold primitives a one-record configuration cannot fake.

Three shapes of idea kept arriving that ``src/app-config.ts`` had no word for,
and every one of them failed the same way twice -- once in the Builder's
configuration and once, differently, in the Tester's assertions:

1. a value COMPUTED from other fields (a quantity times a price, a count of
   days between two dates). With no primitive it was stored as an input field,
   so it was whatever was last typed rather than what the fields now say;
2. an action that applies to EVERY record at once (a reset, a clear-all). With
   no primitive it became a row button, so one click changed one row;
3. a number with a CURRENCY unit. Nothing pinned which side the symbol goes,
   so the app rendered "40 £" while the tests written from the same sentence
   expected "£40" -- and a money stat tile showed a bare number.

Date-relative rules ("more than N days ago") were the fourth: hand-rolled
arithmetic on ``yyyy-mm-dd`` strings that a timezone or a month boundary could
push a day out.

These tests exercise the four primitives -- ``derived``, ``bulkActions``,
``StatDef.unit`` / ``formatNumber``, and ``src/lib/dates.ts`` -- through the
REAL vitest binary against a private copy of the seed, because the pass this
protects is a rendered DOM and a persisted record, not a type.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest
from typing import Dict, List

from harness.tests import support

REPO_ROOT = support.REPO_ROOT
APP_TEMPLATE = REPO_ROOT / "app-template"
NODE_MODULES = APP_TEMPLATE / "node_modules"
VITEST_BIN = NODE_MODULES / ".bin" / "vitest"
VITEST_TIMEOUT_S = 120.0

# A date offset from today, written the way a `date` field stores it. Both
# suites below use it instead of a literal so no test rots as the calendar moves.
ISO_HELPER = (
    "const iso = (offsetDays: number) =>\n"
    "  new Date(Date.now() + offsetDays * 86400000).toISOString().slice(0, 10);\n"
)

JOURNEYS_TEST = '''import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import {
  addRecord, chooseFilter, expectNoRow, expectRow, renderApp, row, rowTitles,
  runAction, runBulkAction, stat,
} from "./test/helpers.js";

__ISO__
describe("computed values", () => {
  it("shows a value computed from other fields, after the stored ones", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "2", Price: "4" });
    expectRow("Alpha", "Quantity 2 left", "Value £8");
    expectRow("Alpha", "Days since check", "0");
  });

  it("recomputes the value when a field it reads changes", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "3", Price: "5" });
    expectRow("Alpha", "Value £15");
    await runAction(user, "Use one", "Alpha");
    expectRow("Alpha", "Value £10");
  });

  it("never offers a computed value on the form", async () => {
    renderApp();
    expect(screen.queryByLabelText("Value")).toBeNull();
    expect(screen.queryByLabelText("Days since check")).toBeNull();
  });
});

describe("currency rendering", () => {
  it("puts the symbol before the number in a row", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1", Price: "4" });
    expectRow("Alpha", "Price £4");
    expect(row("Alpha").textContent).not.toContain("4 £");
  });

  it("puts a word unit after the number in a row", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "7", Price: "0" });
    expectRow("Alpha", "7 left");
  });

  it("carries the unit into a stat tile", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "2", Price: "4" });
    await addRecord(user, { Name: "Beta", Category: "Type A", Quantity: "1", Price: "6" });
    expect(stat("Stock value")).toBe("£14");
    expect(stat("Records")).toBe("2");
  });
});

describe("an action over every record", () => {
  it("changes all three records from one click, whatever the filter shows", async () => {
    const { user } = renderApp();
    for (const [name, category] of [["Alpha", "Type A"], ["Beta", "Type B"], ["Gamma", "Type C"]]) {
      await addRecord(user, { Name: name, Category: category, Quantity: "1" });
      await runAction(user, "Mark held", name, "Sam");
    }
    await chooseFilter(user, /Type A/);
    expect(rowTitles()).toEqual(["Alpha"]);
    await runBulkAction(user, "Mark all returned");
    expect(screen.getByText("Mark all returned applied to 3 records")).toBeVisible();
    await chooseFilter(user, "All");
    expect(rowTitles()).toEqual(["Alpha", "Beta", "Gamma"]);
    for (const name of ["Alpha", "Beta", "Gamma"]) {
      expect(row(name).textContent).not.toContain("Held by Sam");
    }
  });

  it("keeps the change across a reload", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    await runAction(user, "Mark held", "Alpha", "Sam");
    await runBulkAction(user, "Mark all returned");
    expect(screen.getByText("Mark all returned applied to 1 records")).toBeVisible();
    expect(row("Alpha").textContent).not.toContain("Held by Sam");
  });
});

describe("an action that deletes", () => {
  it("removes every record a bulk action accepts and keeps the rest", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    await addRecord(user, { Name: "Beta", Category: "Type B", Quantity: "1" });
    await runAction(user, "Mark held", "Alpha", "Sam");
    await runBulkAction(user, "Remove all held");
    expect(screen.getByText("Remove all held applied to 1 records")).toBeVisible();
    expectNoRow("Alpha");
    expectRow("Beta");
    expect(stat("Records")).toBe("1");
  });

  it("removes one record when a row action returns null", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    await addRecord(user, { Name: "Beta", Category: "Type B", Quantity: "1" });
    await runAction(user, "Drop", "Alpha");
    expectNoRow("Alpha");
    expectRow("Beta");
    expect(screen.getByText("Alpha deleted.")).toBeVisible();
  });
});

describe("a select patch and a field filter's empty text", () => {
  it("applies a parameter-less select patch", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    await runAction(user, "Make C", "Alpha");
    expectRow("Alpha", "Type C");
  });

  it("shows the field filter's own empty text when its chip matches nothing", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    await chooseFilter(user, "Type A");
    expect(rowTitles()).toEqual(["Alpha"]);
    await runAction(user, "Make C", "Alpha");
    expect(rowTitles()).toEqual([]);
    expect(screen.getByText("Nothing matches this view")).toBeVisible();
    expect(screen.getByText("No records of this type.")).toBeVisible();
  });
});

describe("date-relative rules", () => {
  it("narrows to the records last touched more than the threshold ago", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1", "Last checked": iso(-30) });
    await addRecord(user, { Name: "Beta", Category: "Type A", Quantity: "1", "Last checked": iso(0) });
    await chooseFilter(user, /Stale/);
    expect(rowTitles()).toEqual(["Alpha"]);
    expectRow("Alpha", "Checked");
  });

  it("counts the days into the row itself", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1", "Last checked": iso(-10) });
    const text = row("Alpha").textContent ?? "";
    const match = /Days since check (\\d+)/.exec(text);
    expect(match).not.toBeNull();
    expect(Number(match?.[1])).toBeGreaterThanOrEqual(9);
    expect(Number(match?.[1])).toBeLessThanOrEqual(11);
  });
});
'''.replace("__ISO__", ISO_HELPER)

PRIMITIVES_TEST = '''import { describe, expect, it } from "vitest";
import { formatNumber } from "./lib/fields.js";
import { daysBetween, daysSince, daysUntil, isValidDate, today } from "./lib/dates.js";

__ISO__
describe("formatNumber", () => {
  it("leads with a currency symbol and no space", () => {
    expect(formatNumber(40, "£")).toBe("£40");
    expect(formatNumber(40, "$")).toBe("$40");
    expect(formatNumber(40, "€")).toBe("€40");
    expect(formatNumber(40, "¥")).toBe("¥40");
  });

  it("leads with any single non-alphanumeric unit", () => {
    expect(formatNumber(5, "%")).toBe("%5");
    expect(formatNumber(5, "#")).toBe("#5");
  });

  it("trails a word unit after one space", () => {
    expect(formatNumber(40, "pts")).toBe("40 pts");
    expect(formatNumber(4, "left")).toBe("4 left");
    expect(formatNumber(4, "kg")).toBe("4 kg");
    expect(formatNumber(4, "h")).toBe("4 h");
  });

  it("renders a bare number when there is no unit", () => {
    expect(formatNumber(7)).toBe("7");
    expect(formatNumber(7, "")).toBe("7");
    expect(formatNumber("many", "£")).toBe("£many");
  });
});

describe("dates", () => {
  it("counts whole calendar days, in both directions", () => {
    expect(daysBetween("2026-01-01", "2026-01-08")).toBe(7);
    expect(daysBetween("2026-01-08", "2026-01-01")).toBe(-7);
    expect(daysBetween("2026-01-01", "2026-01-01")).toBe(0);
  });

  it("crosses month, year and leap-day boundaries", () => {
    expect(daysBetween("2026-02-28", "2026-03-01")).toBe(1);
    expect(daysBetween("2024-02-28", "2024-03-01")).toBe(2);
    expect(daysBetween("2025-12-31", "2026-01-01")).toBe(1);
  });

  it("does not drift across a daylight-saving change", () => {
    expect(daysBetween("2026-03-28", "2026-03-30")).toBe(2);
    expect(daysBetween("2026-10-24", "2026-10-26")).toBe(2);
  });

  it("returns 0 for an unset or malformed date instead of throwing", () => {
    expect(daysBetween("", "2026-01-01")).toBe(0);
    expect(daysBetween("2026-01-01", "")).toBe(0);
    expect(daysBetween("2026-02-30", "2026-03-01")).toBe(0);
    expect(daysBetween("01/01/2026", "2026-03-01")).toBe(0);
    expect(daysSince("")).toBe(0);
    expect(daysUntil("")).toBe(0);
  });

  it("counts forward with daysUntil and back with daysSince", () => {
    expect(daysUntil(today())).toBe(0);
    expect(daysSince(today())).toBe(0);
    expect(daysUntil(iso(3))).toBeGreaterThanOrEqual(2);
    expect(daysUntil(iso(3))).toBeLessThanOrEqual(4);
    expect(daysSince(iso(-10))).toBeGreaterThanOrEqual(9);
    expect(daysSince(iso(-10))).toBeLessThanOrEqual(11);
    expect(daysSince(iso(10))).toBeLessThanOrEqual(-9);
  });

  it("accepts only a real yyyy-mm-dd date", () => {
    expect(isValidDate(today())).toBe(true);
    expect(isValidDate("2026-01-01")).toBe(true);
    expect(isValidDate("")).toBe(false);
    expect(isValidDate("2026-13-01")).toBe(false);
    expect(isValidDate("2026-02-30")).toBe(false);
    expect(isValidDate("2026-1-1")).toBe(false);
  });
});
'''.replace("__ISO__", ISO_HELPER)


def _seed_copy(destination: pathlib.Path) -> pathlib.Path:
    """A private, writable copy of ``app-template`` -- never write into the real one.

    ``node_modules`` is symlinked rather than copied: it is the installed tree
    the committed lockfile produced, and copying it would cost minutes.
    """
    application = destination / "app"
    shutil.copytree(
        APP_TEMPLATE,
        application,
        ignore=shutil.ignore_patterns("node_modules", "dist"),
        symlinks=True,
    )
    (application / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    return application


class ScaffoldPrimitivesTest(unittest.TestCase):
    """Derived values, bulk actions, units and date helpers, run for real."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if not NODE_MODULES.is_dir() or not VITEST_BIN.exists():
            raise unittest.SkipTest("app-template dependencies are not installed")
        cls._workspace = pathlib.Path(
            support.scratch_root() / "scaffold-primitives"
        )
        if cls._workspace.exists():
            shutil.rmtree(cls._workspace)
        cls._workspace.mkdir(parents=True)
        cls.application = _seed_copy(cls._workspace)
        _add_deleting_actions(cls.application / "src" / "app-config.ts")
        (cls.application / "src" / "journeys.test.tsx").write_text(
            JOURNEYS_TEST, encoding="utf-8"
        )
        (cls.application / "src" / "primitives.test.ts").write_text(
            PRIMITIVES_TEST, encoding="utf-8"
        )
        cls.tsc = _run_tsc(cls.application)
        cls.report = _run_vitest(cls.application)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "_workspace", cls._workspace), ignore_errors=True)

    def _results(self) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        for suite in self.report.get("testResults", []):
            for assertion in suite.get("assertionResults", []):
                results.append(assertion)
        return results

    def test_the_probe_config_type_checks(self):
        # vitest never type-checks; the harness gate does, and a select patch
        # from a parameter-less arrow is exactly what tsc used to reject.
        self.assertEqual(self.tsc[0], 0, self.tsc[1])

    def test_every_primitive_test_passes(self):
        failed = [
            "{0}: {1}".format(entry.get("fullName"), entry.get("failureMessages"))
            for entry in self._results()
            if entry.get("status") != "passed"
        ]
        self.assertEqual(failed, [], "\n".join(failed))

    def test_the_run_was_not_empty_or_skipped(self):
        # A green report over zero tests would prove nothing; so would one that
        # skipped the suite because a helper failed to import.
        results = self._results()
        self.assertGreaterEqual(len(results), 19)
        self.assertEqual(self.report.get("numPendingTests", 0), 0)
        self.assertEqual(self.report.get("numTodoTests", 0), 0)
        self.assertEqual(self.report.get("numFailedTests", 0), 0)

    def test_both_suites_ran(self):
        names = {entry.get("fullName", "") for entry in self._results()}
        for expected in (
            "computed values",
            "currency rendering",
            "an action over every record",
            "an action that deletes",
            "a select patch and a field filter's empty text",
            "date-relative rules",
            "formatNumber",
            "dates",
        ):
            self.assertTrue(
                any(name.startswith(expected) for name in names),
                "no test from {0!r} ran; ran: {1}".format(expected, sorted(names)),
            )


def _add_deleting_actions(config: pathlib.Path) -> None:
    """Give the private copy one row action and one bulk action that delete.

    The seed keeps its worked example small, so the `null` patch is exercised
    here on a copy: a bulk "Remove all held" over `available`, and a one-click
    "Drop" on a row. Both are the shape a clear-all or a "sold and gone" takes.
    """
    text = config.read_text(encoding="utf-8")
    bulk = "  bulkActions: [\n"
    actions = "  actions: [\n"
    assert bulk in text and actions in text, "seed config lost its actions"
    field_filter = '    { kind: "field", field: "category", allLabel: "All" },\n'
    assert field_filter in text, "seed config lost its field filter"
    text = text.replace(
        bulk,
        bulk + "    { id: \"dropHeld\", label: \"Remove all held\",\n"
        "      available: (row) => row.holder !== \"\", apply: () => null },\n",
        1,
    ).replace(
        actions,
        actions + "    { id: \"drop\", label: \"Drop\", apply: () => null },\n"
        # Parameter-less on purpose: the arrow is type-checked before `fields`
        # is inferred, so the literal widens to `string` -- the shape that
        # failed TS2322 in a holdout case (measured 2026-09-04).
        "    { id: \"makeC\", label: \"Make C\", apply: () => ({ category: \"Type C\" }) },\n",
        1,
    ).replace(
        field_filter,
        '    { kind: "field", field: "category", allLabel: "All",\n'
        '      emptyText: "No records of this type." },\n',
        1,
    )
    config.write_text(text, encoding="utf-8")


def _run_tsc(application: pathlib.Path):
    """``tsc --noEmit`` over the copy: (exit code, output tail)."""
    completed = subprocess.run(
        [str(NODE_MODULES / ".bin" / "tsc"), "--noEmit", "-p", "."],
        cwd=str(application), capture_output=True, text=True,
        timeout=VITEST_TIMEOUT_S, check=False,
    )
    return completed.returncode, (completed.stdout + completed.stderr)[-3000:]


def _run_vitest(application: pathlib.Path) -> Dict[str, object]:
    """The real vitest binary over the copy, as JSON."""
    report = application / "vitest-report.json"
    completed = subprocess.run(
        [
            str(VITEST_BIN),
            "run",
            "--reporter=json",
            "--outputFile={0}".format(report),
        ],
        cwd=str(application),
        capture_output=True,
        text=True,
        timeout=VITEST_TIMEOUT_S,
        check=False,
    )
    if not report.is_file():
        raise AssertionError(
            "vitest wrote no report (exit {0})\n{1}\n{2}".format(
                completed.returncode, completed.stdout[-4000:], completed.stderr[-4000:]
            )
        )
    payload = json.loads(report.read_text(encoding="utf-8"))
    if completed.returncode != 0:
        payload.setdefault("harnessStderr", completed.stderr[-4000:])
    return payload


if __name__ == "__main__":
    unittest.main()
