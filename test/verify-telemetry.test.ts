import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  collectSessionFiles,
  combineAndSortCalls,
  deriveCallsFromDirectCallsContent,
  deriveCallsFromSessionContent,
  reconcile,
  verifyTelemetry,
  type CallLogEntryLike,
  type ResultLike,
} from "../tools/verify-telemetry.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true })));
});

async function temporaryDirectory(): Promise<string> {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-cofounder-verify-telemetry-"));
  temporaryDirectories.push(directory);
  return directory;
}

function assistantMessageEntry(
  timestamp: string,
  provider: string,
  model: string,
  usage: { input: number; output: number; cacheRead: number; cacheWrite: number; totalTokens: number },
): string {
  return JSON.stringify({
    type: "message",
    id: "abc",
    parentId: null,
    timestamp,
    message: { role: "assistant", provider, model, usage, api: "openai-completions", stopReason: "stop" },
  });
}

function toolResultEntry(timestamp: string, toolName: string, usage: unknown): string {
  const message: Record<string, unknown> = { role: "toolResult", toolCallId: "call-1", toolName, isError: false };
  if (usage !== undefined) message.usage = usage;
  return JSON.stringify({ type: "message", id: "def", parentId: "abc", timestamp, message });
}

function directCallLine(
  timestampMs: number,
  provider: string,
  model: string,
  usage: { input: number; output: number; cacheRead: number; cacheWrite: number; totalTokens: number },
  attempt: number,
): string {
  return JSON.stringify({
    ts: new Date(timestampMs).toISOString(),
    timestamp: timestampMs,
    call_id: `call-${attempt}`,
    attempt,
    label: "analyst",
    model,
    provider,
    status: 200,
    latency_s: 1.2,
    error: null,
    request_meta: { messages_sha256: "deadbeef", messages_chars: 100, roles: ["system", "user"], params: {} },
    response_body: { ok: true },
    usage,
  });
}

function buildCallLog(entries: Array<Omit<CallLogEntryLike, "index">>): CallLogEntryLike[] {
  return entries.map((entry, index) => ({ index: index + 1, ...entry }));
}

describe("deriveCallsFromSessionContent", () => {
  it("counts assistant messages, ignores tool results with no usage", () => {
    const content = [
      JSON.stringify({ type: "session", version: 3, id: "s1", timestamp: "2026-09-03T00:00:00.000Z" }),
      assistantMessageEntry("2026-09-03T00:00:01.000Z", "berget", "zai-org/GLM-5.2", {
        input: 100,
        output: 10,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 110,
      }),
      toolResultEntry("2026-09-03T00:00:02.000Z", "bash", undefined),
      JSON.stringify({
        type: "message",
        id: "usr",
        parentId: null,
        timestamp: "2026-09-03T00:00:03.000Z",
        message: { role: "user", content: "next" },
      }),
      assistantMessageEntry("2026-09-03T00:00:04.000Z", "berget", "zai-org/GLM-5.2", {
        input: 200,
        output: 20,
        cacheRead: 10,
        cacheWrite: 0,
        totalTokens: 230,
      }),
    ].join("\n");

    const calls = deriveCallsFromSessionContent(content);
    expect(calls).toHaveLength(2);
    expect(calls.map((call) => call.model)).toEqual(["berget/zai-org/GLM-5.2", "berget/zai-org/GLM-5.2"]);
    expect(calls[0]?.input_tokens).toBe(100);
    expect(calls[1]?.cache_read_tokens).toBe(10);
  });

  it("counts a tool result that carries nested LLM usage", () => {
    const content = toolResultEntry("2026-09-03T00:00:00.000Z", "custom-reviewer", {
      input: 5,
      output: 1,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 6,
    });
    const calls = deriveCallsFromSessionContent(content);
    expect(calls).toEqual([
      {
        model: "tool:custom-reviewer",
        input_tokens: 5,
        output_tokens: 1,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        total_tokens: 6,
        timestamp_ms: Date.parse("2026-09-03T00:00:00.000Z"),
      },
    ]);
  });

  it("counts a compaction entry carrying usage as pi-compaction", () => {
    const content = JSON.stringify({
      type: "compaction",
      id: "c1",
      parentId: "abc",
      timestamp: "2026-09-03T00:00:05.000Z",
      summary: "...",
      tokensBefore: 50_000,
      usage: { input: 40, output: 8, cacheRead: 0, cacheWrite: 0, totalTokens: 48 },
    });
    const calls = deriveCallsFromSessionContent(content);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.model).toBe("pi-compaction");
  });

  it("ignores malformed lines", () => {
    expect(deriveCallsFromSessionContent("not json\n\n")).toEqual([]);
  });
});

