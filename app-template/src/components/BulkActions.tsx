import type { AnyConfig, BulkActionDef, FieldDef } from "../lib/config-types.js";

type AnyBulkAction = BulkActionDef<readonly FieldDef[]>;

export interface BulkActionsProps {
  readonly config: AnyConfig;
  onRun(action: AnyBulkAction): void;
}

/** The toolbar for actions that touch every record at once. One button per
 *  `bulkActions` entry, named by its own label — nothing else distinguishes it
 *  from a row button, so a reader (and a test) can name it directly. */
export function BulkActions({ config, onRun }: BulkActionsProps) {
  const actions = config.bulkActions ?? [];
  if (actions.length === 0) return null;

  return (
    <div className="bulk-actions" role="group" aria-label="Actions">
      {actions.map((action) => (
        <button
          key={action.id}
          type="button"
          className="btn"
          onClick={() => onRun(action)}
        >
          {action.label}
        </button>
      ))}
    </div>
  );
}
