export interface EmptyStateProps {
  readonly title: string;
  readonly body: string;
  readonly actionLabel?: string;
  onAction?(): void;
}

/** Shown instead of the list when there is nothing to show. Always says what
 *  the reader can do next, never just "no results". */
export function EmptyState({ title, body, actionLabel, onAction }: EmptyStateProps) {
  return (
    <div className="empty">
      <h3 className="empty__title">{title}</h3>
      <p className="empty__body">{body}</p>
      {actionLabel && onAction ? (
        <button type="button" className="btn btn--ghost" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
}