describe("deriveCallsFromDirectCallsContent", () => {
  it("derives one call per attempt, model as provider/model", () => {
    const content = [
      directCallLine(1_000, "berget", "zai-org/GLM-5.2", { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15 }, 1),
      directCallLine(2_000, "berget", "zai-org/GLM-5.2", { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 }, 1),
    ].join("\n");
    const calls = deriveCallsFromDirectCallsContent(content);
    expect(calls).toHaveLength(2);
    expect(calls[0]?.model).toBe("berget/zai-org/GLM-5.2");
    expect(calls[1]?.input_tokens).toBe(0);
  });
});

describe("collectSessionFiles", () => {
  it("recursively lists .jsonl files and returns [] when the directory is absent", async () => {
    const directory = await temporaryDirectory();
    await mkdir(path.join(directory, "sessions", "1-builder"), { recursive: true });
    await writeFile(path.join(directory, "sessions", "1-builder", "a.jsonl"), "", "utf8");
    await writeFile(path.join(directory, "sessions", "note.txt"), "", "utf8");

    const files = collectSessionFiles(path.join(directory, "sessions"));
    expect(files).toEqual([path.join(directory, "sessions", "1-builder", "a.jsonl")]);
    expect(collectSessionFiles(path.join(directory, "does-not-exist"))).toEqual([]);
  });
});

describe("combineAndSortCalls", () => {
  it("orders calls by timestamp across sources, stable on ties", () => {
    const early = { model: "a", input_tokens: 1, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 1, timestamp_ms: 100 };
    const late = { model: "b", input_tokens: 2, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 2, timestamp_ms: 200 };
    const tie = { model: "c", input_tokens: 3, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 3, timestamp_ms: 100 };
    expect(combineAndSortCalls([late], [early, tie]).map((call) => call.model)).toEqual(["a", "c", "b"]);
  });
});

describe("reconcile", () => {
  it("matches when the derived calls and result.json call_log agree", () => {
    const calls = [
      { model: "berget/glm", input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 15, timestamp_ms: 1 },
    ];
    const result: ResultLike = {
      model_calls: 1,
      input_tokens: 10,
      output_tokens: 5,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 15,
      call_log: buildCallLog([
        { model: "berget/glm", input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 15 },
      ]),
    };
    expect(reconcile(calls, result)).toEqual({ ok: true, message: expect.stringContaining("OK") });
  });

  it("names the first differing call on a call_log mismatch", () => {
    const calls = [
      { model: "berget/glm", input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 15, timestamp_ms: 1 },
      { model: "berget/glm", input_tokens: 20, output_tokens: 6, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 26, timestamp_ms: 2 },
    ];
    const result: ResultLike = {
      model_calls: 2,
      input_tokens: 30,
      output_tokens: 11,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 41,
      call_log: buildCallLog([
        { model: "berget/glm", input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 15 },
        { model: "berget/glm", input_tokens: 999, output_tokens: 6, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 1_005 },
      ]),
    };
    const outcome = reconcile(calls, result);
    expect(outcome.ok).toBe(false);
    expect(outcome.message).toContain("call index 2");
    expect(outcome.message).toContain("input_tokens");
  });

  it("names the differing total when a running total disagrees", () => {
    const calls = [
      { model: "berget/glm", input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 15, timestamp_ms: 1 },
    ];
    const result: ResultLike = {
      model_calls: 1,
      input_tokens: 999,
      output_tokens: 5,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 15,
      call_log: buildCallLog([
        { model: "berget/glm", input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 15 },
      ]),
    };
    const outcome = reconcile(calls, result);
    expect(outcome.ok).toBe(false);
    expect(outcome.message).toContain("input_tokens mismatch");
  });
});

