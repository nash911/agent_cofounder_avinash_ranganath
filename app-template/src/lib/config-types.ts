export type FieldKind = "text" | "longtext" | "number" | "select" | "boolean" | "date";

/** Every value the application stores. Never null: unset text is "". */
export type RecordValue = string | number | boolean;

interface FieldBase {
  /** Key on the record. camelCase, no spaces. */
  readonly name: string;
  /** Visible label AND the accessible name every test queries by. */
  readonly label: string;
  readonly required?: boolean;
  /** Human message shown when this field is rejected. Overrides the default. */
  readonly message?: string;
  /** Hint under the control, wired via aria-describedby. */
  readonly help?: string;
  /** false = the field exists on the record but never appears on the add/edit
   *  form. Use it for a value only an action may set. Default true. */
  readonly inForm?: boolean;
  /** false = hide from the row's meta line. Default true. */
  readonly inList?: boolean;
}

export interface TextField extends FieldBase {
  readonly kind: "text" | "longtext";
  readonly placeholder?: string;
  readonly maxLength?: number;
}
export interface NumberField extends FieldBase {
  readonly kind: "number";
  readonly min?: number;
  readonly max?: number;
  readonly step?: number;
  readonly integer?: boolean;
  /** Suffix shown after the value, e.g. "left", "kg". */
  readonly unit?: string;
  readonly initial?: number;
}
export interface SelectField extends FieldBase {
  readonly kind: "select";
  readonly options: readonly string[];
  readonly initial?: string;
}
export interface BooleanField extends FieldBase {
  readonly kind: "boolean";
  readonly initial?: boolean;
}
export interface DateField extends FieldBase {
  /** ISO yyyy-mm-dd; "" when unset. */
  readonly kind: "date";
  readonly initial?: "today" | "";
}
export type FieldDef =
  | TextField | NumberField | SelectField | BooleanField | DateField;

// --- inference core: field kind -> value type, field list -> row type ---
export type ValueOfField<F> =
  F extends { kind: "number" } ? number :
  F extends { kind: "boolean" } ? boolean :
  F extends { kind: "select"; options: readonly (infer O)[] } ? O :
  string;

export type RowOf<F extends readonly FieldDef[]> =
  { readonly id: string; readonly createdAt: number } &
  { readonly [K in F[number] as K["name"]]: ValueOfField<K> };

export type Patch<F extends readonly FieldDef[]> =
  Partial<Omit<RowOf<F>, "id" | "createdAt">>;

export type Tone = "neutral" | "info" | "good" | "warn" | "alert";

/** Narrowing: by a stored field's options, or by a derived state predicate. */
export type Filter<F extends readonly FieldDef[]> =
  | { readonly kind: "field"; readonly field: F[number]["name"];
      readonly allLabel?: string }
  | { readonly kind: "state"; readonly id: string; readonly label: string;
      readonly match: (row: RowOf<F>) => boolean;
      readonly emptyText?: string };

/** Emphasis. `text` is what the user reads; `tone` only decorates. */
export interface BadgeDef<F extends readonly FieldDef[]> {
  readonly id: string;
  readonly when: (row: RowOf<F>) => boolean;
  readonly text: (row: RowOf<F>) => string;
  readonly tone?: Tone;
}

export interface StatDef<F extends readonly FieldDef[]> {
  readonly id: string;
  readonly label: string;
  readonly compute: (rows: readonly RowOf<F>[]) => number | string;
  /** Render large and accented. Use it for the figure the idea asks to see. */
  readonly emphasis?: boolean;
}

/** A row button that is neither Edit nor Delete. */
export interface ActionDef<F extends readonly FieldDef[]> {
  readonly id: string;
  readonly label: string;
  /** Hide the button when this returns false. Default: always shown. */
  readonly available?: (row: RowOf<F>) => boolean;
  /** Collect one value in a dialog first. Omit for an instant action. */
  readonly input?: { readonly label: string;
                     readonly kind?: "text" | "number";
                     readonly required?: boolean;
                     readonly min?: number };
  /** Ask for confirmation first. Omit for an instant action. */
  readonly confirm?: string;
  /** Return only the fields that change. `input` is "" when there is none. */
  readonly apply: (row: RowOf<F>, input: string) => Patch<F>;
  readonly toast?: (row: RowOf<F>) => string;
  readonly tone?: "default" | "danger";
}

export interface Copy {
  readonly title: string;
  readonly tagline: string;
  readonly noun: string;
  readonly nounPlural: string;
  readonly addLabel: string;
  readonly emptyTitle: string;
  readonly emptyBody: string;
}

export interface AppConfig<F extends readonly FieldDef[] = readonly FieldDef[]> {
  /** localStorage key. Always end it with a version suffix. */
  readonly storageKey: string;
  readonly copy: Copy;
  readonly fields: F;
  /** The field used as each row's heading and as its accessible name. */
  readonly titleField: F[number]["name"];
  readonly subtitleFields?: readonly F[number]["name"][];
  readonly metaFields?: readonly F[number]["name"][];
  readonly sort?: { readonly field: F[number]["name"];
                    readonly direction?: "asc" | "desc" };
  /** Search box over text and select fields. Default true. */
  readonly search?: boolean;
  readonly filters?: readonly Filter<F>[];
  readonly badges?: readonly BadgeDef<F>[];
  readonly summary?: readonly StatDef<F>[];
  readonly actions?: readonly ActionDef<F>[];
  readonly allowEdit?: boolean;      // default true
  readonly allowDelete?: boolean;    // default true
  /** Delete is one click plus an Undo toast. Default true.
   *  Set false to require a confirmation dialog instead. */
  readonly undoDelete?: boolean;
}

export function defineApp<const F extends readonly FieldDef[]>(
  config: AppConfig<F>,
): AppConfig<F> {
  return config;
}

// --- the runtime boundary: erase the inferred field tuple for rendering ---
// Scaffold code renders against these. This is the ONLY cast in the scaffold.
export type AnyConfig = AppConfig<readonly FieldDef[]>;
export type Row = RowOf<readonly FieldDef[]>;
export type RowPatch = Patch<readonly FieldDef[]>;

export function erase<F extends readonly FieldDef[]>(config: AppConfig<F>): AnyConfig {
  return config as unknown as AnyConfig;
}
