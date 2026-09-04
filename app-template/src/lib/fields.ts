import type {
  AnyConfig, FieldDef, RecordValue, Row, RowPatch,
} from "./config-types.js";

export type { Draft } from "./fields-validate.js";
export { validateField, validateDraft } from "./fields-validate.js";

import type { Draft } from "./fields-validate.js";

export function fieldByName(config: AnyConfig, name: string): FieldDef | undefined {
  return config.fields.find((field) => field.name === name);
}

/** Fields the add/edit form renders. */
export function formFields(config: AnyConfig): readonly FieldDef[] {
  return config.fields.filter((field) => field.inForm !== false);
}

/** Fields a row's meta line may show. */
export function listFields(config: AnyConfig): readonly FieldDef[] {
  return config.fields.filter((field) => field.inList !== false);
}

function initialFor(field: FieldDef): string {
  switch (field.kind) {
    case "number":
      return String(field.initial ?? "");
    case "select":
      return field.initial ?? field.options[0] ?? "";
    case "boolean":
      return field.initial === true ? "true" : "";
    case "date":
      return field.initial === "today"
        ? new Date().toISOString().slice(0, 10)
        : "";
    default:
      return "";
  }
}

export function initialDraft(fields: readonly FieldDef[]): Draft {
  const draft: Draft = {};
  for (const field of fields) draft[field.name] = initialFor(field);
  return draft;
}

export function draftFromRow(fields: readonly FieldDef[], row: Row): Draft {
  const draft: Draft = {};
  for (const field of fields) {
    const value: RecordValue | undefined = row[field.name];
    if (value === undefined) draft[field.name] = initialFor(field);
    else if (field.kind === "boolean") draft[field.name] = value === true ? "true" : "";
    else draft[field.name] = String(value);
  }
  return draft;
}

/** Draft strings to the values a record stores. */
export function toValues(fields: readonly FieldDef[], draft: Draft): RowPatch {
  const values: Record<string, RecordValue> = {};
  for (const field of fields) {
    const raw = draft[field.name] ?? "";
    if (field.kind === "number") {
      const parsed = Number(raw.trim());
      values[field.name] = Number.isNaN(parsed) ? 0 : parsed;
    } else if (field.kind === "boolean") {
      values[field.name] = raw === "true";
    } else if (field.kind === "longtext") {
      values[field.name] = raw;
    } else {
      values[field.name] = raw.trim();
    }
  }
  return values;
}

/** A unit that is a currency symbol -- or any single non-alphanumeric
 *  character -- reads before the number with no space: "£40". Every other unit
 *  reads after it with one space: "40 pts". One rule, so a field, a derived
 *  value and a stat tile never disagree about the same money. */
function unitLeads(unit: string): boolean {
  return "£$€¥".includes(unit) || (unit.length === 1 && !/[a-z0-9]/i.test(unit));
}

/** A number as the user reads it, with its unit in the right place. */
export function formatNumber(value: number | string, unit?: string): string {
  const text = String(value);
  if (unit === undefined || unit === "") return text;
  return unitLeads(unit) ? `${unit}${text}` : `${text} ${unit}`;
}

/** One stored value as the user reads it. */
export function formatValue(field: FieldDef, value: RecordValue | undefined): string {
  if (value === undefined) return "";
  switch (field.kind) {
    case "number":
      return formatNumber(String(value), field.unit);
    case "boolean":
      return value === true ? "Yes" : "No";
    default:
      return String(value);
  }
}

/** Anything loaded from storage becomes a value of the field's own type. */
export function coerceStored(field: FieldDef, value: unknown): RecordValue {
  switch (field.kind) {
    case "number":
      return typeof value === "number" && Number.isFinite(value)
        ? value
        : field.initial ?? 0;
    case "select":
      return typeof value === "string" && field.options.includes(value)
        ? value
        : field.initial ?? field.options[0] ?? "";
    case "boolean":
      return value === true;
    default:
      return typeof value === "string" ? value : "";
  }
}