describe("verifyTelemetry end to end", () => {
  it("exits ok on a matching temp run dir: 2 assistant messages, 1 usage-less tool result, and 2 direct-call attempts", async () => {
    const runDir = await temporaryDirectory();
    await mkdir(path.join(runDir, "sessions", "1-builder"), { recursive: true });
    await mkdir(path.join(runDir, "harness"), { recursive: true });

    const sessionContent = [
      JSON.stringify({ type: "session", version: 3, id: "s1", timestamp: "2026-09-03T00:00:00.000Z" }),
      assistantMessageEntry("2026-09-03T00:00:01.000Z", "berget", "zai-org/GLM-5.2", {
        input: 100,
        output: 10,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 110,
      }),
      toolResultEntry("2026-09-03T00:00:02.000Z", "bash", undefined),
      assistantMessageEntry("2026-09-03T00:00:03.000Z", "berget", "zai-org/GLM-5.2", {
        input: 200,
        output: 20,
        cacheRead: 5,
        cacheWrite: 0,
        totalTokens: 225,
      }),
    ].join("\n");
    await writeFile(path.join(runDir, "sessions", "1-builder", "s1.jsonl"), sessionContent, "utf8");

    const directContent = [
      directCallLine(500, "berget", "zai-org/GLM-5.2", { input: 5, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 7 }, 1),
      directCallLine(1_500, "berget", "zai-org/GLM-5.2", { input: 6, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 9 }, 1),
    ].join("\n");
    await writeFile(path.join(runDir, "harness", "direct-calls.jsonl"), directContent, "utf8");

    const result: ResultLike = {
      model_calls: 4,
      input_tokens: 5 + 100 + 6 + 200,
      output_tokens: 2 + 10 + 3 + 20,
      cache_read_tokens: 0 + 0 + 0 + 5,
      cache_write_tokens: 0,
      total_tokens: 7 + 110 + 9 + 225,
      // Chronological order: the two direct-gateway attempts carry small
      // epoch-ms timestamps (500, 1_500); both session assistant messages
      // carry 2026 ISO timestamps, so they sort after both direct calls.
      call_log: buildCallLog([
        { model: "berget/zai-org/GLM-5.2", input_tokens: 5, output_tokens: 2, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 7 },
        { model: "berget/zai-org/GLM-5.2", input_tokens: 6, output_tokens: 3, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 9 },
        { model: "berget/zai-org/GLM-5.2", input_tokens: 100, output_tokens: 10, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 110 },
        { model: "berget/zai-org/GLM-5.2", input_tokens: 200, output_tokens: 20, cache_read_tokens: 5, cache_write_tokens: 0, total_tokens: 225 },
      ]),
    };
    const resultPath = path.join(runDir, "result.json");
    await writeFile(resultPath, JSON.stringify(result), "utf8");

    const matching = await verifyTelemetry(runDir, resultPath);
    expect(matching.ok).toBe(true);

    // Perturb one call_log entry: no longer reconciles, names the index.
    const perturbedCallLog = structuredClone(result);
    const secondCall = perturbedCallLog.call_log[1];
    if (secondCall) secondCall.input_tokens = 101;
    await writeFile(resultPath, JSON.stringify(perturbedCallLog), "utf8");
    const perturbedOutcome = await verifyTelemetry(runDir, resultPath);
    expect(perturbedOutcome.ok).toBe(false);
    expect(perturbedOutcome.message).toContain("call index 2");

    // Perturb a running total instead: names the total, not a call index.
    const perturbedTotal = structuredClone(result);
    perturbedTotal.output_tokens = 999;
    await writeFile(resultPath, JSON.stringify(perturbedTotal), "utf8");
    const totalOutcome = await verifyTelemetry(runDir, resultPath);
    expect(totalOutcome.ok).toBe(false);
    expect(totalOutcome.message).toContain("output_tokens mismatch");
  });

  it("treats a missing direct-calls.jsonl as zero direct calls, not an error", async () => {
    const runDir = await temporaryDirectory();
    await mkdir(path.join(runDir, "sessions", "1-builder"), { recursive: true });
    const sessionContent = assistantMessageEntry("2026-09-03T00:00:01.000Z", "berget", "zai-org/GLM-5.2", {
      input: 1,
      output: 1,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 2,
    });
    await writeFile(path.join(runDir, "sessions", "1-builder", "s1.jsonl"), sessionContent, "utf8");
    const result: ResultLike = {
      model_calls: 1,
      input_tokens: 1,
      output_tokens: 1,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 2,
      call_log: buildCallLog([
        { model: "berget/zai-org/GLM-5.2", input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 2 },
      ]),
    };
    const resultPath = path.join(runDir, "result.json");
    await writeFile(resultPath, JSON.stringify(result), "utf8");

    const outcome = await verifyTelemetry(runDir, resultPath);
    expect(outcome.ok).toBe(true);
  });
});
