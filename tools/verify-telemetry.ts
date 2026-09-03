/**
 * Contract C9: independent reconciliation of a judged run's telemetry.
 *
 * The organizer ruling (2026-09-03) makes the per-invocation files —
 * `<run>/sessions/**\/*.jsonl` (Pi RPC sessions) and
 * `<run>/harness/direct-calls.jsonl` (direct-gateway attempts, contract C2) —
 * the audit artifact for the BYO track, not `events.jsonl`. This tool
 * re-derives the call list from those files alone, orders it by timestamp,
 * and compares it against `result.json`'s `call_log` (index order) plus the
 * six running totals and `model_calls`. It never reads `events.jsonl`.
 *
 * Usage: `npm run verify:telemetry -- [runDir] [resultPath]`
 * Defaults: the latest `artifacts/runs/*` directory; `./result.json`.
 * Exit 0 with a one-line summary on a match; exit 1 naming the first
 * differing call (index, expected vs actual) or the total that differs.
 */
import { readdirSync, type Dirent } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SOURCE_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SOURCE_DIRECTORY, "..");

export interface DerivedCall {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  timestamp_ms: number;
}

interface UsageLike {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  totalTokens: number;
}

export interface CallLogEntryLike {
  index: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

export interface ResultLike {
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  call_log: CallLogEntryLike[];
}

export interface ReconciliationResult {
  ok: boolean;
  message: string;
}

function finiteNonNegative(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isUsageLike(value: unknown): value is UsageLike {
  if (typeof value !== "object" || value === null) return false;
  const usage = value as Record<string, unknown>;
  return (
    finiteNonNegative(usage.input) &&
    finiteNonNegative(usage.output) &&
    finiteNonNegative(usage.cacheRead) &&
    finiteNonNegative(usage.cacheWrite) &&
    finiteNonNegative(usage.totalTokens)
  );
}

function usageToCall(usage: UsageLike, model: string, timestampMs: number): DerivedCall {
  return {
    model,
    input_tokens: usage.input,
    output_tokens: usage.output,
    cache_read_tokens: usage.cacheRead,
    cache_write_tokens: usage.cacheWrite,
    total_tokens: usage.totalTokens,
    timestamp_ms: timestampMs,
  };
}

function parseJsonLines(content: string): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  for (const line of content.split(/\r?\n/u)) {
    if (line.trim() === "") continue;
    try {
      const parsed: unknown = JSON.parse(line);
      if (typeof parsed === "object" && parsed !== null) records.push(parsed as Record<string, unknown>);
    } catch {
      // Malformed lines stay in the raw audit artifact; ignored for reconciliation.
    }
  }
  return records;
}

/**
 * Pi session entries (`docs/session-format.md`): `message` entries with
 * `message.role === "assistant"` (model `<provider>/<model>`) or
 * `"toolResult"` carrying `usage` (model `tool:<name>`); `compaction`
 * entries carrying a top-level `usage` (model `pi-compaction`). Everything
 * else — `session`, `model_change`, `thinking_level_change`, user messages,
 * tool results with no nested LLM usage — contributes nothing.
 */
export function deriveCallsFromSessionContent(content: string): DerivedCall[] {
  const calls: DerivedCall[] = [];
  for (const record of parseJsonLines(content)) {
    const rawTimestamp = record.timestamp;
    const parsedTimestamp = typeof rawTimestamp === "string" ? Date.parse(rawTimestamp) : Number.NaN;
    const timestampMs = Number.isFinite(parsedTimestamp) ? parsedTimestamp : 0;

    if (record.type === "message") {
      const message = record.message;
      if (typeof message !== "object" || message === null) continue;
      const entry = message as Record<string, unknown>;
      if (entry.role === "assistant" && isUsageLike(entry.usage)) {
        const provider = typeof entry.provider === "string" ? entry.provider : "unknown";
        const model = typeof entry.model === "string" ? entry.model : "unknown";
        calls.push(usageToCall(entry.usage, `${provider}/${model}`, timestampMs));
      } else if (entry.role === "toolResult" && isUsageLike(entry.usage)) {
        const toolName = typeof entry.toolName === "string" ? entry.toolName : "unknown";
        calls.push(usageToCall(entry.usage, `tool:${toolName}`, timestampMs));
      }
      continue;
    }

    if (record.type === "compaction" && isUsageLike(record.usage)) {
      calls.push(usageToCall(record.usage, "pi-compaction", timestampMs));
    }
  }
  return calls;
}

/**
 * `<run>/harness/direct-calls.jsonl` (contract C2): one raw record per HTTP
 * attempt, `usage` already shaped like a Pi `Usage` object (all-zero, still
 * counted, on a failed attempt with no provider usage).
 */
export function deriveCallsFromDirectCallsContent(content: string): DerivedCall[] {
  const calls: DerivedCall[] = [];
  for (const record of parseJsonLines(content)) {
    if (!isUsageLike(record.usage)) continue;
    const provider = typeof record.provider === "string" ? record.provider : "unknown";
    const model = typeof record.model === "string" ? record.model : "unknown";
    const timestampMs = typeof record.timestamp === "number" ? record.timestamp : 0;
    calls.push(usageToCall(record.usage, `${provider}/${model}`, timestampMs));
  }
  return calls;
}

/** Recursively lists `*.jsonl` files under `root` (a `sessions/` directory); `[]` if absent. */
export function collectSessionFiles(root: string): string[] {
  let entries: Dirent[];
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch {
    return [];
  }
  const files: string[] = [];
  for (const entry of entries) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...collectSessionFiles(full));
    else if (entry.isFile() && entry.name.endsWith(".jsonl")) files.push(full);
  }
  return files.sort();
}

