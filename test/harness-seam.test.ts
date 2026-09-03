import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { usesDetachedProcessGroup } from "../src/process-tree.js";
import {
  HARNESS_SHUTDOWN_MARGIN_MS,
  annotateTelemetrySources,
  buildHarnessArguments,
  harnessChildEnvironment,
  parseArguments,
  resolveHarnessInterpreter,
  runHarness,
} from "../src/run-challenge.js";
import type { RunResult } from "../src/types.js";
import { collectUsageFromJsonLines } from "../src/usage.js";
import { validateResultObject } from "../src/validate-result.js";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true })));
});

async function temporaryDirectory(): Promise<string> {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-cofounder-harness-seam-"));
  temporaryDirectories.push(directory);
  return directory;
}

function withEnvironment<T>(overrides: Record<string, string | undefined>, action: () => T): T {
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(overrides)) {
    previous.set(key, process.env[key]);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  try {
    return action();
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

function valueAfter(args: string[], flag: string): string | undefined {
  const index = args.indexOf(flag);
  return index === -1 ? undefined : args[index + 1];
}

function assistantEvent(model: string, input: number, output: number, cacheRead: number): string {
  return JSON.stringify({
    type: "message_end",
    message: {
      role: "assistant",
      model,
      usage: {
        input,
        output,
        cacheRead,
        cacheWrite: 0,
        totalTokens: input + output + cacheRead,
      },
    },
  });
}

/** Executable probe that answers for an explicit allow list only. */
function probeFor(executables: string[]): (candidate: string) => boolean {
  return (candidate) => executables.includes(candidate);
}

/**
 * Runs `action` with the process-level `uncaughtException` listeners replaced so
 * a stream error with no listener — the `wx` EEXIST path both `runPi` and
 * `runHarness` deliberately share — can be observed instead of failing the file.
 */
async function captureUncaughtExceptions(action: () => Promise<void>): Promise<NodeJS.ErrnoException[]> {
  const existing = process.listeners("uncaughtException");
  const captured: NodeJS.ErrnoException[] = [];
  const handler = (error: Error): void => {
    captured.push(error);
  };
  process.removeAllListeners("uncaughtException");
  process.on("uncaughtException", handler);
  try {
    await action();
    const deadline = Date.now() + 3_000;
    while (captured.length === 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  } finally {
    process.off("uncaughtException", handler);
    for (const listener of existing) process.on("uncaughtException", listener);
  }
  return captured;
}

describe("harness agent selection", () => {
  it("defaults to the python harness and reads CHALLENGE_AGENT leniently", () => {
    withEnvironment({ CHALLENGE_AGENT: undefined }, () => {
      expect(parseArguments([]).agent).toBe("python");
    });
    withEnvironment({ CHALLENGE_AGENT: "pi" }, () => {
      expect(parseArguments([]).agent).toBe("pi");
    });
    withEnvironment({ CHALLENGE_AGENT: "" }, () => {
      expect(parseArguments([]).agent).toBe("python");
    });
    withEnvironment({ CHALLENGE_AGENT: "orchestrator" }, () => {
      expect(() => parseArguments([])).not.toThrow();
      expect(parseArguments([]).agent).toBe("python");
    });
  });

  it("lets the explicit flag override the organizer environment and rejects a bad value", () => {
    withEnvironment({ CHALLENGE_AGENT: "python" }, () => {
      expect(parseArguments(["--agent", "pi"]).agent).toBe("pi");
    });
    withEnvironment({ CHALLENGE_AGENT: "pi" }, () => {
      expect(parseArguments(["--agent", "python"]).agent).toBe("python");
    });
    expect(() => parseArguments(["--agent", "node"])).toThrow(/Unknown agent/u);
    expect(() => parseArguments(["--agent"])).toThrow(/Missing value/u);
  });

  it("keeps the starter's own flags working alongside the new one", () => {
    const parsed = parseArguments(["--agent", "pi", "--idea-file", "organizer/idea.txt", "--prepare-only"]);
    expect(parsed.agent).toBe("pi");
    expect(parsed.ideaFile).toBe(path.resolve("organizer/idea.txt"));
    expect(parsed.prepareOnly).toBe(true);
  });
});

describe("harness invocation", () => {
  it("hands the harness absolute paths and a deadline inside the runner's own", () => {
    const args = buildHarnessArguments(
      "/repo/contract-public/development-idea.txt",
      "/repo/artifacts/runs/run-1/sessions",
      "/repo/output/app",
      900_000,
      "/repo",
      {},
    );

    expect(args[0]).toBe("-m");
    expect(args[1]).toBe("harness");
    expect(valueAfter(args, "--idea-file")).toBe(path.resolve("/repo/contract-public/development-idea.txt"));
    expect(valueAfter(args, "--session-root")).toBe(path.resolve("/repo/artifacts/runs/run-1/sessions"));
    expect(valueAfter(args, "--cwd")).toBe(path.resolve("/repo/output/app"));
    expect(valueAfter(args, "--repo-root")).toBe(path.resolve("/repo"));
    expect(valueAfter(args, "--thinking")).toBe("off");
    expect(args).not.toContain("--provider");
    expect(args).not.toContain("--model");

    const harnessTimeout = Number(valueAfter(args, "--timeout-ms"));
    expect(harnessTimeout).toBe(900_000 - HARNESS_SHUTDOWN_MARGIN_MS);
    expect(harnessTimeout).toBeLessThan(900_000);
  });

  it("never hands the harness a non-positive deadline", () => {
    const args = buildHarnessArguments("idea.txt", "sessions", "app", 5_000, "/repo", {});
    expect(valueAfter(args, "--timeout-ms")).toBe("1000");
  });

  it("forwards organizer provider, model and thinking overrides", () => {
    const args = buildHarnessArguments("idea.txt", "sessions", "app", 900_000, "/repo", {
      CHALLENGE_MODEL: "glm-4.6",
      CHALLENGE_PROVIDER: "berget",
      CHALLENGE_THINKING: "low",
    });
    expect(valueAfter(args, "--provider")).toBe("berget");
    expect(valueAfter(args, "--model")).toBe("glm-4.6");
    expect(valueAfter(args, "--thinking")).toBe("low");
  });
});

describe("harness child environment", () => {
  it("forces the python bootstrap without touching organizer keys", () => {
    const parent: NodeJS.ProcessEnv = {
      CHALLENGE_MODEL: "glm-4.6",
      CHALLENGE_PROVIDER: "berget",
      CHALLENGE_TIMEOUT_MS: "900000",
      PATH: "/usr/bin",
      PYTHONHOME: "/opt/broken",
      PYTHONPATH: "/inherited",
      PYTHONSAFEPATH: "1",
      PYTHONSTARTUP: "/opt/startup.py",
    };
    const env = harnessChildEnvironment(parent, "/repo", false);

    expect(env.PI_OFFLINE).toBe("1");
    expect(env.PYTHONUNBUFFERED).toBe("1");
    expect(env.PYTHONDONTWRITEBYTECODE).toBe("1");
    expect(env.PYTHONNOUSERSITE).toBe("1");
    expect(env.PYTHONPATH).toBe(["/repo", "/inherited"].join(path.delimiter));
    expect(env.PYTHONSAFEPATH).toBeUndefined();
    expect(env.PYTHONHOME).toBeUndefined();
    expect(env.PYTHONSTARTUP).toBeUndefined();
    expect(env.HARNESS_COLOR).toBeUndefined();
    expect(env.CHALLENGE_PROVIDER).toBe("berget");
    expect(env.CHALLENGE_MODEL).toBe("glm-4.6");
    expect(env.CHALLENGE_TIMEOUT_MS).toBe("900000");
    expect(env.PATH).toBe("/usr/bin");
    expect(parent.PYTHONSAFEPATH).toBe("1");
  });

  it("puts the repository first on an absent PYTHONPATH and colours only on request", () => {
    expect(harnessChildEnvironment({}, "/repo", false).PYTHONPATH).toBe("/repo");
    expect(harnessChildEnvironment({}, "/repo", true).HARNESS_COLOR).toBe("1");
    expect(harnessChildEnvironment({ HARNESS_COLOR: "1" }, "/repo", false).HARNESS_COLOR).toBeUndefined();
  });

  it("passes an organizer PI_CODING_AGENT_DIR through and never invents one", () => {
    const absolute = harnessChildEnvironment({ PI_CODING_AGENT_DIR: "/organizer/.pi" }, "/repo", false);
    expect(absolute.PI_CODING_AGENT_DIR).toBe("/organizer/.pi");

    const relative = harnessChildEnvironment({ PI_CODING_AGENT_DIR: ".pi-agent" }, "/repo", false);
    expect(relative.PI_CODING_AGENT_DIR).toBe(path.resolve("/repo", ".pi-agent"));

    const absent = harnessChildEnvironment({ CHALLENGE_PROVIDER: "berget" }, "/repo", false);
    expect("PI_CODING_AGENT_DIR" in absent).toBe(false);
    expect(absent.PI_CODING_AGENT_DIR).toBeUndefined();
  });
});

describe("harness interpreter resolution", () => {
  const pathDirectory = path.join(path.sep, "usr", "bin");
  const interpreterOnPath = path.join(pathDirectory, process.platform === "win32" ? "python3.exe" : "python3");
  const explicitInterpreter = path.join(path.sep, "opt", "python", "bin", "python3");

  it("prefers HARNESS_PYTHON over PATH", () => {
    const resolved = resolveHarnessInterpreter(
      "/repo",
      { HARNESS_PYTHON: explicitInterpreter, PATH: pathDirectory },
      probeFor([explicitInterpreter, interpreterOnPath]),
    );
    expect(resolved).toBe(explicitInterpreter);
  });

  it("falls back to python3 on PATH, skipping empty and non-matching entries", () => {
    const searchPath = ["", path.join(path.sep, "nowhere"), pathDirectory].join(path.delimiter);
    expect(resolveHarnessInterpreter("/repo", { PATH: searchPath }, probeFor([interpreterOnPath]))).toBe(
      interpreterOnPath,
    );
    expect(
      resolveHarnessInterpreter(
        "/repo",
        { HARNESS_PYTHON: explicitInterpreter, PATH: searchPath },
        probeFor([interpreterOnPath]),
      ),
    ).toBe(interpreterOnPath);
  });

  it("returns undefined without throwing when nothing resolves", () => {
    expect(resolveHarnessInterpreter("/repo", {}, probeFor([]))).toBeUndefined();
    expect(resolveHarnessInterpreter("/repo", { PATH: pathDirectory }, probeFor([]))).toBeUndefined();
    expect(() =>
      resolveHarnessInterpreter("/repo", { HARNESS_PYTHON: explicitInterpreter, PATH: pathDirectory }, () => {
        throw new Error("probe exploded");
      }),
    ).not.toThrow();
    expect(
      resolveHarnessInterpreter("/repo", { HARNESS_PYTHON: explicitInterpreter }, () => {
        throw new Error("probe exploded");
      }),
    ).toBeUndefined();
  });
});

describe("usage over concatenated harness event streams", () => {
  it("sums two session-headed streams and keeps the call log contiguous", () => {
    const firstSession = [
      JSON.stringify({ type: "session", id: "session-1", model: "berget/glm-4.6" }),
      assistantEvent("glm-4.6", 100, 10, 1_000),
      assistantEvent("glm-4.6", 200, 20, 2_000),
    ];
    const secondSession = [
      JSON.stringify({ type: "session", id: "session-2", model: "berget/glm-4.6" }),
      assistantEvent("glm-4.6", 400, 40, 4_000),
      assistantEvent("glm-4.6", 800, 80, 8_000),
      assistantEvent("glm-4.6", 1_600, 160, 16_000),
    ];
    const usage = collectUsageFromJsonLines([...firstSession, ...secondSession].join("\n"));

    expect(usage.model_calls).toBe(5);
    expect(usage.input_tokens).toBe(3_100);
    expect(usage.output_tokens).toBe(310);
    expect(usage.cache_read_tokens).toBe(31_000);
    expect(usage.call_log.map((call) => call.index)).toEqual([1, 2, 3, 4, 5]);
  });

  it("counts an RPC-shaped stream that carries no session header at all", () => {
    const lines = [
      JSON.stringify({ id: "req-1", type: "response", command: "prompt", success: true }),
      assistantEvent("glm-4.6", 100, 10, 1_000),
      JSON.stringify({ type: "extension_ui_request", id: "ui-1", extension: "protected-paths" }),
      assistantEvent("glm-4.6", 200, 20, 2_000),
      JSON.stringify({
        type: "compaction_end",
        result: { usage: { input: 50, output: 5, cacheRead: 500, cacheWrite: 0, totalTokens: 555 } },
      }),
      assistantEvent("glm-4.6", 400, 40, 4_000),
      JSON.stringify({ type: "agent_settled" }),
    ];
    const assistantCount = 3;
    const usage = collectUsageFromJsonLines(lines.join("\n"));

    expect(usage.model_calls).toBe(assistantCount + 1);
    expect(usage.call_log.map((call) => call.model)).toEqual([
      "glm-4.6",
      "glm-4.6",
      "pi-compaction",
      "glm-4.6",
    ]);
    expect(usage.input_tokens).toBe(750);
    expect(usage.output_tokens).toBe(75);
    expect(usage.cache_read_tokens).toBe(7_500);
  });
});

describe("runHarness process behaviour", () => {
  const processGroupTest = usesDetachedProcessGroup() ? it : it.skip;

  it("forwards the child's stdout byte for byte and captures its stderr", async () => {
    const directory = await temporaryDirectory();
    const payloadPath = path.join(directory, "payload.jsonl");
    const eventFile = path.join(directory, "events.jsonl");
    const stderrFile = path.join(directory, "harness.stderr.log");
    const payload = `${[
      JSON.stringify({ id: "req-1", type: "response", command: "prompt", success: true }),
      "{ this line is not json at all",
      JSON.stringify({
        type: "message_end",
        message: {
          role: "assistant",
          model: "glm-4.6",
          usage: { input: 100, output: 10, cacheRead: 1_000, cacheWrite: 0, totalTokens: 1_110 },
          filler: "x".repeat(120_000),
        },
      }),
      JSON.stringify({ type: "agent_settled" }),
    ].join("\n")}\n`;
    await writeFile(payloadPath, payload, "utf8");

    const result = await runHarness(
      process.execPath,
      [
        "-e",
        'process.stdout.write(require("node:fs").readFileSync(process.argv[1])); process.stderr.write("harness stderr line\\n");',
        payloadPath,
      ],
      directory,
      eventFile,
      stderrFile,
      20_000,
      harnessChildEnvironment(process.env, REPOSITORY_ROOT, false),
    );

    expect(result).toEqual({ exitCode: 0, timedOut: false });
    expect(await readFile(eventFile)).toEqual(Buffer.from(payload, "utf8"));
    expect(await readFile(stderrFile, "utf8")).toContain("harness stderr line");

    const usage = collectUsageFromJsonLines(await readFile(eventFile, "utf8"));
    expect(usage.model_calls).toBe(1);
    expect(usage.output_tokens).toBe(10);
  });

  it("terminates a child that outlives the deadline and reports exit code 124", async () => {
    const directory = await temporaryDirectory();
    const startedAt = Date.now();
    const result = await runHarness(
      process.execPath,
      ["-e", "setTimeout(() => {}, 30_000)"],
      directory,
      path.join(directory, "events.jsonl"),
      path.join(directory, "harness.stderr.log"),
      1_000,
      harnessChildEnvironment(process.env, REPOSITORY_ROOT, false),
    );

    expect(result).toEqual({ exitCode: 124, timedOut: true });
    expect(Date.now() - startedAt).toBeLessThan(8_000);
  }, 20_000);

  processGroupTest("kills the grandchildren the harness spawned, not only the harness", async () => {
    const directory = await temporaryDirectory();
    const eventFile = path.join(directory, "events.jsonl");
    const result = await runHarness(
      process.execPath,
      [
        "-e",
        'const { spawn } = require("node:child_process"); const grandchild = spawn(process.execPath, ["-e", "setTimeout(() => {}, 30000)"], { stdio: "ignore" }); process.stdout.write(JSON.stringify({ type: "grandchild", pid: grandchild.pid }) + "\\n"); setTimeout(() => {}, 30000);',
      ],
      directory,
      eventFile,
      path.join(directory, "harness.stderr.log"),
      1_500,
      harnessChildEnvironment(process.env, REPOSITORY_ROOT, false),
    );

    expect(result).toEqual({ exitCode: 124, timedOut: true });
    const announced = (await readFile(eventFile, "utf8"))
      .split("\n")
      .filter((line) => line.trim() !== "")
      .map((line) => JSON.parse(line) as { type?: string; pid?: number });
    const grandchildPid = announced.find((event) => event.type === "grandchild")?.pid;
    expect(typeof grandchildPid).toBe("number");

    const deadline = Date.now() + 5_000;
    let reaped = false;
    while (!reaped && Date.now() < deadline) {
      try {
        process.kill(grandchildPid as number, 0);
        await new Promise((resolve) => setTimeout(resolve, 50));
      } catch (error) {
        reaped = (error as NodeJS.ErrnoException).code === "ESRCH";
      }
    }
    expect(reaped).toBe(true);
  }, 20_000);

  it("refuses to append to an event file a previous run already wrote", async () => {
    const directory = await temporaryDirectory();
    const eventFile = path.join(directory, "events.jsonl");
    const firstLine = `${JSON.stringify({ type: "agent_settled" })}\n`;
    const script = 'process.stdout.write(process.argv[1]);';

    const first = await runHarness(
      process.execPath,
      ["-e", script, firstLine],
      directory,
      eventFile,
      path.join(directory, "first.stderr.log"),
      20_000,
      harnessChildEnvironment(process.env, REPOSITORY_ROOT, false),
    );
    expect(first).toEqual({ exitCode: 0, timedOut: false });

    const captured = await captureUncaughtExceptions(async () => {
      await runHarness(
        process.execPath,
        ["-e", script, `${JSON.stringify({ type: "second_run" })}\n`],
        directory,
        eventFile,
        path.join(directory, "second.stderr.log"),
        20_000,
        harnessChildEnvironment(process.env, REPOSITORY_ROOT, false),
      );
    });

    expect(captured.map((error) => error.code)).toContain("EEXIST");
    expect(await readFile(eventFile, "utf8")).toBe(firstLine);
  }, 20_000);
});

describe("annotateTelemetrySources (contract C8)", () => {
  const baseResult: RunResult = {
    status: "partial",
    app_url: "http://localhost:3000",
    start_command: "npm run dev",
    summary: "A bookshelf tracker.",
    implemented_features: ["Add a book"],
    assumptions: [],
    tests_run: [{ command: "npm test", journey: "Add a book", result: "passed" }],
    harness_checks: [],
    model_calls: 1,
    input_tokens: 10,
    output_tokens: 5,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    total_tokens: 15,
    reasoning_tokens: 0,
    cost_total: 0,
    call_log: [
      {
        index: 1,
        model: "berget/zai-org/GLM-5.2",
        input_tokens: 10,
        output_tokens: 5,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        total_tokens: 15,
      },
    ],
    pi_exit_code: 0,
    telemetry_source: "pi-json-event-stream",
    port_reclamation: {
      preexisting_listener: false,
      listener_after_pi: false,
      attempted: false,
      reclaimed: false,
      process_ids: [],
      diagnostic: "no listener before or after Pi",
    },
  };

  it("adds telemetry_sources and direct_call_count without touching the schema's telemetry_source const", () => {
    const annotated = annotateTelemetrySources(baseResult, 4);
    expect(annotated.telemetry_source).toBe("pi-json-event-stream");
    expect(annotated.telemetry_sources).toEqual(["pi-json-event-stream", "direct-gateway"]);
    expect(annotated.direct_call_count).toBe(4);
    expect(annotated).not.toBe(baseResult);
    expect(baseResult).not.toHaveProperty("telemetry_sources");
  });

  it("still validates against contract-public/result.schema.json, with any direct call count", async () => {
    for (const directCallCount of [0, 4]) {
      const annotated = annotateTelemetrySources(baseResult, directCallCount);
      const errors = await validateResultObject(annotated);
      expect(errors).toEqual([]);
    }
  });
});
