import type { FieldDef } from "./config-types.js";

/** Forms carry every value as a string; conversion happens at the boundary. */
export type Draft = Record<string, string>;

/** The default message for the first rule the raw value breaks. */
function defaultProblem(field: FieldDef, raw: string): string | undefined {
  const label = field.label;
  const trimmed = raw.trim();
  if (field.required === true && trimmed === "") return `${label} is required.`;
  if (trimmed === "") return undefined;

  switch (field.kind) {
    case "number": {
      const parsed = Number(trimmed);
      if (Number.isNaN(parsed)) return `${label} must be a number.`;
      if (field.integer === true && !Number.isInteger(parsed)) {
        return `${label} must be a whole number.`;
      }
      if (field.min !== undefined && parsed < field.min) {
        return `${label} must be at least ${field.min}.`;
      }
      if (field.max !== undefined && parsed > field.max) {
        return `${label} must be at most ${field.max}.`;
      }
      return undefined;
    }
    case "text":
    case "longtext": {
      const value = field.kind === "longtext" ? raw : trimmed;
      if (field.maxLength !== undefined && value.length > field.maxLength) {
        return `${label} must be ${field.maxLength} characters or fewer.`;
      }
      return undefined;
    }
    case "select":
      if (!field.options.includes(trimmed)) return `Choose a ${label.toLowerCase()}.`;
      return undefined;
    case "date":
      if (!/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return `${label} must be a date.`;
      return undefined;
    default:
      return undefined;
  }
}

/** `undefined` means the value is acceptable. A field's own `message`, when
 *  present, replaces every message that field could produce. */
export function validateField(field: FieldDef, raw: string): string | undefined {
  const problem = defaultProblem(field, raw);
  if (problem === undefined) return undefined;
  return field.message ?? problem;
}

/** `{}` means the whole draft is acceptable. */
export function validateDraft(
  fields: readonly FieldDef[],
  draft: Draft,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const problem = validateField(field, draft[field.name] ?? "");
    if (problem !== undefined) errors[field.name] = problem;
  }
  return errors;
}