/** Every derived call, across every source, ordered by timestamp (stable for ties). */
export function combineAndSortCalls(...groups: DerivedCall[][]): DerivedCall[] {
  return groups
    .flat()
    .map((call, sequence) => ({ call, sequence }))
    .sort((a, b) => a.call.timestamp_ms - b.call.timestamp_ms || a.sequence - b.sequence)
    .map(({ call }) => call);
}

const TOTAL_FIELDS = [
  "model_calls",
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "total_tokens",
] as const;

const CALL_FIELDS = [
  "model",
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "total_tokens",
] as const;

/** Compares the derived call list against `result.json`'s `call_log` and totals. */
export function reconcile(derived: DerivedCall[], result: ResultLike): ReconciliationResult {
  const totals: Record<(typeof TOTAL_FIELDS)[number], number> = {
    model_calls: derived.length,
    input_tokens: derived.reduce((sum, call) => sum + call.input_tokens, 0),
    output_tokens: derived.reduce((sum, call) => sum + call.output_tokens, 0),
    cache_read_tokens: derived.reduce((sum, call) => sum + call.cache_read_tokens, 0),
    cache_write_tokens: derived.reduce((sum, call) => sum + call.cache_write_tokens, 0),
    total_tokens: derived.reduce((sum, call) => sum + call.total_tokens, 0),
  };

  for (const field of TOTAL_FIELDS) {
    if (totals[field] !== result[field]) {
      return {
        ok: false,
        message: `verify:telemetry: ${field} mismatch: derived ${totals[field]} vs result.json ${result[field]}`,
      };
    }
  }

  const maxLength = Math.max(derived.length, result.call_log.length);
  for (let position = 0; position < maxLength; position += 1) {
    const index = position + 1;
    const expected = derived[position];
    const actual = result.call_log[position];
    if (!expected || !actual) {
      return {
        ok: false,
        message:
          `verify:telemetry: call index ${index} mismatch: derived has ` +
          `${expected ? "an entry" : "none"}, result.json has ${actual ? "an entry" : "none"}`,
      };
    }
    for (const field of CALL_FIELDS) {
      if (expected[field] !== actual[field]) {
        return {
          ok: false,
          message:
            `verify:telemetry: call index ${index} field "${field}" mismatch: ` +
            `expected ${JSON.stringify(expected[field])}, result.json has ${JSON.stringify(actual[field])}`,
        };
      }
    }
  }

  return {
    ok: true,
    message:
      `verify:telemetry OK: ${derived.length} calls reconciled ` +
      `(input=${totals.input_tokens} output=${totals.output_tokens} cache_read=${totals.cache_read_tokens} ` +
      `cache_write=${totals.cache_write_tokens} total=${totals.total_tokens})`,
  };
}

/** Re-derives the call list for `runDir` and reconciles it against `resultPath`. */
export async function verifyTelemetry(runDir: string, resultPath: string): Promise<ReconciliationResult> {
  const sessionFiles = collectSessionFiles(path.join(runDir, "sessions"));
  const sessionCalls: DerivedCall[] = [];
  for (const file of sessionFiles) {
    sessionCalls.push(...deriveCallsFromSessionContent(await readFile(file, "utf8")));
  }

  let directCalls: DerivedCall[] = [];
  try {
    const directCallsContent = await readFile(path.join(runDir, "harness", "direct-calls.jsonl"), "utf8");
    directCalls = deriveCallsFromDirectCallsContent(directCallsContent);
  } catch {
    directCalls = [];
  }

  const derived = combineAndSortCalls(sessionCalls, directCalls);
  const result = JSON.parse(await readFile(resultPath, "utf8")) as ResultLike;
  return reconcile(derived, result);
}

/** The most recently created `artifacts/runs/<id>` directory (ISO ids sort chronologically). */
export function latestRunDirectory(repositoryRoot: string): string {
  const runsRoot = path.join(repositoryRoot, "artifacts", "runs");
  const entries = readdirSync(runsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
  const latest = entries.at(-1);
  if (latest === undefined) throw new Error(`No run directories found under ${runsRoot}`);
  return path.join(runsRoot, latest);
}

async function main(): Promise<void> {
  const runDirArgument = process.argv[2];
  const resultPathArgument = process.argv[3];
  const runDir = runDirArgument ? path.resolve(runDirArgument) : latestRunDirectory(REPOSITORY_ROOT);
  const resultPath = resultPathArgument ? path.resolve(resultPathArgument) : path.resolve("result.json");

  const outcome = await verifyTelemetry(runDir, resultPath);
  if (outcome.ok) {
    console.log(outcome.message);
    return;
  }
  console.error(outcome.message);
  process.exitCode = 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
