import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  applyDecision,
  decideThinkingGuard,
  guardPayload,
  isFirstPartyHost,
  resolveThinkingLevel,
  wireEnableThinking,
  type ThinkingGuardContext,
} from "../solution/extensions/thinking-guard.js";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PI_BINARY = path.join(REPOSITORY_ROOT, "node_modules", ".bin", "pi");
const EXTENSION_PATH = path.join(REPOSITORY_ROOT, "solution", "extensions", "thinking-guard.ts");

const BERGET_CONTEXT: ThinkingGuardContext = {
  model: { baseUrl: "https://api.berget.ai/v1" },
};

function contextWith(thinkingLevel: string | undefined, baseUrl = "https://api.berget.ai/v1"): ThinkingGuardContext {
  return { model: { baseUrl }, thinkingLevel };
}

interface PiRun {
  readonly exitCode: number | null;
  readonly stdout: string;
  readonly stderr: string;
  readonly timedOut: boolean;
}

async function runPiListModels(extensionPath: string, timeoutMs: number): Promise<PiRun> {
  return await new Promise<PiRun>((resolve) => {
    const child = spawn(
      PI_BINARY,
      [
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--extension",
        extensionPath,
        "--list-models",
      ],
      { cwd: REPOSITORY_ROOT, stdio: ["ignore", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ exitCode: code, stdout, stderr, timedOut });
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve({ exitCode: null, stdout, stderr, timedOut });
    });
  });
}

describe("guardPayload", () => {
  it("disables thinking when the session thinking level is off", () => {
    const result = guardPayload({ model: "zai-org/GLM-5.2" }, contextWith("off"), {});
    expect(result).toEqual({
      model: "zai-org/GLM-5.2",
      chat_template_kwargs: { enable_thinking: false },
    });
  });

  it("enables thinking when the session thinking level is high", () => {
    const result = guardPayload({ model: "zai-org/GLM-5.2" }, contextWith("high"), {});
    expect(result?.chat_template_kwargs).toEqual({ enable_thinking: true });
  });

  it("never overwrites a payload that already decided about thinking", () => {
    const alreadyDecided: Record<string, unknown>[] = [
      { chat_template_kwargs: { enable_thinking: true } },
      { thinking: { type: "disabled" } },
      { enable_thinking: false },
      { reasoning: { effort: "high" } },
      { reasoning_effort: "low" },
    ];
    for (const payload of alreadyDecided) {
      expect(guardPayload(payload, contextWith("off"), {})).toBeUndefined();
    }
  });

  it("stands down for first-party provider hosts", () => {
    expect(guardPayload({}, contextWith("off", "https://api.z.ai/api/paas/v4"), {})).toBeUndefined();
    expect(guardPayload({}, contextWith("off", "https://open.bigmodel.cn/api/paas/v4"), {})).toBeUndefined();
    expect(guardPayload({}, contextWith("off", "https://api.openai.com/v1"), {})).toBeUndefined();
    expect(guardPayload({}, contextWith("off", "https://openrouter.ai/api/v1"), {})).toBeUndefined();
  });

  it("stands down when HARNESS_THINKING_GUARD is 0", () => {
    expect(guardPayload({}, contextWith("off"), { HARNESS_THINKING_GUARD: "0" })).toBeUndefined();
    expect(guardPayload({}, contextWith("high"), { HARNESS_THINKING_GUARD: "0" })).toBeUndefined();
  });

  it("defaults to disabled thinking when neither the context nor CHALLENGE_THINKING says otherwise", () => {
    const result = guardPayload({}, contextWith(undefined), {});
    expect(result?.chat_template_kwargs).toEqual({ enable_thinking: false });
  });

  it("falls back to CHALLENGE_THINKING when the context carries no thinking level", () => {
    const result = guardPayload({}, contextWith(undefined), { CHALLENGE_THINKING: "high" });
    expect(result?.chat_template_kwargs).toEqual({ enable_thinking: true });
  });

  it("treats an empty CHALLENGE_THINKING as off rather than as thinking on", () => {
    const result = guardPayload({}, contextWith(undefined), { CHALLENGE_THINKING: "" });
    expect(result?.chat_template_kwargs).toEqual({ enable_thinking: false });
  });

  it("does not mutate the payload in place", () => {
    const payload = { model: "zai-org/GLM-5.2", messages: [{ role: "user", content: "hi" }] };
    const snapshot = JSON.stringify(payload);
    const result = guardPayload(payload, contextWith("off"), {});
    expect(JSON.stringify(payload)).toBe(snapshot);
    expect(result).not.toBe(payload);
    expect(payload).not.toHaveProperty("chat_template_kwargs");
  });

  it("ignores a payload that is not a plain object", () => {
    expect(guardPayload("not-a-payload", contextWith("off"), {})).toBeUndefined();
    expect(guardPayload([1, 2, 3], contextWith("off"), {})).toBeUndefined();
    expect(guardPayload(null, contextWith("off"), {})).toBeUndefined();
  });

  it("still fires when the model is unknown", () => {
    const result = guardPayload({}, {}, {});
    expect(result?.chat_template_kwargs).toEqual({ enable_thinking: false });
  });
});

