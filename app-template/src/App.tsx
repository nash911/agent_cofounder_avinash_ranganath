import { useEffect, useMemo, useState } from "react";
import { appConfig } from "./app-config.js";
import { BulkActions } from "./components/BulkActions.js";
import { PendingDialog, type Pending } from "./components/ConfirmDialog.js";
import { FilterBar } from "./components/FilterBar.js";
import { RecordForm } from "./components/RecordForm.js";
import { RecordList } from "./components/RecordList.js";
import { Summary } from "./components/Summary.js";
import { Toast } from "./components/Toast.js";
import { applyFilter, emptyTextFor, matchesSearch } from "./lib/collection.js";
import { validateConfig } from "./lib/config-validate.js";
import { erase, type ActionDef, type AnyConfig, type BulkActionDef, type FieldDef, type Row, type RowPatch } from "./lib/config-types.js";
import { createLocalRepository, type Repository } from "./lib/repository.js";
import { useRecords } from "./lib/use-records.js";

export interface AppProps {
  /** Tests inject a config; the app defaults to the declaration in `src/app-config.ts`. */
  readonly config?: AnyConfig;
  /** Tests inject a memory repository. */
  readonly repository?: Repository;
}

/** The whole product, rendered from one declaration. Nothing here knows what
 *  the records are; every domain decision lives in `src/app-config.ts`. */
export function App({ config, repository }: AppProps = {}) {
  const cfg = config ?? erase(appConfig);
  // Created once, never in an effect: StrictMode double-invokes effects and a
  // repository built twice would clobber storage on mount.
  const [repo] = useState(() => repository ?? createLocalRepository(cfg));
  const problems = useMemo(() => validateConfig(cfg), [cfg]);
  const api = useRecords(cfg, repo);
  const [query, setQuery] = useState("");
  const [activeFilter, setActiveFilter] = useState("all");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  useEffect(() => {
    document.title = cfg.copy.title;
  }, [cfg]);

  const editing = api.rows.find((row) => row.id === editingId);
  const visible = useMemo(
    () => applyFilter(cfg, api.rows, activeFilter).filter((row) => matchesSearch(cfg, row, query)),
    [cfg, api.rows, activeFilter, query],
  );
  const narrowed = activeFilter !== "all" || query.trim() !== "";

  function submit(values: RowPatch): void {
    if (editing) {
      api.edit(editing.id, values);
      setEditingId(null);
    } else {
      api.add(values);
    }
  }

  function requestDelete(row: Row): void {
    if (cfg.undoDelete === false) setPending({ kind: "delete", row });
    else api.remove(row.id);
  }

  function requestAction(action: ActionDef<readonly FieldDef[]>, row: Row): void {
    if (action.input || action.confirm) setPending({ kind: "action", action, row });
    else api.runAction(action, row, "");
  }

  function requestBulkAction(action: BulkActionDef<readonly FieldDef[]>): void {
    if (action.confirm) setPending({ kind: "bulk", action });
    else api.runBulkAction(action);
  }

  function resolvePending(input: string): void {
    if (pending === null) return;
    if (pending.kind === "delete") api.remove(pending.row.id);
    else if (pending.kind === "bulk") api.runBulkAction(pending.action);
    else api.runAction(pending.action, pending.row, input);
    setPending(null);
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <h1 className="brand">{cfg.copy.title}</h1>
        <p className="tagline">{cfg.copy.tagline}</p>
      </header>
      {problems.length > 0 ? (
        <div className="notice notice--warn">
          <p>This configuration has problems. The app is still running.</p>
          <ul>
            {problems.map((problem) => (
              <li key={`${problem.where}:${problem.message}`}>
                {`${problem.where}: ${problem.message}`}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {api.notice ? (
        <div className="notice notice--warn">
          <p>{api.notice}</p>
          <button type="button" className="btn btn--ghost" aria-label="Dismiss notice" onClick={api.dismissNotice}>
            Dismiss
          </button>
        </div>
      ) : null}
      <Summary config={cfg} rows={api.rows} />
      <BulkActions config={cfg} onRun={requestBulkAction} />
      <FilterBar
        config={cfg}
        rows={api.rows}
        query={query}
        activeFilter={activeFilter}
        onQuery={setQuery}
        onFilter={setActiveFilter}
      />
      <main className="layout" id="main" tabIndex={-1}>
        <RecordForm
          key={editing ? editing.id : "add"}
          config={cfg}
          mode={editing ? "edit" : "add"}
          initial={editing}
          onSubmit={submit}
          onCancel={() => setEditingId(null)}
        />
        <RecordList
          config={cfg}
          rows={visible}
          emptyKind={narrowed ? "filtered" : "none"}
          emptyText={emptyTextFor(cfg, activeFilter)}
          onEdit={(row) => setEditingId(row.id)}
          onDelete={requestDelete}
          onAction={requestAction}
        />
      </main>
      {pending ? (
        <PendingDialog
          config={cfg}
          pending={pending}
          onResolve={resolvePending}
          onCancel={() => setPending(null)}
        />
      ) : null}
      {api.toast ? (
        <Toast text={api.toast.text} action={api.toast.action} onDismiss={api.dismissToast} />
      ) : null}
    </div>
  );
}
