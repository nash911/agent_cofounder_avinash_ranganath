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
