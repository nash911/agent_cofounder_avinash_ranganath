# Generated application contract

- Keep the application self-contained and runnable with `npm run dev` at `http://localhost:3000`.
- Store durable single-user browser data locally when persistence is required.
- Prefer semantic HTML and accessible names so browser automation can use the interface without brittle selectors.
- The seed intentionally contains no product tests; `src/journeys.test.tsx` supplies them. The runner rejects zero-test reports and any skipped or todo test.
- Use only the dependencies already installed from the committed lockfile. Do not add packages or run dependency-install commands.

## What is already built

A complete, styled, accessible record application rendered from one
declaration. It works — do not rebuild or read it.

| File | What it already does |
|---|---|
| `src/app-config.ts` | **The file you write.** `export const appConfig = defineApp({...})`; reproduced below |
| `src/lib/config-types.ts` | `defineApp`, `AppConfig`, `FieldDef`, `Filter`, `BadgeDef`, `DerivedDef`, `StatDef`, `ActionDef`, `BulkActionDef` |
| `src/lib/repository.ts` | versioned localStorage, corrupt-data recovery, safe writes, memory adapter |
| `src/lib/fields.ts`, `collection.ts` | validation, coercion, formatting, search, narrowing, sorting |
| `src/lib/dates.ts` | `today` `isValidDate` `daysBetween` `daysUntil` `daysSince` |
| `src/lib/use-records.ts` | add / edit / delete-with-undo / run action / run bulk action, all persisted |
| `src/App.tsx`, `src/components/*` | the whole UI: form, list, badges, filters, `Dialog`, `ConfirmDialog`, `Toast`, `EmptyState`, `ErrorBoundary` |
| `src/styles.css` | tokens, type scale, one accent, dark mode, 360–1280px, focus, motion |
| `src/test/helpers.tsx` | `renderApp` `fill` `addRecord` `editRecord` `removeRecord` `runAction` `runBulkAction` `chooseFilter` `search` `reload` `corruptStorage` `row` `rowTitles` `expectRow` `expectNoRow` `stat` `confirmDialog` |
| `src/test/journeys.template.tsx` | one worked test per journey; reproduced below |

## The one file you write: `src/app-config.ts`

Rewrite it in one write, about 90 lines. The seeded version is reproduced below
— it demonstrates every construct, so you never need to read it.

Field kinds: `text` `longtext` `number` (`min` `max` `step` `integer` `unit`
`initial`) `select` (`options` `initial`) `boolean` `date`. All take `name`
`label` `required` `message` `help` `inForm` `inList`. Unset text is `""`.

Three keys sit beside `fields`, never inside it:

- **`derived`** — a value COMPUTED from one row:
  `{ name, label, compute: (row) => number | string, unit? }`. Anything the
  idea *works out* — a total, a price times a quantity, a count of days — is
  derived, never a field: a field stores one answer and goes stale the moment
  another field changes. It renders in the row's meta line after `metaFields`,
  never on the form, and is never saved. `name` must differ from every field
  name.
- **`bulkActions`** — one button that changes EVERY record at once (a reset, a
  clear-all, an archive-all):
  `{ id, label, confirm?, available?: (row) => boolean, apply: (row) => Patch }`.
  It renders in its own toolbar between the stats and the filters and patches
  every row `available` accepts — all of them by default — whatever the filter
  happens to be showing, then reports `<label> applied to N records`. A button
  on one row is `actions`; "for all of them" is `bulkActions`.
- **`summary[].unit`** — the unit a stat tile renders its figure with.

**Numbers read the same way everywhere.** A unit that is a currency symbol
(`£ $ € ¥`) — or any other single non-alphanumeric character — comes BEFORE the
number with no space: `£40`. Every other unit comes after it with one space:
`40 pts`. Fields, derived values and stat tiles all obey this one rule, so the
same money never renders two ways. Booleans read as `Yes` / `No`.

