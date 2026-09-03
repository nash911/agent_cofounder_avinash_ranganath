import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  buildHarnessArguments,
  normalizeThinkingLevel,
  resolveHarnessInterpreter,
} from "../src/run-challenge.js";
import {
  decideThinkingGuard,
  guardPayload,
  isGuardedApi,
  type ThinkingGuardContext,
} from "../solution/extensions/thinking-guard.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })),
  );
});

async function temporaryDirectory(): Promise<string> {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-cofounder-harness-fixes-"));
  temporaryDirectories.push(directory);
  return directory;
}

function valueAfter(args: string[], flag: string): string | undefined {
  const index = args.indexOf(flag);
  return index === -1 ? undefined : args[index + 1];
}

describe("thinking level coercion", () => {
  it("keeps every level Pi actually accepts", () => {
    for (const level of ["off", "minimal", "low", "medium", "high", "xhigh", "max"]) {
      expect(normalizeThinkingLevel(level)).toBe(level);
    }
  });

  it("normalises case and surrounding whitespace", () => {
    expect(normalizeThinkingLevel("OFF")).toBe("off");
    expect(normalizeThinkingLevel("  High  ")).toBe("high");
  });

  it("coerces an empty or misspelled organizer value to off rather than Pi's default", () => {
    for (const raw of [undefined, "", "   ", "none", "disabled", "true"]) {
      expect(normalizeThinkingLevel(raw)).toBe("off");
    }
  });

  it("never hands the harness a --thinking value Pi would ignore", () => {
    expect(valueAfter(buildHarnessArguments("i", "s", "a", 900_000, "/repo", {}), "--thinking")).toBe("off");
    expect(
      valueAfter(
        buildHarnessArguments("i", "s", "a", 900_000, "/repo", { CHALLENGE_THINKING: "" }),
        "--thinking",
      ),
    ).toBe("off");
    expect(
      valueAfter(
        buildHarnessArguments("i", "s", "a", 900_000, "/repo", { CHALLENGE_THINKING: "none" }),
        "--thinking",
      ),
    ).toBe("off");
    expect(
      valueAfter(
        buildHarnessArguments("i", "s", "a", 900_000, "/repo", { CHALLENGE_THINKING: "low" }),
        "--thinking",
      ),
    ).toBe("low");
  });
});

describe("harness interpreter probe", () => {
  it("rejects an executable that cannot actually be exec'd", async () => {
    const directory = await temporaryDirectory();
    const shim = path.join(directory, "python3");
    await writeFile(shim, "#!/nonexistent/dir/python3.11\n", "utf8");
    await chmod(shim, 0o755);

    // The default predicate has to run the candidate, not just stat it: a
    // dangling shim passes accessSync(X_OK) and then makes spawn emit `error`.
    expect(resolveHarnessInterpreter(directory, { HARNESS_PYTHON: shim, PATH: "" })).toBeUndefined();
  }, 30_000);

  it("rejects an interpreter that cannot import the harness package", async () => {
    const directory = await temporaryDirectory();
    const shim = path.join(directory, "python3");
    await writeFile(shim, "#!/bin/sh\nexit 1\n", "utf8");
    await chmod(shim, 0o755);

    expect(resolveHarnessInterpreter(directory, { HARNESS_PYTHON: shim, PATH: "" })).toBeUndefined();
  }, 30_000);
});

describe("thinking guard API gate", () => {
  function context(api: string | undefined): ThinkingGuardContext {
    return { model: { baseUrl: "https://api.berget.ai/v1", api }, thinkingLevel: "off" };
  }

  it("only treats OpenAI chat completions (or an unknown API) as guardable", () => {
    expect(isGuardedApi("openai-completions")).toBe(true);
    expect(isGuardedApi(undefined)).toBe(true);
    for (const api of [
      "anthropic-messages",
      "google-generative-ai",
      "openai-responses",
      "bedrock-converse-stream",
      "pi-messages",
    ]) {
      expect(isGuardedApi(api)).toBe(false);
    }
  });

  it("never injects chat_template_kwargs into a non chat-completions payload", () => {
    for (const api of ["anthropic-messages", "google-generative-ai", "openai-responses"]) {
      expect(guardPayload({ model: "m", messages: [] }, context(api), {})).toBeUndefined();
      expect(decideThinkingGuard({ model: "m" }, context(api), {}).reason).toBe(`api-is-${api}`);
    }
  });

  it("still fires for an OpenAI-compatible deployment", () => {
    const guarded = guardPayload({ model: "m" }, context("openai-completions"), {});
    expect(guarded?.chat_template_kwargs).toEqual({ enable_thinking: false });
    expect(decideThinkingGuard({}, context("openai-completions"), {}).reason).toBe("applied");
  });
});
