import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react";

const FOCUSABLE = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function focusableIn(root: HTMLElement | null): HTMLElement[] {
  return root === null ? [] : Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
}

export interface DialogProps {
  readonly title: string;
  onClose(): void;
  readonly children: ReactNode;
}

/** A modal built from a plain `div[role="dialog"][aria-modal="true"]`.
 *  Never the native modal element: jsdom implements neither it nor its
 *  imperative open method, so under the test runner it would never appear at
 *  all. Focus moves in on open, is trapped by Tab, and returns to the
 *  invoking element on close. */
export function Dialog({ title, onClose, children }: DialogProps) {
  const titleId = useId();
  const surfaceRef = useRef<HTMLDivElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (returnTo.current === null) {
      const active = document.activeElement;
      returnTo.current = active instanceof HTMLElement ? active : null;
    }
    const first = focusableIn(surfaceRef.current)[0];
    (first ?? surfaceRef.current)?.focus();
    return () => returnTo.current?.focus();
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusableIn(surfaceRef.current);
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === surfaceRef.current)) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  return (
    <div
      className="dialog__backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={surfaceRef}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <h2 className="dialog__title" id={titleId}>
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
