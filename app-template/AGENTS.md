# Generated application contract

- Keep the application self-contained and runnable with `npm run dev` at `http://localhost:3000`.
- Store durable single-user browser data locally when persistence is required.
- Prefer semantic HTML and accessible names so browser automation can use the interface without brittle selectors.
- Add tests for the product's critical user journeys and run them before claiming success.
- The seed intentionally contains no product tests. Add at least one completed, passing `src/**/*.test.ts` or `src/**/*.test.tsx` test; the runner rejects zero-test reports and any skipped or todo tests.
- Use only the dependencies already installed from the committed lockfile. Do not add packages or run dependency-install commands.
- `report.partial.json` contains only `status`, `app_url`, `start_command`, `summary`, `implemented_features`, `assumptions`, and `tests_run`.
- A `success` report must contain at least one `tests_run` entry and every entry must be `passed`. If a journey failed or was not run, record it as `failed`, explain why in `journey`, and use `partial` (or `failed` when the app cannot run).
- The runner owns the final `app_url`, location-aware `start_command`, independent `harness_checks`, and telemetry fields. Your product-journey test records remain in the specification-defined `tests_run` field.
- Do not create or edit `result.json`; the outer challenge runner derives its telemetry from Pi.

## What is already built

A complete, styled, accessible record application rendered from one
declaration. It works — do not rebuild or read it.

| File | What it already does |
|---|---|
| `src/app-config.ts` | **The file you write.** `export const appConfig = defineApp({...})`; read it first |
| `src/lib/config-types.ts` | `defineApp`, `AppConfig`, `FieldDef`, `Filter`, `BadgeDef`, `StatDef`, `ActionDef` |
| `src/lib/repository.ts` | versioned localStorage, corrupt-data recovery, safe writes, memory adapter |
| `src/lib/fields.ts`, `collection.ts` | validation, coercion, formatting, search, narrowing, sorting |
| `src/lib/use-records.ts` | add / edit / delete-with-undo / run action, all persisted |
| `src/App.tsx`, `src/components/*` | the whole UI: form, list, badges, filters, `Dialog`, `ConfirmDialog`, `Toast`, `EmptyState`, `ErrorBoundary` |
| `src/styles.css` | tokens, type scale, one accent, dark mode, 360–1280px, focus, motion |
| `src/test/helpers.tsx` | `renderApp` `fill` `addRecord` `editRecord` `removeRecord` `runAction` `chooseFilter` `search` `reload` `corruptStorage` `row` `rowTitles` `expectRow` `expectNoRow` `stat` `confirmDialog` |
| `src/test/journeys.template.tsx` | one worked test per journey; copy it |

## The one file you write: `src/app-config.ts`

Rewrite it in one write, about 60 lines. Read the seeded version first — it
demonstrates every construct. Map the idea:

- each attribute named → one `fields` entry. A quantity is `kind: "number"`,
  never text; a fixed set of choices is `kind: "select"`.
- a value only an action sets (a holder, an owner) → a field with `inForm: false`
- "which ones are X now" → a `{ kind: "state", ... }` entry in `filters`
- "how many are X" → a `summary` entry (`emphasis: true` for the headline figure)
- "one type at a time" → a `{ kind: "field", field: "..." }` filter
- anything that should stand out → a `badges` entry; its `text` is what the user
  reads, `tone` only decorates
- any verb other than add/edit/delete → an `actions` entry (`input` for one value,
  `confirm` for a warning, neither when instant)
- any threshold or vague quantity ("a couple", "running low", "overdue") → an
  exported `const` above `appConfig`, reused by the filter, badge and summary,
  plus one `assumptions` entry

Field kinds: `text` `longtext` `number` (`min` `max` `step` `integer` `unit`
`initial`) `select` (`options` `initial`) `boolean` `date`. All take `name`
`label` `required` `message` `help` `inForm` `inList`. Unset text is `""`.

## Rules

- **Never annotate the export.** Write `export const appConfig = defineApp({...})`,
  never `... : AppConfig = ...`. The annotation destroys inference.
- **Never hoist `fields` into a separate `const`.** Keep the array inline in
  `defineApp({...})` or `row.<name>` loses its type in every predicate.
- `src/lib/` never imports components; no `localStorage` outside `src/lib/repository.ts`.
- No `window.matchMedia`, `showModal` or unguarded `scrollIntoView` — jsdom has
  none of them. Use the `Dialog` component.
- Reuse the existing class names; invent no domain class names and add no CSS.
- Write ONE test file, `src/journeys.test.tsx`, with `src/test/helpers.tsx`
  (import it as `./test/helpers.js`): six lines per journey; import `describe`,
  `it`, `expect` from `vitest`. Never render `<App />` in a test — use `renderApp`.
- If the config genuinely cannot express something, add ONE small component under
  `src/components/`; replacing a scaffold file is almost never cheapest.
