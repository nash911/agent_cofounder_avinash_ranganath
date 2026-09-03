import { useId, type ReactNode } from "react";
import type { FieldDef } from "../lib/config-types.js";

export interface FieldProps {
  readonly field: FieldDef;
  readonly value: string;
  readonly error?: string;
  readonly autoFocus?: boolean;
  onChange(value: string): void;
}

/** One `FieldDef` rendered as one labelled control. Every control is named by
 *  a real `<label htmlFor>`, so tests and screen readers find it the same way.
 *  Errors are `role="alert"` and wired through `aria-describedby`. */
export function Field({ field, value, error, autoFocus, onChange }: FieldProps) {
  const base = useId();
  const id = `${base}c`;
  const helpId = `${base}h`;
  const errId = `${base}e`;
  const describedBy = [field.help ? helpId : "", error ? errId : ""]
    .filter((part) => part !== "")
    .join(" ");
  const common = {
    id,
    className: "field__control",
    autoFocus,
    "aria-invalid": error ? true : undefined,
    "aria-required": field.required ? true : undefined,
    "aria-describedby": describedBy === "" ? undefined : describedBy,
  };

  let control: ReactNode;
  if (field.kind === "longtext") {
    control = (
      <textarea
        {...common}
        rows={3}
        value={value}
        placeholder={field.placeholder}
        maxLength={field.maxLength}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  } else if (field.kind === "number") {
    control = (
      <input
        {...common}
        type="number"
        inputMode="numeric"
        min={field.min}
        max={field.max}
        step={field.step}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  } else if (field.kind === "select") {
    control = (
      <select {...common} value={value} onChange={(event) => onChange(event.target.value)}>
        {field.required ? null : <option value="">Choose…</option>}
        {field.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  } else if (field.kind === "boolean") {
    control = (
      <input
        {...common}
        type="checkbox"
        checked={value === "true"}
        onChange={(event) => onChange(event.target.checked ? "true" : "")}
      />
    );
  } else if (field.kind === "date") {
    control = (
      <input
        {...common}
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  } else {
    control = (
      <input
        {...common}
        type="text"
        placeholder={field.placeholder}
        maxLength={field.maxLength}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }

  const label = (
    <label className="field__label" htmlFor={id}>
      {field.label}
      {field.required ? (
        <span className="field__required" aria-hidden="true">
          *
        </span>
      ) : null}
    </label>
  );

  return (
    <div className="field" data-kind={field.kind}>
      {field.kind === "boolean" ? control : label}
      {field.kind === "boolean" ? label : control}
      {field.kind === "number" && field.unit ? (
        <span className="field__unit">{field.unit}</span>
      ) : null}
      {field.help ? (
        <p className="field__help" id={helpId}>
          {field.help}
        </p>
      ) : null}
      {error ? (
        <p className="field__error" id={errId} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
