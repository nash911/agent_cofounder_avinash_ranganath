import type { AnyConfig, RecordValue, Row, RowPatch } from "./config-types.js";
import { coerceStored } from "./fields.js";

export const STORAGE_VERSION = 1;

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** localStorage when it is present AND writable; undefined in private mode. */
export function safeStorage(): StorageLike | undefined {
  try {
    if (typeof window === "undefined") return undefined;
    const storage = window.localStorage;
    if (!storage) return undefined;
    const probe = "__storage_probe__";
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return storage;
  } catch {
    return undefined;
  }
}

let idCounter = 0;

export function makeId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `r${++idCounter}-${Math.random().toString(36).slice(2, 10)}`;
}

export function memoryStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => { map.set(key, value); },
    removeItem: (key) => { map.delete(key); },
  };
}

/** Every declared field, coerced from `source`; nothing else. */
export function valuesFor(
  config: AnyConfig,
  source: RowPatch,
): Record<string, RecordValue> {
  const values: Record<string, RecordValue> = {};
  for (const field of config.fields) {
    values[field.name] = coerceStored(field, source[field.name]);
  }
  return values;
}

/** One stored entry to a row, or undefined when it is beyond repair. Every
 *  declared field is coerced; every undeclared key is discarded. */
function hydrate(config: AnyConfig, entry: unknown): Row | undefined {
  if (entry === null || typeof entry !== "object") return undefined;
  const source = entry as Record<string, unknown>;
  if (typeof source.id !== "string") return undefined;
  const stamp = source.createdAt;
  const createdAt =
    typeof stamp === "number" && Number.isFinite(stamp) ? stamp : Date.now();
  const values: Record<string, RecordValue> = {};
  for (const field of config.fields) {
    values[field.name] = coerceStored(field, source[field.name]);
  }
  return { ...values, id: source.id, createdAt };
}

export interface Decoded {
  readonly rows: Row[];
  /** true when the whole payload was unreadable and must be set aside. */
  readonly corrupt: boolean;
}

/** Accepts `{version,rows}` and a bare legacy array. A malformed individual
 *  row is dropped on its own; one bad row never loses the collection. */
export function decode(config: AnyConfig, raw: string | null): Decoded {
  if (raw === null) return { rows: [], corrupt: false };
  let payload: unknown;
  try { payload = JSON.parse(raw); } catch { return { rows: [], corrupt: true }; }
  let entries: unknown = payload;
  if (!Array.isArray(payload)) {
    if (payload === null || typeof payload !== "object") {
      return { rows: [], corrupt: true };
    }
    entries = (payload as { rows?: unknown }).rows;
  }
  if (!Array.isArray(entries)) return { rows: [], corrupt: true };
  const rows: Row[] = [];
  for (const entry of entries) {
    const row = hydrate(config, entry);
    if (row !== undefined) rows.push(row);
  }
  return { rows, corrupt: false };
}

export function encode(rows: readonly Row[]): string {
  return JSON.stringify({ version: STORAGE_VERSION, rows });
}
