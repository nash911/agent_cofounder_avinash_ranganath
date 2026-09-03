import type { AnyConfig } from "./config-types.js";

export interface ConfigProblem { readonly where: string; readonly message: string }

/** Cross-references the type system cannot reach once the field tuple is
 *  erased. One problem per line, human-readable, never throws. */
export function validateConfig(config: AnyConfig): ConfigProblem[] {
  const problems: ConfigProblem[] = [];
  const names = config.fields.map((field) => field.name);
  const declared = names.join(", ");
  const known = new Set(names);

  const unknown = (name: string, inWhat: string, where: string): void => {
    problems.push({
      where,
      message: `Unknown field "${name}" in ${inWhat}. Declared fields: ${declared}.`,
    });
  };

  const seenName = new Set<string>();
  config.fields.forEach((field, index) => {
    if (seenName.has(field.name)) {
      problems.push({
        where: `fields[${index}]`,
        message: `Duplicate field name "${field.name}". Every field name must be unique.`,
      });
    }
    seenName.add(field.name);
    if (field.kind === "select" && field.options.length === 0) {
      problems.push({
        where: `fields[${index}].options`,
        message: `Select field "${field.name}" has no options. Add at least one option.`,
      });
    }
  });

  const titleField = config.fields.find((field) => field.name === config.titleField);
  if (titleField === undefined) unknown(config.titleField, "titleField", "titleField");
  else if (titleField.kind === "number" || titleField.kind === "boolean") {
    problems.push({
      where: "titleField",
      message: `titleField "${titleField.name}" is a ${titleField.kind} field. A row heading reads better from a text or select field.`,
    });
  }

  for (const [key, list] of [
    ["subtitleFields", config.subtitleFields],
    ["metaFields", config.metaFields],
  ] as const) {
    (list ?? []).forEach((name, index) => {
      if (!known.has(name)) unknown(name, key, `${key}[${index}]`);
    });
  }

  if (config.sort !== undefined && !known.has(config.sort.field)) {
    unknown(config.sort.field, "sort", "sort.field");
  }

  (config.filters ?? []).forEach((filter, index) => {
    if (filter.kind !== "field") return;
    if (known.has(filter.field)) return;
    unknown(filter.field, `filter "${filter.allLabel ?? filter.field}"`, `filters[${index}]`);
  });

  const seenId = new Set<string>();
  const ids: readonly (readonly [string, readonly { readonly id: string }[]])[] = [
    ["filters", (config.filters ?? []).filter((f) => f.kind === "state")],
    ["badges", config.badges ?? []],
    ["summary", config.summary ?? []],
    ["actions", config.actions ?? []],
  ];
  for (const [where, entries] of ids) {
    for (const entry of entries) {
      const scoped = `${where}:${entry.id}`;
      if (seenId.has(scoped)) {
        problems.push({
          where,
          message: `Duplicate id "${entry.id}" in ${where}. Every id in a group must be unique.`,
        });
      }
      seenId.add(scoped);
    }
  }

  if (!/\.v\d+$/.test(config.storageKey)) {
    problems.push({
      where: "storageKey",
      message: `storageKey "${config.storageKey}" has no version suffix. End it with ".v1" so a later shape change can start clean.`,
    });
  }

  return problems;
}
