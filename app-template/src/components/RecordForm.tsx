import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Field } from "./Field.js";
import type { AnyConfig, Row, RowPatch } from "../lib/config-types.js";
import {
  draftFromRow, formFields, initialDraft, toValues, validateDraft, type Draft,
} from "../lib/fields.js";

export interface RecordFormProps {
  readonly config: AnyConfig;
  readonly mode: "add" | "edit";
  readonly initial?: Row;
  onSubmit(values: RowPatch): void;
  onCancel?(): void;
}

/** Add/edit form over `formFields(config)`. Validates on submit, then on every
 *  change; the submit button is never disabled, because a disabled button
 *  hides the messages. Mount it with a `key` so switching rows re-seeds it. */
export function RecordForm({ config, mode, initial, onSubmit, onCancel }: RecordFormProps) {
  const fields = formFields(config);
  const [draft, setDraft] = useState<Draft>(() =>
    mode === "edit" && initial ? draftFromRow(fields, initial) : initialDraft(fields),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const [invalidAt, setInvalidAt] = useState(0);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (invalidAt === 0) return;
    formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [invalidAt]);

  function change(name: string, value: string): void {
    const next: Draft = { ...draft, [name]: value };
    setDraft(next);
    if (submitted) setErrors(validateDraft(fields, next));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const found = validateDraft(fields, draft);
    setSubmitted(true);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      setInvalidAt((tick) => tick + 1);
      return;
    }
    onSubmit(toValues(fields, draft));
    if (mode === "add") {
      setDraft(initialDraft(fields));
      setSubmitted(false);
      setErrors({});
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLFormElement>): void {
    if (event.key === "Escape" && mode === "edit" && onCancel) {
      event.preventDefault();
      onCancel();
    }
  }

  const failing = fields.filter((field) => errors[field.name] !== undefined);
  const submitLabel = mode === "edit" ? "Save changes" : config.copy.addLabel;

  return (
    <section className="panel">
      <h2 className="panel__title">
        {mode === "edit" ? `Edit ${config.copy.noun}` : config.copy.addLabel}
      </h2>
      <form className="form" ref={formRef} noValidate onSubmit={handleSubmit} onKeyDown={handleKeyDown}>
        {submitted && failing.length > 0 ? (
          <div className="form__summary" role="alert">
            <p>
              {failing.length === 1
                ? "Fix 1 problem before saving."
                : `Fix ${failing.length} problems before saving.`}
            </p>
            <ul>
              {failing.map((field) => (
                <li key={field.name}>{`${field.label}: ${errors[field.name]}`}</li>
              ))}
            </ul>
          </div>
        ) : null}
        <div className="form__grid">
          {fields.map((field, index) => (
            <Field
              key={field.name}
              field={field}
              value={draft[field.name] ?? ""}
              error={errors[field.name]}
              autoFocus={mode === "edit" && index === 0}
              onChange={(value) => change(field.name, value)}
            />
          ))}
        </div>
        <div className="form__actions">
          <button type="submit" className="btn btn--primary">
            {submitLabel}
          </button>
          {mode === "edit" && onCancel ? (
            <button type="button" className="btn btn--ghost" onClick={onCancel}>
              Cancel
            </button>
          ) : null}
        </div>
      </form>
    </section>
  );
}
