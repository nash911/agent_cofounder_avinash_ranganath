/** Journey-test helpers. Every query is by accessible name or visible label,
 *  never by class name. `userEvent.setup()` runs inside `renderApp`. */
import { fireEvent, render, screen, within, type RenderResult } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { expect } from "vitest";
import { App } from "../App.js";
import { appConfig } from "../app-config.js";
import { erase, type AnyConfig, type RowPatch } from "../lib/config-types.js";
import { createLocalRepository, type Repository, type StorageLike } from "../lib/repository.js";

type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
type Values = Record<string, string>;
interface Harness { config: AnyConfig; storage: StorageLike; repository: Repository; result: RenderResult }

let renders = 0;
let active: Harness | undefined;

/** Matches a visible label, ignoring any trailing required marker. */
const labelIs = (label: string) => (content: string) => content.replace(/[\s*]+$/, "") === label;
const nameOf = (el: Element) => (el.getAttribute("aria-label") ?? el.textContent ?? "").trim();
/** Matches an accessible name ignoring whitespace (hidden-span row labels). */
const nameIs = (name: string) => (text: string) => text.replace(/\s+/g, "") === name.replace(/\s+/g, "");
function current(): Harness {
  if (!active) throw new Error("Call renderApp() before any other helper.");
  return active;
}
function memoryStorage(): StorageLike {   // isolated per test; nothing leaks
  const entries = new Map<string, string>();
  return { getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value); }, removeItem: (key) => { entries.delete(key); } };
}
function remount(harness: Harness): void {
  harness.result.unmount();
  harness.result = render(<App config={harness.config} repository={harness.repository} />);
}
function confirmButton(dialog: HTMLElement): HTMLElement {
  const rest = within(dialog).getAllByRole("button").filter((b) => !/^(cancel|close)$/i.test(nameOf(b)));
  const button = rest[rest.length - 1];
  if (!button) throw new Error("The open dialog has no confirm button.");
  return button;
}
async function setValue(user: UserEvent, el: Control, value: string): Promise<void> {
  if (el instanceof HTMLSelectElement) { await user.selectOptions(el, value); return; }
  const box = el instanceof HTMLInputElement && el.type === "checkbox";
  if (box) { if (el.checked !== (value === "true")) await user.click(el); return; }
  await user.clear(el);                     // user-event APPENDS; never skip this
  if (value === "") return;
  await user.type(el, value);
  if (el.value !== value) fireEvent.change(el, { target: { value } });
}
/** The control (`control`) or the stat value (`!control`) labelled `label`. */
function labelled(scope: HTMLElement, label: string | RegExp, control: boolean): HTMLElement {
  const all = within(scope).getAllByLabelText(typeof label === "string" ? labelIs(label) : label);
  const found = all.find((el) => el.matches("input, select, textarea") === control);
  if (!found) throw new Error(`No ${control ? "control" : "stat"} labelled ${String(label)}.`);
  return found;
}
async function fillWithin(user: UserEvent, scope: HTMLElement, values: Values): Promise<void> {
  for (const [label, value] of Object.entries(values)) await setValue(user, labelled(scope, label, true) as Control, value);
}

/** Renders the app against a fresh repository with a unique storage key.
 *  `records` are seeded before the first render, like previously saved data. */
export function renderApp(options?: { config?: AnyConfig; records?: readonly RowPatch[] }): { user: UserEvent; repository: Repository } {
  const config: AnyConfig = { ...(options?.config ?? erase(appConfig)), storageKey: `test-records-${++renders}.v1` };
  const storage = memoryStorage();
  const repository = createLocalRepository(config, storage);
  for (const values of options?.records ?? []) repository.create(values);
  const result = render(<App config={config} repository={repository} />);
  active = { config, storage, repository, result };
  return { user: userEvent.setup(), repository };
}

/** Types by visible label; selects choose an option, checkboxes take "true"/"".
 *  Clears first, so an initial value never survives. Values are always strings. */
export function fill(user: UserEvent, values: Values): Promise<void> {
  return fillWithin(user, document.body, values);
}

/** fill(...) then click the add button. */
export async function addRecord(user: UserEvent, values: Values): Promise<void> {
  await fill(user, values);
  await user.click(screen.getByRole("button", { name: nameIs(current().config.copy.addLabel) }));
}

