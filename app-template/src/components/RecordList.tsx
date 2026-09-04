import { Badge } from "./Badge.js";
import { EmptyState } from "./EmptyState.js";
import { badgesFor, derivedFor, titleOf } from "../lib/collection.js";
import type { ActionDef, AnyConfig, FieldDef, Row } from "../lib/config-types.js";
import { fieldByName, formatValue, listFields } from "../lib/fields.js";

type AnyAction = ActionDef<readonly FieldDef[]>;

export interface RecordListProps {
  readonly config: AnyConfig;
  readonly rows: readonly Row[];
  readonly emptyKind: "none" | "filtered";
  readonly emptyText?: string;
  onEdit(row: Row): void;
  onDelete(row: Row): void;
  onAction(action: AnyAction, row: Row): void;
}

function resolve(config: AnyConfig, names: readonly string[]): FieldDef[] {
  return names
    .map((name) => fieldByName(config, name))
    .filter((field): field is FieldDef => field !== undefined);
}

/** The meta line falls back to every listable field that is not already the
 *  title or a subtitle, so a config that declares no `metaFields` still
 *  shows its data. */
function metaNames(config: AnyConfig): readonly string[] {
  if (config.metaFields) return config.metaFields;
  const subtitles = config.subtitleFields ?? [];
  return listFields(config)
    .map((field) => field.name)
    .filter((name) => name !== config.titleField && !subtitles.includes(name));
}

export function RecordList(props: RecordListProps) {
  const { config, rows, emptyKind, emptyText, onEdit, onDelete, onAction } = props;
  const subtitleFields = resolve(config, config.subtitleFields ?? []);
  const metaFields = resolve(config, metaNames(config));
  const actions = config.actions ?? [];

  return (
    <section className="panel">
      <h2 className="panel__title">{config.copy.nounPlural}</h2>
      {rows.length === 0 ? (
        <EmptyState
          title={emptyKind === "none" ? config.copy.emptyTitle : "Nothing matches this view"}
          body={
            emptyKind === "none"
              ? config.copy.emptyBody
              : emptyText ?? "Try a different filter."
          }
        />
      ) : (
        <ul className="list">
          {rows.map((row) => {
            const title = titleOf(config, row);
            const badges = badgesFor(config, row);
            const derived = derivedFor(config, row);
            const flag = badges.find((badge) => badge.tone === "warn" || badge.tone === "alert");
            const subtitle = subtitleFields
              .map((field) => formatValue(field, row[field.name]))
              .filter((text) => text !== "")
              .join(" · ");
            return (
              <li
                key={row.id}
                className={flag ? "row row--flag" : "row"}
                aria-label={title}
                data-tone={flag?.tone}
              >
                <h3 className="row__title">{title}</h3>
                {subtitle === "" ? null : <p className="row__sub">{subtitle}</p>}
                {metaFields.length === 0 && derived.length === 0 ? null : (
                  <dl className="row__meta">
                    {metaFields.map((field) => (
                      <div key={`field:${field.name}`}>
                        <dt>{field.label}</dt>{" "}
                        <dd>{formatValue(field, row[field.name])}</dd>
                      </div>
                    ))}
                    {derived.map((entry) => (
                      <div key={`derived:${entry.name}`}>
                        <dt>{entry.label}</dt>{" "}
                        <dd>{entry.value}</dd>
                      </div>
                    ))}
                  </dl>
                )}
                {badges.length === 0 ? null : (
                  <div className="row__badges">
                    {badges.map((badge) => (
                      <Badge key={badge.id} tone={badge.tone}>
                        {badge.text}
                      </Badge>
                    ))}
                  </div>
                )}
                <div className="row__actions">
                  {config.allowEdit === false ? null : (
                    <button
                      type="button"
                      className="btn btn--ghost"
                      aria-label={`Edit ${title}`}
                      onClick={() => onEdit(row)}
                    >
                      Edit
                    </button>
                  )}
                  {actions
                    .filter((action) => (action.available ? action.available(row) : true))
                    .map((action) => (
                      <button
                        key={action.id}
                        type="button"
                        className={action.tone === "danger" ? "btn btn--danger" : "btn"}
                        aria-label={`${action.label} ${title}`}
                        onClick={() => onAction(action, row)}
                      >
                        {action.label}
                      </button>
                    ))}
                  {config.allowDelete === false ? null : (
                    <button
                      type="button"
                      className="btn btn--ghost"
                      aria-label={`Delete ${title}`}
                      onClick={() => onDelete(row)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
