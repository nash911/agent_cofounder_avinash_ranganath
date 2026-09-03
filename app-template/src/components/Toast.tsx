export interface ToastAction {
  readonly label: string;
  run(): void;
}

export interface ToastProps {
  readonly text: string;
  readonly action?: ToastAction;
  onDismiss(): void;
}

/** A polite live region. It is mounted only while there is something to say,
 *  so assistive technology announces the text as it appears. The action
 *  button carries its own label ("Undo") as its accessible name. */
export function Toast({ text, action, onDismiss }: ToastProps) {
  return (
    <div className="toast-region" role="status" aria-live="polite">
      <div className="toast">
        <span className="toast__text">{text}</span>
        {action ? (
          <button
            type="button"
            className="btn btn--ghost toast__action"
            onClick={() => {
              action.run();
              onDismiss();
            }}
          >
            {action.label}
          </button>
        ) : null}
        <button
          type="button"
          className="btn btn--ghost"
          aria-label="Dismiss notification"
          onClick={onDismiss}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