describe("wireEnableThinking", () => {
  it("reads the provider-set value when the guard stands down", () => {
    const payload = { model: "m", chat_template_kwargs: { enable_thinking: false } };
    expect(decideThinkingGuard(payload, contextWith("off"), {}).fired).toBe(false);
    expect(wireEnableThinking(payload)).toBe(false);
  });

  it("reads the guard-applied value when the guard fires", () => {
    const payload = { model: "m" };
    const decision = decideThinkingGuard(payload, contextWith("off"), {});
    expect(decision.fired).toBe(true);
    expect(wireEnableThinking(applyDecision(payload, decision))).toBe(false);
    expect(payload).toEqual({ model: "m" });
  });

  it("returns null when nothing on the wire decides about thinking", () => {
    expect(wireEnableThinking({ model: "m" })).toBeNull();
    expect(wireEnableThinking({ model: "m", chat_template_kwargs: { enable_thinking: "yes" } })).toBeNull();
    expect(wireEnableThinking("not an object")).toBeNull();
  });
});

describe("decideThinkingGuard", () => {
  it("reports why it stood down", () => {
    expect(decideThinkingGuard({}, BERGET_CONTEXT, { HARNESS_THINKING_GUARD: "0" }).reason).toBe(
      "disabled-by-env",
    );
    expect(decideThinkingGuard({}, contextWith("off", "https://api.z.ai/v1"), {}).reason).toBe(
      "first-party-host",
    );
    expect(decideThinkingGuard({ thinking: {} }, BERGET_CONTEXT, {}).reason).toBe("payload-has-thinking");
    expect(decideThinkingGuard({}, BERGET_CONTEXT, {}).reason).toBe("applied");
  });
});

describe("resolveThinkingLevel", () => {
  it("prefers the live context level over the environment", () => {
    expect(resolveThinkingLevel({ thinkingLevel: "off" }, { CHALLENGE_THINKING: "high" })).toBe("off");
    expect(resolveThinkingLevel({}, { CHALLENGE_THINKING: "high" })).toBe("high");
    expect(resolveThinkingLevel({}, {})).toBe("off");
  });
});

describe("isFirstPartyHost", () => {
  it("matches known vendor endpoints and their subdomains only", () => {
    expect(isFirstPartyHost("https://api.z.ai/api/paas/v4")).toBe(true);
    expect(isFirstPartyHost("https://eu.api.moonshot.ai/v1")).toBe(true);
    expect(isFirstPartyHost("https://api.berget.ai/v1")).toBe(false);
    expect(isFirstPartyHost("https://localhost:8000/v1")).toBe(false);
    expect(isFirstPartyHost(undefined)).toBe(false);
  });
});

describe("thinking-guard extension loading", () => {
  it("loads into pi without an extension error and without calling a model", async () => {
    const run = await runPiListModels(EXTENSION_PATH, 20_000);
    expect(run.timedOut).toBe(false);
    const combined = `${run.stdout}\n${run.stderr}`.toLowerCase();
    expect(combined).not.toContain("failed to load extension");
    expect(combined).not.toContain("extension error");
    expect(run.stderr.toLowerCase()).not.toContain("thinking-guard.ts");
    expect(run.exitCode).toBe(0);
  }, 30_000);
});