**Date rules come from `src/lib/dates.ts`; never hand-roll them.** Import them
with `import { daysBetween, daysUntil, daysSince, today } from "./lib/dates.js";`.
`today()` is today as `yyyy-mm-dd`, local. `daysBetween(a, b)` is whole days
from `a` to `b`. `daysUntil(iso)` counts forward from today, so "within the
next 3 days" is `daysUntil(row.due) <= 3 && daysUntil(row.due) >= 0`.
`daysSince(iso)` counts back, so "more than 7 days ago" is
`daysSince(row.seen) > 7`. An unset or malformed date gives `0`;
`isValidDate(iso)` says whether one is real. A test writes a date as an offset
from today, never as a literal, so it cannot rot: see the template below.

## Rules

- **Never annotate the export.** Write `export const appConfig = defineApp({...})`,
  never `... : AppConfig = ...`. The annotation destroys inference.
- **Never hoist `fields` into a separate `const`.** Keep the array inline in
  `defineApp({...})` or `row.<name>` loses its type in every predicate.
- **Never write `as const`.** `defineApp` infers every literal type itself; an
  `as const` on `fields` (or anywhere) creates a second, unrelated type and
  `tsc` fails with TS2719 ("Two different types with this name").
- **Every `match`/`when`/`available`/`compute`/`apply` is a function, never a
  string.** A rule written as English text fails `tsc` (TS2322).
- **Give an action `input` or `confirm` only when the idea asks for one.** An
  action with neither runs on the first click; a dialog nobody asked for turns
  a one-click journey into a two-step one and fails every test written from
  the same sentence.
- `src/lib/` never imports components; no `localStorage` outside `src/lib/repository.ts`.
- No `window.matchMedia`, `showModal` or unguarded `scrollIntoView` — jsdom has
  none of them. Use the `Dialog` component.
- Reuse the existing class names; invent no domain class names and add no CSS.
- Write ONE test file, `src/journeys.test.tsx`, with `src/test/helpers.tsx`
  (import it as `./test/helpers.js`): six lines per journey; import `describe`,
  `it`, `expect` from `vitest`. Never render `<App />` in a test — use `renderApp`.
  Assert what the app now shows: `expectNoRow` is for something removed or
  filtered out, never for a record just added.
- If the config genuinely cannot express something, choose the closest expressible shape
  and note it in `assumptions` — never create a file your mission did not name.

## Worked example: `src/app-config.ts`

The seeded configuration, verbatim. Every construct you need is here — do not
read the file, rewrite it from this.

