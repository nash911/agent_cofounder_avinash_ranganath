import { describe, expect, it } from "vitest";
import { collectUsageFromJsonLines } from "../src/usage.js";

/**
 * Three real `message_end` lines copied verbatim from
 * `artifacts/runs/2026-09-02T21-55-20-565Z/events.jsonl` (the Phase 1
 * `--agent pi` control run), unmodified.
 */
const REAL_ASSISTANT_MESSAGE_END_LINES: readonly string[] = [
  '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"I\'ll start by reading the skill file to understand the approach, then inspect the current project setup."},{"type":"toolCall","id":"call_c8e7cb6918924bf3b603a874","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/solution/skills/mvp-builder/SKILL.md"}},{"type":"toolCall","id":"call_9ae489331fc3480fa3ae4f5d","name":"bash","arguments":{"command":"ls -la /home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app"}}],"api":"openai-completions","provider":"berget","model":"zai-org/GLM-5.2","usage":{"input":43,"output":98,"cacheRead":2304,"cacheWrite":0,"reasoning":0,"totalTokens":2445,"cost":{"input":6.02e-05,"output":0.0004312,"cacheRead":0.0032256,"cacheWrite":0,"total":0.003717}},"stopReason":"toolUse","timestamp":1788386121006,"responseId":"c4efbd238713419fbf2d92611dc72cd0","rawStopReason":"tool_calls"}}',
  '{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"call_e61f7a9fc38542dabb5c4458","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/package.json"}},{"type":"toolCall","id":"call_e06c4bc83b204eb2bfe6c6c3","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/vite.config.ts"}},{"type":"toolCall","id":"call_ed693c9a55bb4454b84c7562","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/vitest.config.ts"}},{"type":"toolCall","id":"call_dc29095b621d4436805ddc49","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/tsconfig.json"}},{"type":"toolCall","id":"call_58c9e27cede147b7abc97e93","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/index.html"}},{"type":"toolCall","id":"call_b2291d8443854fdaa56de16a","name":"read","arguments":{"path":"/home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/AGENTS.md"}}],"api":"openai-completions","provider":"berget","model":"zai-org/GLM-5.2","usage":{"input":919,"output":212,"cacheRead":2432,"cacheWrite":0,"reasoning":0,"totalTokens":3563,"cost":{"input":0.0012866,"output":0.0009328,"cacheRead":0.0034048,"cacheWrite":0,"total":0.005624199999999999}},"stopReason":"toolUse","timestamp":1788386123973,"responseId":"718a8db415054f629ceec7477b256344","rawStopReason":"tool_calls"}}',
  '{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"call_1889e2ac5a694a6d8648b786","name":"bash","arguments":{"command":"find /home/nash/Dropbox/AgentCofounder/agent_cofounder_avinash_ranganath/output/app/src -type f | head -50"}}],"api":"openai-completions","provider":"berget","model":"zai-org/GLM-5.2","usage":{"input":1161,"output":43,"cacheRead":3520,"cacheWrite":0,"reasoning":0,"totalTokens":4724,"cost":{"input":0.0016254,"output":0.00018920000000000002,"cacheRead":0.004928,"cacheWrite":0,"total":0.0067426}},"stopReason":"toolUse","timestamp":1788386132072,"responseId":"dd5b3273cd974d848da9683f272bd66d","rawStopReason":"tool_calls"}}',
];

/**
 * The Phase 1 control run (`--agent pi`, unguarded, per the ratified
 * deviation) carried no compaction in its captured stream — this session was
 * too short. The shape below is not invented: it is the `compaction_end`
 * fixture already exercised in `test/harness-seam.test.ts` ("sums two
 * session-headed streams...") and documented by `src/usage.ts`'s own
 * `callFromEvent` handling (`record.result.usage`).
 */
const COMPACTION_END_LINE = JSON.stringify({
  type: "compaction_end",
  result: {
    usage: { input: 50, output: 5, cacheRead: 500, cacheWrite: 0, totalTokens: 555, cost: { total: 0.0008 } },
  },
});

