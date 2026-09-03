import type { AnyConfig, RecordValue, Row, RowPatch } from "./config-types.js";
import type { StorageLike } from "./repository-storage.js";
import { coerceStored } from "./fields.js";
import {
  decode, encode, makeId, memoryStorage, safeStorage, valuesFor,
} from "./repository-storage.js";

export type { StorageLike } from "./repository-storage.js";
export { STORAGE_VERSION, safeStorage, makeId } from "./repository-storage.js";

const WRITE_FAILED =
  "Changes are not being saved. Your browser storage is unavailable or full.";
const UNREADABLE =
  "Saved data could not be read. It has been set aside and the list has started empty.";

export interface RepositoryStatus {
  /** false once a write has failed. */
  readonly ok: boolean;
  /** true when stored data was unreadable and has been set aside. */
  readonly recovered: boolean;
  /** Human notice for the UI banner; undefined when there is nothing to say. */
  readonly message?: string;
}

export interface Repository {
  list(): Row[];
  create(values: RowPatch): Row;
  update(id: string, values: RowPatch): Row | undefined;
  remove(id: string): { row: Row; index: number } | undefined;
  restore(row: Row, index: number): void;
  replaceAll(rows: readonly Row[]): void;
  clear(): void;
  status(): RepositoryStatus;
  subscribe(listener: () => void): () => void;
}

/** Persists to `config.storageKey`, or to an internal map when the browser
 *  refuses storage — in which case it reports `ok: false` from the start. */
export function createLocalRepository(
  config: AnyConfig,
  storage?: StorageLike,
): Repository {
  const found = storage ?? safeStorage();
  return createStore(config, found ?? memoryStorage(), found === undefined);
}

export function createMemoryRepository(
  config: AnyConfig,
  seed?: readonly RowPatch[],
): Repository {
  const repository = createStore(config, memoryStorage(), false);
  for (const values of seed ?? []) repository.create(values);
  return repository;
}

function createStore(
  config: AnyConfig,
  storage: StorageLike,
  unavailable: boolean,
): Repository {
  const key = config.storageKey;
  const listeners = new Set<() => void>();
  let rows: Row[] = [];
  let ok = !unavailable;
  let recovered = false;
  let detached = unavailable;
  let lastRaw: string | null = null;

  /** Re-read whenever the stored text differs from what we last read or wrote,
   *  so a page that corrupts storage behind us is noticed on the next read. */
  function sync(): void {
    if (detached) return;
    let raw: string | null;
    try { raw = storage.getItem(key); } catch { detached = true; ok = false; return; }
    if (raw === lastRaw) return;
    lastRaw = raw;
    const result = decode(config, raw);
    rows = result.rows;
    if (!result.corrupt || raw === null) return;
    recovered = true;
    try { storage.setItem(`${key}.corrupt`, raw); } catch { /* best effort */ }
  }

  function commit(): void {
    if (!detached) {
      const payload = encode(rows);
      try { storage.setItem(key, payload); lastRaw = payload; }
      catch { detached = true; ok = false; }
    }
    for (const listener of [...listeners]) listener();
  }

  sync();

  return {
    list() { sync(); return rows; },
    create(values) {
      sync();
      const row: Row = {
        ...valuesFor(config, values), id: makeId(), createdAt: Date.now(),
      };
      rows = [...rows, row];
      commit();
      return row;
    },
    update(id, values) {
      sync();
      const index = rows.findIndex((row) => row.id === id);
      if (index === -1) return undefined;
      const current = rows[index];
      const patch: Record<string, RecordValue> = {};
      for (const field of config.fields) {
        const given = values[field.name];
        if (given !== undefined) patch[field.name] = coerceStored(field, given);
      }
      const next: Row = {
        ...current, ...patch, id: current.id, createdAt: current.createdAt,
      };
      rows = [...rows.slice(0, index), next, ...rows.slice(index + 1)];
      commit();
      return next;
    },
    remove(id) {
      sync();
      const index = rows.findIndex((row) => row.id === id);
      if (index === -1) return undefined;
      const row = rows[index];
      rows = [...rows.slice(0, index), ...rows.slice(index + 1)];
      commit();
      return { row, index };
    },
    restore(row, index) {
      sync();
      const at = Math.max(0, Math.min(index, rows.length));
      rows = [...rows.slice(0, at), row, ...rows.slice(at)];
      commit();
    },
    replaceAll(next) { sync(); rows = [...next]; commit(); },
    clear() {
      rows = [];
      if (!detached) {
        try { storage.removeItem(key); lastRaw = null; }
        catch { detached = true; ok = false; }
      }
      for (const listener of [...listeners]) listener();
    },
    status() {
      const message = !ok ? WRITE_FAILED : recovered ? UNREADABLE : undefined;
      return { ok, recovered, message };
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}