```ts
import { defineApp } from "./lib/config-types.js";
// Every date helper, always by this one import line. This idea only needs
// `daysSince`; `daysBetween(a, b)`, `daysUntil(iso)` and `today()` are the
// same shape for any other date-relative rule.
import { daysBetween, daysUntil, daysSince, today } from "./lib/dates.js";

/** Ambiguity: the idea says "only a couple left" without defining it.
 *  Decision: 2 or fewer. Recorded in report.partial.json assumptions. */
export const LOW_THRESHOLD = 2;

/** Ambiguity: "checked recently" names no number.
 *  Decision: stale after 7 days. Recorded in report.partial.json assumptions. */
export const STALE_DAYS = 7;

export const appConfig = defineApp({
  storageKey: "records.v1",
  copy: {
    title: "Record Tracker",
    tagline: "Everything you keep, in one list.",
    noun: "record",
    nounPlural: "records",
    addLabel: "Add record",
    emptyTitle: "No records yet",
    emptyBody: "Add your first record with the form.",
  },
  fields: [
    { kind: "text", name: "name", label: "Name", required: true,
      message: "Name is required." },
    { kind: "text", name: "source", label: "Source" },
    { kind: "select", name: "category", label: "Category", required: true,
      options: ["Type A", "Type B", "Type C"], initial: "Type A" },
    { kind: "number", name: "quantity", label: "Quantity", required: true,
      min: 0, integer: true, initial: 0, unit: "left",
      message: "Quantity must be a whole number, 0 or more." },
    { kind: "number", name: "price", label: "Price", min: 0, initial: 0,
      unit: "£" },
    { kind: "date", name: "checked", label: "Last checked", initial: "today" },
    { kind: "text", name: "holder", label: "Held by", inForm: false },
  ],
  titleField: "name",
  subtitleFields: ["source"],
  metaFields: ["category", "quantity", "price", "checked"],
  sort: { field: "name", direction: "asc" },
  filters: [
    { kind: "field", field: "category", allLabel: "All" },
    { kind: "state", id: "low", label: "Running low",
      match: (row) => row.quantity <= LOW_THRESHOLD,
      emptyText: "Nothing is running low." },
    { kind: "state", id: "stale", label: "Stale",
      match: (row) => daysSince(row.checked) > STALE_DAYS,
      emptyText: "Everything was checked recently." },
    { kind: "state", id: "held", label: "Held",
      match: (row) => row.holder !== "", emptyText: "Nothing is held." },
  ],
  badges: [
    { id: "low", when: (row) => row.quantity <= LOW_THRESHOLD, tone: "alert",
      text: (row) => `Running low - ${row.quantity} left` },
    { id: "stale", when: (row) => daysSince(row.checked) > STALE_DAYS,
      tone: "warn", text: (row) => `Checked ${daysSince(row.checked)} days ago` },
    { id: "held", when: (row) => row.holder !== "", tone: "info",
      text: (row) => `Held by ${row.holder}` },
  ],
  derived: [
    { name: "value", label: "Value", unit: "£",
      compute: (row) => row.quantity * row.price },
    { name: "sinceCheck", label: "Days since check",
      compute: (row) => daysSince(row.checked) },
  ],
  summary: [
    { id: "total", label: "Records", compute: (rows) => rows.length },
    { id: "value", label: "Stock value", unit: "£",
      compute: (rows) => rows.reduce((sum, r) => sum + r.quantity * r.price, 0) },
    { id: "low", label: "Running low", emphasis: true,
      compute: (rows) => rows.filter((r) => r.quantity <= LOW_THRESHOLD).length },
  ],
  actions: [
    { id: "hold", label: "Mark held", available: (row) => row.holder === "",
      input: { label: "Held by", required: true },
      apply: (_row, input) => ({ holder: input }),
      toast: (row) => `${row.name} marked held.` },
    { id: "release", label: "Mark returned", available: (row) => row.holder !== "",
      apply: () => ({ holder: "" }), toast: (row) => `${row.name} returned.` },
    { id: "useOne", label: "Use one", available: (row) => row.quantity > 0,
      apply: (row) => ({ quantity: row.quantity - 1 }) },
  ],
  bulkActions: [
    { id: "releaseAll", label: "Mark all returned",
      confirm: "This returns every record at once.",
      apply: () => ({ holder: "" }) },
  ],
});
```

## Journey test template

`src/test/journeys.template.tsx`, verbatim: one worked test per journey pattern.
Copy its idioms into `src/journeys.test.tsx`, which sits one directory above the
helpers, so import them from `"./test/helpers.js"`.