/** Opens the row's Edit control, clears and fills the named fields, saves. */
export async function editRecord(user: UserEvent, title: string, values: Values): Promise<void> {
  await user.click(within(row(title)).getAllByRole("button", { name: nameIs(`Edit ${title}`) })[0]);
  await fill(user, values);
  await user.click(screen.getByRole("button", { name: nameIs("Save changes") }));
}

/** Clicks "Delete <title>", confirming in the dialog if one opens. */
export async function removeRecord(user: UserEvent, title: string): Promise<void> {
  await user.click(within(row(title)).getAllByRole("button", { name: nameIs(`Delete ${title}`) })[0]);
  if (screen.queryByRole("dialog")) await confirmDialog(user);
}

/** Clicks "<label> <title>", fills the dialog's single input if one opens, confirms. */
export async function runAction(user: UserEvent, label: string, title: string, input?: string): Promise<void> {
  await user.click(within(row(title)).getAllByRole("button", { name: nameIs(`${label} ${title}`) })[0]);
  const dialog = screen.queryByRole("dialog");
  if (!dialog) return;
  const s = within(dialog);
  const control = s.queryAllByRole("textbox")[0] ?? s.queryAllByRole("spinbutton")[0]
    ?? s.queryAllByRole("combobox")[0] ?? s.queryAllByRole("checkbox")[0];
  if (input !== undefined && control) await setValue(user, control as Control, input);
  await user.click(confirmButton(dialog));
}

/** Clicks the bulk action button named `label` in the "Actions" toolbar, then
 *  confirms if a dialog opens. One click, every record. */
export async function runBulkAction(user: UserEvent, label: string): Promise<void> {
  const group = screen.getByRole("group", { name: "Actions" });
  await user.click(within(group).getByRole("button", { name: nameIs(label) }));
  if (screen.queryByRole("dialog")) await confirmDialog(user);
}

/** Clicks the filter chip whose accessible name starts with `label`. */
export async function chooseFilter(user: UserEvent, label: string | RegExp): Promise<void> {
  const chips = screen.getAllByRole("button").filter((b) => b.hasAttribute("aria-pressed"));
  const chip = chips.find((b) => (typeof label === "string" ? nameOf(b).startsWith(label) : label.test(nameOf(b))));
  if (!chip) throw new Error(`No filter named ${String(label)}. Filters: ${chips.map(nameOf).join(" | ")}`);
  await user.click(chip);
}

/** Types into the search box. */
export async function search(user: UserEvent, query: string): Promise<void> {
  const box = screen.queryByRole("searchbox") ?? labelled(document.body, `Search ${current().config.copy.nounPlural}`, true);
  await setValue(user, box as Control, query);
}

/** A browser refresh: unmounts and re-renders against the SAME repository. */
export function reload(): void {
  remount(current());
}

/** Writes junk into the app's storage key to exercise recovery, then reloads. */
export function corruptStorage(value = "{not json"): void {
  const harness = current();
  harness.storage.setItem(harness.config.storageKey, value);
  harness.repository = createLocalRepository(harness.config, harness.storage);
  remount(harness);
}

/** The row for that record, by accessible name; throws listing the visible titles. */
export function row(title: string | RegExp): HTMLElement {
  const found = screen.queryAllByRole("listitem").find((li) => (typeof title === "string" ? nameOf(li) === title : title.test(nameOf(li))));
  if (!found) throw new Error(`No record named ${String(title)}. Visible: ${rowTitles().join(", ") || "(none)"}`);
  return found;
}

/** Visible row titles, in display order. The workhorse assertion. */
export function rowTitles(): string[] {
  return screen.queryAllByRole("listitem").map((li) => li.getAttribute("aria-label") ?? "").filter((t) => t !== "");
}

export function expectRow(title: string, ...text: string[]): void {
  const element = row(title);
  for (const fragment of text) expect(element).toHaveTextContent(fragment);
}

export function expectNoRow(title: string): void {
  expect(rowTitles()).not.toContain(title);
}

/** Text of the named stat tile: stat("Running low") === "2". */
export function stat(label: string | RegExp): string {
  return (labelled(screen.getByRole("status", { name: "Summary" }), label, false).textContent ?? "").trim();
}

/** Fills any field in the open dialog, then clicks its confirm button. */
export async function confirmDialog(user: UserEvent, values?: Values): Promise<void> {
  const dialog = screen.getByRole("dialog");
  if (values) await fillWithin(user, dialog, values);
  await user.click(confirmButton(dialog));
}
