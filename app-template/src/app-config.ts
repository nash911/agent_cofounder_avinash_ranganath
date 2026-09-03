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
