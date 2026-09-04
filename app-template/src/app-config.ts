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
