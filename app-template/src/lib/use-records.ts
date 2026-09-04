import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ActionDef, AnyConfig, BulkActionDef, FieldDef, Row, RowPatch,
} from "./config-types.js";
import { sortRows, titleOf } from "./collection.js";
import type { Repository, RepositoryStatus } from "./repository.js";

const TOAST_MS = 6000;

export interface ToastState {
  readonly text: string;
  readonly action?: { label: string; run(): void };
}

export interface RecordsApi {
  readonly rows: Row[];
  readonly status: RepositoryStatus;
  readonly notice?: string;
  readonly toast?: ToastState;
  add(values: RowPatch): void;
  edit(id: string, values: RowPatch): void;
  remove(id: string): void;
  runAction(action: ActionDef<readonly FieldDef[]>, row: Row, input: string): void;
  /** Applies one patch to every record the action accepts, in one step. */
  runBulkAction(action: BulkActionDef<readonly FieldDef[]>): void;
  dismissToast(): void;
  dismissNotice(): void;
}

/** The only module that both touches the repository and raises toasts. */
export function useRecords(config: AnyConfig, repository: Repository): RecordsApi {
  const [version, setVersion] = useState(0);
  const [toast, setToast] = useState<ToastState | undefined>(undefined);
  const [dismissed, setDismissed] = useState<string | undefined>(undefined);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const bump = useCallback(() => { setVersion((value) => value + 1); }, []);

  useEffect(() => {
    return repository.subscribe(bump);
  }, [repository, bump]);

  const clearTimer = useCallback(() => {
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = undefined;
  }, []);

  useEffect(() => clearTimer, [clearTimer]);

  const showToast = useCallback((next: ToastState) => {
    clearTimer();
    setToast(next);
    timer.current = setTimeout(() => {
      timer.current = undefined;
      setToast(undefined);
    }, TOAST_MS);
  }, [clearTimer]);

  const dismissToast = useCallback(() => {
    clearTimer();
    setToast(undefined);
  }, [clearTimer]);

  // `version` is the subscription signal; the repository owns the data.
  const rows = useMemo(
    () => sortRows(config, repository.list()),
    [config, repository, version],
  );
  const status = repository.status();
  const notice = status.message === dismissed ? undefined : status.message;

  const add = useCallback((values: RowPatch) => {
    repository.create(values);
  }, [repository]);

  const edit = useCallback((id: string, values: RowPatch) => {
    repository.update(id, values);
  }, [repository]);

  const remove = useCallback((id: string) => {
    const removed = repository.remove(id);
    if (removed === undefined) return;
    const text = `${titleOf(config, removed.row)} deleted.`;
    if (config.undoDelete === false) {
      showToast({ text });
      return;
    }
    showToast({
      text,
      action: { label: "Undo", run: () => { repository.restore(removed.row, removed.index); } },
    });
  }, [config, repository, showToast]);

  const runAction = useCallback((
    action: ActionDef<readonly FieldDef[]>, row: Row, input: string,
  ) => {
    repository.update(row.id, action.apply(row, input));
    const text = action.toast?.(row);
    if (text !== undefined && text !== "") showToast({ text });
  }, [repository, showToast]);

  const runBulkAction = useCallback((
    action: BulkActionDef<readonly FieldDef[]>,
  ) => {
    // Every stored record, not the filtered view: a bulk action means all of
    // them, and a chip left pressed must never silently shrink its reach.
    const targets = repository.list()
      .filter((row) => (action.available ? action.available(row) : true));
    for (const row of targets) repository.update(row.id, action.apply(row));
    showToast({ text: `${action.label} applied to ${targets.length} records` });
  }, [repository, showToast]);

  const dismissNotice = useCallback(() => {
    setDismissed(repository.status().message);
  }, [repository]);

  return {
    rows, status, notice, toast,
    add, edit, remove, runAction, runBulkAction, dismissToast, dismissNotice,
  };
}
