import { Component, type ErrorInfo, type ReactNode } from "react";

export interface ErrorBoundaryProps {
  readonly children: ReactNode;
  /** Keys beginning with this prefix are removed by "Clear saved data". */
  readonly storageKey?: string;
}

interface ErrorBoundaryState {
  readonly error: Error | null;
}

/** Catches a render error anywhere below it and offers the two recoveries that
 *  actually work in a single-user browser app: reload, or drop the saved data
 *  that is making the app throw and start again. */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Unhandled render error", error, info.componentStack);
  }

  private reload(): void {
    try {
      window.location.reload();
    } catch {
      this.setState({ error: null });
    }
  }

  private clearAndRestart(): void {
    const prefix = this.props.storageKey;
    try {
      const doomed: string[] = [];
      for (let index = 0; index < window.localStorage.length; index += 1) {
        const key = window.localStorage.key(index);
        if (key !== null && (prefix === undefined || key.startsWith(prefix))) doomed.push(key);
      }
      for (const key of doomed) window.localStorage.removeItem(key);
    } catch {
      // Storage is unavailable; reloading is still worth trying.
    }
    this.reload();
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <div className="error-card" role="alert">
        <h1>Something went wrong</h1>
        <p>{error.message || "The application stopped unexpectedly."}</p>
        <div className="form__actions">
          <button type="button" className="btn btn--primary" onClick={() => this.reload()}>
            Reload
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => this.clearAndRestart()}>
            Clear saved data and restart
          </button>
        </div>
      </div>
    );
  }
}