```tsx
/** JOURNEY TEST TEMPLATE: copy these into `src/journeys.test.tsx` (one directory
 *  up, so import helpers from "./test/helpers.js"). Not collected by vitest --
 *  the seed ships zero runnable tests -- but typechecked, so it compiles. */
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import {
  addRecord, chooseFilter, confirmDialog, corruptStorage, editRecord,
  expectNoRow, expectRow, reload, removeRecord, renderApp, row, rowTitles,
  runAction, runBulkAction, search, stat,
} from "./helpers.js";

/** A date N days from today, as the `yyyy-mm-dd` a date field stores. Never
 *  write a literal date in a test: the calendar moves and the test rots. */
const iso = (offsetDays: number) =>
  new Date(Date.now() + offsetDays * 86400000).toISOString().slice(0, 10);

describe("journeys", () => {
  it("adds a record and shows it in the list", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Source: "Acme", Category: "Type A", Quantity: "4" });
    expectRow("Alpha", "Acme", "4 left");
  });

  it("rejects an empty required field with a message", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "", Category: "Type A", Quantity: "1" });
    expect(screen.getByText("Name is required.")).toBeVisible();
    expect(rowTitles()).toEqual([]);
  });

  it("edits a record", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "4" });
    await editRecord(user, "Alpha", { Name: "Alpha II" });
    expectRow("Alpha II");
    expectNoRow("Alpha");
  });

  it("deletes a record and can undo it", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "4" });
    await removeRecord(user, "Alpha");
    expectNoRow("Alpha");
    expect(screen.getByText("Alpha deleted.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expectRow("Alpha");
  });

  it("narrows the list to one category", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "4" });
    await addRecord(user, { Name: "Beta", Category: "Type B", Quantity: "4" });
    await chooseFilter(user, /Type A/);
    expect(rowTitles()).toEqual(["Alpha"]);
  });

  it("narrows the list to a derived state", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    await addRecord(user, { Name: "Beta", Category: "Type A", Quantity: "40" });
    await chooseFilter(user, /Running low/);
    expect(rowTitles()).toEqual(["Alpha"]);
    expectRow("Alpha", "Running low");
  });

  it("shows the derived count the idea asks for", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1" });
    expect(stat("Running low")).toBe("1");
  });

  it("shows a value computed from other fields", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "2", Price: "4" });
    expectRow("Alpha", "Value", "£8");
  });

  it("renders a currency unit before the number", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1", Price: "4" });
    expectRow("Alpha", "£4");
  });

  it("shows a stat with its unit", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "2", Price: "4" });
    expect(stat("Stock value")).toBe("£8");
  });

  it("narrows the list by a date-relative rule", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "1", "Last checked": iso(-30) });
    await addRecord(user, { Name: "Beta", Category: "Type A", Quantity: "1", "Last checked": iso(0) });
    await chooseFilter(user, /Stale/);
    expect(rowTitles()).toEqual(["Alpha"]);
  });

  it("runs one action over every record at once", async () => {
    const { user } = renderApp();
    for (const name of ["Alpha", "Beta", "Gamma"]) {
      await addRecord(user, { Name: name, Category: "Type A", Quantity: "1" });
      await runAction(user, "Mark held", name, "Sam");
    }
    await runBulkAction(user, "Mark all returned");
    expect(screen.getByText("Mark all returned applied to 3 records")).toBeVisible();
    for (const name of ["Alpha", "Beta", "Gamma"]) {
      expect(row(name).textContent).not.toContain("Held by Sam");
    }
  });

  it("runs an action that changes state", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "4" });
    await runAction(user, "Mark held", "Alpha", "Sam");
    expectRow("Alpha", "Held by Sam");
    await runAction(user, "Mark returned", "Alpha");
    expect(row("Alpha").textContent).not.toContain("Held by Sam");
  });

  it("keeps records across a refresh", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "4" });
    reload();
    expectRow("Alpha", "Type A");
  });

  it("recovers from malformed saved data", () => {
    renderApp({ records: [{ name: "Alpha", category: "Type A", quantity: 4 }] });
    corruptStorage();
    expect(screen.getByText(/could not be read/i)).toBeVisible();
    expect(rowTitles()).toEqual([]);
  });

  it("finds a record by search", async () => {
    const { user } = renderApp();
    await addRecord(user, { Name: "Alpha", Category: "Type A", Quantity: "4" });
    await addRecord(user, { Name: "Beta", Category: "Type A", Quantity: "4" });
    await search(user, "alph");
    expect(rowTitles()).toEqual(["Alpha"]);
  });
});
```
