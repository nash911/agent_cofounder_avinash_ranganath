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
| `src/lib/config-types.ts` | `defineApp`, `AppConfig`, `FieldDef`, `Filter`, `BadgeDef`, `StatDef`, `ActionDef` |
| `src/lib/repository.ts` | versioned localStorage, corrupt-data recovery, safe writes, memory adapter |
| `src/lib/fields.ts`, `collection.ts` | validation, coercion, formatting, search, narrowing, sorting |
| `src/lib/use-records.ts` | add / edit / delete-with-undo / run action, all persisted |
| `src/App.tsx`, `src/components/*` | the whole UI: form, list, badges, filters, `Dialog`, `ConfirmDialog`, `Toast`, `EmptyState`, `ErrorBoundary` |
| `src/styles.css` | tokens, type scale, one accent, dark mode, 360–1280px, focus, motion |
| `src/test/helpers.tsx` | `renderApp` `fill` `addRecord` `editRecord` `removeRecord` `runAction` `chooseFilter` `search` `reload` `corruptStorage` `row` `rowTitles` `expectRow` `expectNoRow` `stat` `confirmDialog` |
| `src/test/journeys.template.tsx` | one worked test per journey; reproduced below |

## The one file you write: `src/app-config.ts`

Rewrite it in one write, about 60 lines. The seeded version is reproduced below
— it demonstrates every construct, so you never need to read it.

Field kinds: `text` `longtext` `number` (`min` `max` `step` `integer` `unit`
`initial`) `select` (`options` `initial`) `boolean` `date`. All take `name`
`label` `required` `message` `help` `inForm` `inList`. Unset text is `""`.

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
- `src/lib/` never imports components; no `localStorage` outside `src/lib/repository.ts`.
- No `window.matchMedia`, `showModal` or unguarded `scrollIntoView` — jsdom has
  none of them. Use the `Dialog` component.
- Reuse the existing class names; invent no domain class names and add no CSS.
- Write ONE test file, `src/journeys.test.tsx`, with `src/test/helpers.tsx`
  (import it as `./test/helpers.js`): six lines per journey; import `describe`,
  `it`, `expect` from `vitest`. Never render `<App />` in a test — use `renderApp`.
- If the config genuinely cannot express something, choose the closest expressible shape
  and note it in `assumptions` — never create a file your mission did not name.

## Worked example: `src/app-config.ts`

The seeded configuration, verbatim. Every construct you need is here — do not
read the file, rewrite it from this.

```ts
import { defineApp } from "./lib/config-types.js";

/** Ambiguity: the idea says "only a couple left" without defining it.
 *  Decision: 2 or fewer. Recorded in report.partial.json assumptions. */
export const LOW_THRESHOLD = 2;

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
    { kind: "text", name: "holder", label: "Held by", inForm: false },
  ],
  titleField: "name",
  subtitleFields: ["source"],
  metaFields: ["category", "quantity"],
  sort: { field: "name", direction: "asc" },
  filters: [
    { kind: "field", field: "category", allLabel: "All" },
    { kind: "state", id: "low", label: "Running low",
      match: (row) => row.quantity <= LOW_THRESHOLD,
      emptyText: "Nothing is running low." },
    { kind: "state", id: "held", label: "Held",
      match: (row) => row.holder !== "", emptyText: "Nothing is held." },
  ],
  badges: [
    { id: "low", when: (row) => row.quantity <= LOW_THRESHOLD, tone: "alert",
      text: (row) => `Running low - ${row.quantity} left` },
    { id: "held", when: (row) => row.holder !== "", tone: "info",
      text: (row) => `Held by ${row.holder}` },
  ],
  summary: [
    { id: "total", label: "Records", compute: (rows) => rows.length },
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
  runAction, search, stat,
} from "./helpers.js";

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
