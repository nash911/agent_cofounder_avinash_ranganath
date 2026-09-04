import type {
  AnyConfig, FieldDef, Filter, RecordValue, Row, Tone,
} from "./config-types.js";
import { fieldByName, formatNumber } from "./fields.js";

export interface FilterOption {
  readonly id: string;        // "all" | `field:${value}` | `state:${stateId}`
  readonly label: string;
  readonly count: number;
}
export interface ResolvedBadge { readonly id: string; readonly text: string; readonly tone: Tone }
export interface ResolvedDerived {
  readonly name: string; readonly label: string; readonly value: string;
}
export interface ResolvedStat {
  readonly id: string; readonly label: string;
  readonly value: string; readonly emphasis: boolean;
}

type FieldFilter = Extract<Filter<readonly FieldDef[]>, { kind: "field" }>;
type StateFilter = Extract<Filter<readonly FieldDef[]>, { kind: "state" }>;

function fieldFilters(config: AnyConfig): FieldFilter[] {
  return (config.filters ?? []).filter((f): f is FieldFilter => f.kind === "field");
}
function stateFilters(config: AnyConfig): StateFilter[] {
  return (config.filters ?? []).filter((f): f is StateFilter => f.kind === "state");
}

export function titleOf(config: AnyConfig, row: Row): string {
  return String(row[config.titleField] ?? "");
}

/** Case-insensitive substring over text, longtext and select values only. */
export function matchesSearch(config: AnyConfig, row: Row, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return config.fields.some((field) => {
    if (field.kind !== "text" && field.kind !== "longtext" && field.kind !== "select") {
      return false;
    }
    return String(row[field.name] ?? "").toLowerCase().includes(needle);
  });
}

function valuesForField(
  field: FieldDef | undefined, name: string, rows: readonly Row[],
): string[] {
  if (field !== undefined && field.kind === "select") return [...field.options];
  const seen: string[] = [];
  for (const row of rows) {
    const value = String(row[name] ?? "");
    if (value !== "" && !seen.includes(value)) seen.push(value);
  }
  return seen;
}

/** Counts run over the unsearched rows so they stay stable while typing. */
export function filterOptions(config: AnyConfig, rows: readonly Row[]): FilterOption[] {
  const options: FilterOption[] = [{
    id: "all",
    label: fieldFilters(config)[0]?.allLabel ?? "All",
    count: rows.length,
  }];
  for (const filter of fieldFilters(config)) {
    const field = fieldByName(config, filter.field);
    for (const value of valuesForField(field, filter.field, rows)) {
      const id = `field:${value}`;   // no field name in the id: one chip per value
      if (options.some((option) => option.id === id)) continue;
      options.push({
        id, label: value,
        count: rows.filter((row) => String(row[filter.field] ?? "") === value).length,
      });
    }
  }
  for (const filter of stateFilters(config)) {
    options.push({
      id: `state:${filter.id}`,
      label: filter.label,
      count: rows.filter((row) => filter.match(row)).length,
    });
  }
  return options;
}

/** The field a `field:` id narrows on: the select that declares the value,
 *  else a field some row actually holds it in, else the first field filter. */
function fieldNameFor(config: AnyConfig, value: string, rows: readonly Row[]): string | undefined {
  const candidates = fieldFilters(config);
  for (const filter of candidates) {
    const field = fieldByName(config, filter.field);
    if (field !== undefined && field.kind === "select" && field.options.includes(value)) {
      return filter.field;
    }
  }
  for (const filter of candidates) {
    if (rows.some((row) => String(row[filter.field] ?? "") === value)) return filter.field;
  }
  return candidates[0]?.field;
}

export function applyFilter(
  config: AnyConfig, rows: readonly Row[], filterId: string,
): Row[] {
  if (filterId.startsWith("field:")) {
    const value = filterId.slice("field:".length);
    const name = fieldNameFor(config, value, rows);
    if (name === undefined) return [...rows];
    return rows.filter((row) => String(row[name] ?? "") === value);
  }
  if (filterId.startsWith("state:")) {
    const id = filterId.slice("state:".length);
    const filter = stateFilters(config).find((entry) => entry.id === id);
    if (filter === undefined) return [...rows];
    return rows.filter((row) => filter.match(row));
  }
  return [...rows];
}

export function emptyTextFor(config: AnyConfig, filterId: string): string | undefined {
  if (filterId.startsWith("state:")) {
    const id = filterId.slice("state:".length);
    return stateFilters(config).find((entry) => entry.id === id)?.emptyText;
  }
  if (filterId.startsWith("field:")) {
    const declared = fieldFilters(config).find((entry) => entry.emptyText !== undefined);
    return declared?.emptyText ?? `No ${config.copy.nounPlural} in this view.`;
  }
  return undefined;
}

function compareValues(field: FieldDef, a: RecordValue, b: RecordValue): number {
  if (field.kind === "number") return Number(a) - Number(b);
  if (field.kind === "boolean") return Number(a === true) - Number(b === true);
  return String(a).localeCompare(String(b), undefined, {
    sensitivity: "base", numeric: true,
  });
}

/** Stable. Ties break by `createdAt` ascending; no `sort` means newest first. */
export function sortRows(config: AnyConfig, rows: readonly Row[]): Row[] {
  const sort = config.sort;
  const field = sort === undefined ? undefined : fieldByName(config, sort.field);
  const sign = sort?.direction === "desc" ? -1 : 1;
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      let primary = 0;
      if (sort === undefined) primary = b.row.createdAt - a.row.createdAt;
      else if (field !== undefined) {
        primary = sign * compareValues(field, a.row[sort.field], b.row[sort.field]);
      }
      if (primary !== 0) return primary;
      const stamp = a.row.createdAt - b.row.createdAt;
      return stamp !== 0 ? stamp : a.index - b.index;
    })
    .map((entry) => entry.row);
}

export function badgesFor(config: AnyConfig, row: Row): ResolvedBadge[] {
  return (config.badges ?? [])
    .filter((badge) => badge.when(row))
    .map((badge) => ({ id: badge.id, text: badge.text(row), tone: badge.tone ?? "neutral" }));
}

/** A number carries its unit the way a number field does ("£40", "40 pts");
 *  a string is already the text the reader wants. */
function withUnit(value: number | string, unit?: string): string {
  return typeof value === "number" ? formatNumber(value, unit) : String(value);
}

/** Values computed from a row and never stored. Recomputed on every render, so
 *  they cannot go stale the way a hand-maintained field would. */
export function derivedFor(config: AnyConfig, row: Row): ResolvedDerived[] {
  return (config.derived ?? []).map((entry) => ({
    name: entry.name,
    label: entry.label,
    value: withUnit(entry.compute(row), entry.unit),
  }));
}

export function computeStats(config: AnyConfig, rows: readonly Row[]): ResolvedStat[] {
  return (config.summary ?? []).map((stat) => ({
    id: stat.id,
    label: stat.label,
    value: withUnit(stat.compute(rows), stat.unit),
    emphasis: stat.emphasis ?? false,
  }));
}
