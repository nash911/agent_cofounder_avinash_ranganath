import { useState, type FormEvent } from "react";
import { Dialog } from "./Dialog.js";
import { Field } from "./Field.js";
import { titleOf } from "../lib/collection.js";
import type {
  ActionDef, AnyConfig, BulkActionDef, FieldDef, Row,
} from "../lib/config-types.js";
import { validateField } from "../lib/fields.js";

type AnyAction = ActionDef<readonly FieldDef[]>;
type AnyBulkAction = BulkActionDef<readonly FieldDef[]>;

export interface ConfirmDialogProps {
  readonly title: string;
  readonly body?: string;
  /** Renders one control and validates it before confirming. */
  readonly field?: FieldDef;
  readonly confirmLabel: string;
  readonly destructive?: boolean;
  onConfirm(input: string): void;
  onCancel(): void;
}

export function ConfirmDialog(props: ConfirmDialogProps) {
  const { title, body, field, confirmLabel, destructive, onConfirm, onCancel } = props;
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | undefined>(undefined);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (field) {
      const problem = validateField(field, value);
      if (problem !== undefined) {
        setError(problem);
        return;
      }
    }
    onConfirm(value);
  }

  return (
    <Dialog title={title} onClose={onCancel}>
      <form onSubmit={submit} noValidate>
        <div className="dialog__body">
          {body ? <p>{body}</p> : null}
          {field ? (
            <Field
              field={field}
              value={value}
              error={error}
              onChange={(next) => {
                setValue(next);
                if (error !== undefined) setError(validateField(field, next));
              }}
            />
          ) : null}
        </div>
        <div className="dialog__actions">
          <button type="button" className="btn btn--ghost" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="submit"
            className={destructive ? "btn btn--danger" : "btn btn--primary"}
            disabled={field?.required === true && value.trim() === ""}
          >
            {confirmLabel}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/** What `App.tsx` is waiting on: a delete that needs confirming, an action
 *  that declared `input` and/or `confirm`, or a bulk action that declared
 *  `confirm`. Actions with neither never reach here — they run straight
 *  away. */
export type Pending =
  | { readonly kind: "delete"; readonly row: Row }
  | { readonly kind: "action"; readonly action: AnyAction; readonly row: Row }
  | { readonly kind: "bulk"; readonly action: AnyBulkAction };

function inputField(input: NonNullable<AnyAction["input"]>): FieldDef {
  if (input.kind === "number") {
    return {
      kind: "number", name: "input", label: input.label,
      required: input.required, min: input.min,
    };
  }
  return { kind: "text", name: "input", label: input.label, required: input.required };
}

export interface PendingDialogProps {
  readonly config: AnyConfig;
  readonly pending: Pending;
  onResolve(input: string): void;
  onCancel(): void;
}

/** Turns a `Pending` into `ConfirmDialog` props, so `App.tsx` carries none of
 *  this wiring. */
export function PendingDialog({ config, pending, onResolve, onCancel }: PendingDialogProps) {
  if (pending.kind === "bulk") {
    return (
      <ConfirmDialog
        title={pending.action.label}
        body={pending.action.confirm}
        confirmLabel={pending.action.label}
        onConfirm={() => onResolve("")}
        onCancel={onCancel}
      />
    );
  }
  const title = titleOf(config, pending.row);
  if (pending.kind === "delete") {
    return (
      <ConfirmDialog
        title={`Delete ${title}?`}
        body={`This removes ${title} from your ${config.copy.nounPlural}.`}
        confirmLabel="Delete"
        destructive
        onConfirm={() => onResolve("")}
        onCancel={onCancel}
      />
    );
  }
  const { action } = pending;
  return (
    <ConfirmDialog
      title={`${action.label} ${title}`}
      body={action.confirm}
      field={action.input ? inputField(action.input) : undefined}
      confirmLabel={action.label}
      destructive={action.tone === "danger"}
      onConfirm={onResolve}
      onCancel={onCancel}
    />
  );
}