/** Contract C1: one synthetic `message_end` per direct-gateway HTTP attempt. */
function syntheticDirectCallLine(options: {
  input: number;
  output: number;
  cacheRead: number;
  callId: string;
  attempt: number;
  providerResponseId: string | null;
  stopReason: "stop" | "error";
  errorMessage?: string;
  timestampMs: number;
}): string {
  const totalTokens = options.input + options.output + options.cacheRead;
  const message: Record<string, unknown> = {
    role: "assistant",
    provider: "berget",
    responseModel: "zai-org/GLM-5.2",
    model: "zai-org/GLM-5.2",
    usage: {
      input: options.input,
      output: options.output,
      cacheRead: options.cacheRead,
      cacheWrite: 0,
      totalTokens,
      reasoning: 0,
      cost: { total: totalTokens * 0.00001 },
    },
    source: "direct-gateway",
    call_id: options.callId,
    attempt: options.attempt,
    provider_response_id: options.providerResponseId,
    stopReason: options.stopReason,
    timestamp: options.timestampMs,
  };
  if (options.errorMessage !== undefined) message.errorMessage = options.errorMessage;
  return JSON.stringify({ type: "message_end", message });
}

const SYNTHETIC_DIRECT_CALL_LINES: readonly string[] = [
  syntheticDirectCallLine({
    input: 10,
    output: 5,
    cacheRead: 0,
    callId: "call-analyst-1",
    attempt: 1,
    providerResponseId: "resp-analyst-1",
    stopReason: "stop",
    timestampMs: 1_788_386_200_000,
  }),
  // A failed attempt: zero usage, but still counted (contract C1: "A failed
  // attempt with no usage emits all-zero usage (still counted)").
  syntheticDirectCallLine({
    input: 0,
    output: 0,
    cacheRead: 0,
    callId: "call-architect-1",
    attempt: 1,
    providerResponseId: null,
    stopReason: "error",
    errorMessage: "connection reset",
    timestampMs: 1_788_386_201_000,
  }),
  // The retried attempt that then succeeded.
  syntheticDirectCallLine({
    input: 12,
    output: 6,
    cacheRead: 2,
    callId: "call-architect-1",
    attempt: 2,
    providerResponseId: "resp-architect-1",
    stopReason: "stop",
    timestampMs: 1_788_386_203_000,
  }),
];

describe("collectUsageFromJsonLines over a mixed real Pi + direct-gateway stream", () => {
  const content = [...REAL_ASSISTANT_MESSAGE_END_LINES, COMPACTION_END_LINE, ...SYNTHETIC_DIRECT_CALL_LINES].join(
    "\n",
  );
  const usage = collectUsageFromJsonLines(content);

  it("counts every real Pi call, the compaction, and every synthetic direct-gateway attempt", () => {
    expect(usage.model_calls).toBe(REAL_ASSISTANT_MESSAGE_END_LINES.length + 1 + SYNTHETIC_DIRECT_CALL_LINES.length);
    expect(usage.model_calls).toBe(7);
  });

  it("sums input, output, cache_read and total tokens across both sources", () => {
    expect(usage.input_tokens).toBe(43 + 919 + 1_161 + 50 + 10 + 0 + 12);
    expect(usage.output_tokens).toBe(98 + 212 + 43 + 5 + 5 + 0 + 6);
    expect(usage.cache_read_tokens).toBe(2_304 + 2_432 + 3_520 + 500 + 0 + 0 + 2);
    expect(usage.cache_write_tokens).toBe(0);
    expect(usage.total_tokens).toBe(2_445 + 3_563 + 4_724 + 555 + 15 + 0 + 20);
  });

  it("keeps the call_log contiguous and indexed from 1", () => {
    expect(usage.call_log.map((call) => call.index)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("labels a synthetic direct-gateway line's model as '<provider>/<responseModel>'", () => {
    expect(usage.call_log[4]?.model).toBe("berget/zai-org/GLM-5.2");
    expect(usage.call_log[5]?.model).toBe("berget/zai-org/GLM-5.2");
    expect(usage.call_log[6]?.model).toBe("berget/zai-org/GLM-5.2");
  });

  it("still counts the failed attempt, with its all-zero usage", () => {
    const failedCall = usage.call_log[5];
    expect(failedCall).toEqual({
      index: 6,
      model: "berget/zai-org/GLM-5.2",
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 0,
      reasoning_tokens: 0,
      cost_total: 0,
    });
  });

  it("ignores the extra contract-C1 fields (source, call_id, attempt, provider_response_id, timestamp)", () => {
    // Every synthetic line carries these fields; none of them leak into the
    // CallLogEntry shape collectUsageFromJsonLines produces.
    for (const call of usage.call_log.slice(4)) {
      expect(call).not.toHaveProperty("source");
      expect(call).not.toHaveProperty("call_id");
      expect(call).not.toHaveProperty("attempt");
      expect(call).not.toHaveProperty("provider_response_id");
      expect(call).not.toHaveProperty("timestamp");
    }
  });
});
