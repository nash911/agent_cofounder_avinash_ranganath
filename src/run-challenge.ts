import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { prepareOutput } from "./prepare-output.js";
import { auditAppPortAfterPi } from "./port-owner.js";
import { signalProcessTree, terminateProcessTree, usesDetachedProcessGroup } from "./process-tree.js";
import {
  composeResult,
  missingRequiredResultPaths,
  readPartialResult,
  rootStartCommand,
  writeResult,
} from "./result.js";
import { collectUsageFromJsonLines } from "./usage.js";
import type { RunResult } from "./types.js";
import { validateResultObject } from "./validate-result.js";
import { portHasListener, unavailableAppVerification, verifyGeneratedApp } from "./verify-app.js";
import { spawnSync } from "node:child_process";
import { accessSync, constants as fileConstants, mkdirSync, renameSync, rmSync, statSync } from "node:fs";

export type AgentKind = "python" | "pi";

interface Arguments {
  ideaFile: string;
  outputDirectory: string;
  prepareOnly: boolean;
  skipAppInstall: boolean;
  agent: AgentKind;
}

export interface CommandResult {
  exitCode: number;
  timedOut: boolean;
}

const SOURCE_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SOURCE_DIRECTORY, "..");
const APP_PORT = 3000;

export function runRequiresFailureExit(
  piExitCode: number,
  resultStatus: RunResult["status"],
  missingResultPaths: string[],
): boolean {
  return missingResultPaths.length > 0 || piExitCode !== 0 || resultStatus !== "success";
}

function printHelp(): void {
  console.log(`Usage: npm run challenge -- [options]

Options:
  --agent <python|pi>     Orchestrator that drives Pi (default: python, env CHALLENGE_AGENT)
  --idea-file <path>      Idea prompt file (default: contract-public/development-idea.txt)
  --output-dir <path>     Generated app directory below output/ (default: output/app)
  --prepare-only          Reset the app from the seed without invoking Pi
  --skip-app-install      Do not run npm ci in the generated app
  --help                  Show this help

Environment:
  CHALLENGE_PROVIDER      Optional Pi provider override
  CHALLENGE_MODEL         Optional Pi model override
  CHALLENGE_THINKING      Optional Pi thinking level (default: off)
  CHALLENGE_TIMEOUT_MS    Wall-clock limit for Pi (default: 900000)
  HARNESS_PYTHON          Python interpreter for the harness agent (default: python3 on PATH);
                          it must be able to run "-c 'import harness'" from the repository root
`);
}

export function parseArguments(argv: string[]): Arguments {
  const parsed: Arguments = {
    ideaFile: path.join(REPOSITORY_ROOT, "contract-public", "development-idea.txt"),
    outputDirectory: path.join("output", "app"),
    prepareOnly: false,
    skipAppInstall: false,
    agent: process.env.CHALLENGE_AGENT === "pi" ? "pi" : "python",
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help") {
      printHelp();
      process.exit(0);
    }
    if (argument === "--prepare-only") {
      parsed.prepareOnly = true;
      continue;
    }
    if (argument === "--skip-app-install") {
      parsed.skipAppInstall = true;
      continue;
    }
    if (argument === "--agent") {
      const value = argv[index + 1];
      if (!value) throw new Error(`Missing value for ${argument}`);
      if (value !== "python" && value !== "pi") throw new Error(`Unknown agent: ${value}`);
      parsed.agent = value;
      index += 1;
      continue;
    }
    if (argument === "--idea-file" || argument === "--output-dir") {
      const value = argv[index + 1];
      if (!value) throw new Error(`Missing value for ${argument}`);
      if (argument === "--idea-file") parsed.ideaFile = path.resolve(value);
      else parsed.outputDirectory = value;
      index += 1;
      continue;
    }
    throw new Error(`Unknown argument: ${argument}`);
  }
  return parsed;
}

function commandName(name: string): string {
  return process.platform === "win32" ? `${name}.cmd` : name;
}

async function runInherited(command: string, args: string[], cwd: string): Promise<number> {
  return await new Promise<number>((resolve, reject) => {
    const child = spawn(command, args, { cwd, stdio: "inherit", env: process.env, shell: false });
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? 1));
  });
}

function summarizeEventLine(line: string): void {
  try {
    const event = JSON.parse(line) as Record<string, unknown>;
    if (event.type === "tool_execution_end") {
      console.log(`[pi] completed tool: ${String(event.toolName ?? "unknown")}`);
    }
    if (event.type === "message_end") {
      const message = event.message as Record<string, unknown> | undefined;
      const usage = message?.usage as Record<string, unknown> | undefined;
      if (message?.role === "assistant" && usage) {
        console.log(
          `[pi] model call completed: input=${String(usage.input ?? 0)} output=${String(usage.output ?? 0)}`,
        );
      }
    }
  } catch {
    // The unmodified line remains in events.jsonl for independent inspection.
  }
}

export async function runPi(
  args: string[],
  cwd: string,
  eventFile: string,
  stderrFile: string,
  timeoutMs: number,
): Promise<CommandResult> {
  const events = createWriteStream(eventFile, { flags: "wx" });
  const errors = createWriteStream(stderrFile, { flags: "wx" });
  let lineBuffer = "";
  let piChild: ReturnType<typeof spawn> | undefined;

  try {
    return await new Promise<CommandResult>((resolve, reject) => {
      const piBinary = path.join(
        REPOSITORY_ROOT,
        "node_modules",
        ".bin",
        process.platform === "win32" ? "pi.cmd" : "pi",
      );
      const child = spawn(piBinary, args, {
        cwd,
        detached: usesDetachedProcessGroup(),
        env: { ...process.env, PI_OFFLINE: "1" },
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      });
      piChild = child;
      let timedOut = false;
      let killTimer: NodeJS.Timeout | undefined;
      const timeout = setTimeout(() => {
        timedOut = true;
        signalProcessTree(child, "SIGTERM");
        killTimer = setTimeout(() => signalProcessTree(child, "SIGKILL"), 5_000);
      }, timeoutMs);

      child.stdout.on("data", (chunk: Buffer) => {
        events.write(chunk);
        lineBuffer += chunk.toString("utf8");
        const lines = lineBuffer.split(/\r?\n/u);
        lineBuffer = lines.pop() ?? "";
        for (const line of lines) summarizeEventLine(line);
      });
      child.stderr.pipe(errors);
      child.stderr.pipe(process.stderr);
      child.once("error", (error) => {
        clearTimeout(timeout);
        if (killTimer) clearTimeout(killTimer);
        reject(error);
      });
      child.once("close", (code) => {
        clearTimeout(timeout);
        if (killTimer) clearTimeout(killTimer);
        if (lineBuffer !== "") summarizeEventLine(lineBuffer);
        resolve({ exitCode: timedOut ? 124 : (code ?? 1), timedOut });
      });
    });
  } finally {
    if (piChild) await terminateProcessTree(piChild);
    await Promise.all([
      new Promise<void>((resolve) => events.end(resolve)),
      new Promise<void>((resolve) => errors.end(resolve)),
    ]);
  }
}

export function buildPiArguments(
  idea: string,
  systemPrompt: string,
  publicJourneys: string,
  appContext: string,
  artifactDirectory: string,
): string[] {
  const args = [
    "--mode",
    "json",
    "--offline",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--append-system-prompt",
    `${systemPrompt.trim()}\n\n${publicJourneys.trim()}\n\n${appContext.trim()}`,
    "--session-dir",
    path.join(artifactDirectory, "sessions"),
    "--extension",
    path.join(REPOSITORY_ROOT, "solution", "extensions", "protected-paths.ts"),
    "--skill",
    path.join(REPOSITORY_ROOT, "solution", "skills", "mvp-builder"),
  ];
  if (process.env.CHALLENGE_PROVIDER) args.push("--provider", process.env.CHALLENGE_PROVIDER);
  if (process.env.CHALLENGE_MODEL) args.push("--model", process.env.CHALLENGE_MODEL);
  args.push("--thinking", process.env.CHALLENGE_THINKING ?? "off");
  args.push(`## Product idea\n\n${idea.trim()}\n`);
  return args;
}

function timeoutFromEnvironment(): number {
  const raw = process.env.CHALLENGE_TIMEOUT_MS ?? "900000";
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 1_000) {
    throw new Error("CHALLENGE_TIMEOUT_MS must be an integer of at least 1000");
  }
  return value;
}

async function main(): Promise<void> {
  const args = parseArguments(process.argv.slice(2));
  const idea = await readFile(args.ideaFile, "utf8");
  const outputDirectory = await prepareOutput(REPOSITORY_ROOT, args.outputDirectory);
  console.log(`Prepared clean application workspace: ${outputDirectory}`);

  if (!args.skipAppInstall) {
    const installCode = await runInherited(
      commandName("npm"),
      ["ci", "--ignore-scripts", "--prefer-offline"],
      outputDirectory,
    );
    if (installCode !== 0) throw new Error(`App dependency installation failed with exit code ${installCode}`);
  }
  if (args.prepareOnly) return;

  const [systemPrompt, publicJourneys, appContext] = await Promise.all([
    readFile(path.join(REPOSITORY_ROOT, "solution", "system-prompt.md"), "utf8"),
    readFile(path.join(REPOSITORY_ROOT, "contract-public", "journeys.md"), "utf8"),
    readFile(path.join(outputDirectory, "AGENTS.md"), "utf8"),
  ]);

  const runId = new Date().toISOString().replaceAll(":", "-").replaceAll(".", "-");
  const artifactDirectory = path.join(REPOSITORY_ROOT, "artifacts", "runs", runId);
  await mkdir(path.join(artifactDirectory, "sessions"), { recursive: true });
  await writeFile(path.join(artifactDirectory, "idea.txt"), idea, "utf8");

  const eventFile = path.join(artifactDirectory, "events.jsonl");
  const stderrFile = path.join(artifactDirectory, "pi.stderr.log");
  const appPortHadListenerBeforePi = await portHasListener(APP_PORT);
  const harnessInterpreter =
    args.agent === "python" ? resolveHarnessInterpreter(REPOSITORY_ROOT, process.env) : undefined;
  if (args.agent === "python" && harnessInterpreter === undefined) {
    console.error(
      `Harness interpreter unresolved; falling back to the direct Pi agent. Probed: ${harnessInterpreterProbeSummary(REPOSITORY_ROOT, process.env)}`,
    );
  }
  const pi = harnessInterpreter ? await runHarness(
    harnessInterpreter,
    buildHarnessArguments(
      args.ideaFile,
      path.join(artifactDirectory, "sessions"),
      outputDirectory,
      timeoutFromEnvironment(),
      REPOSITORY_ROOT,
      process.env,
    ),
    REPOSITORY_ROOT,
    eventFile,
    stderrFile,
    timeoutFromEnvironment(),
    harnessChildEnvironment(process.env, REPOSITORY_ROOT, process.stderr.isTTY === true && !process.env.NO_COLOR),
  ) : await runPi(
    buildPiArguments(idea, systemPrompt, publicJourneys, appContext, artifactDirectory),
    outputDirectory,
    eventFile,
    stderrFile,
    timeoutFromEnvironment(),
  );
  const portReclamation = await auditAppPortAfterPi(APP_PORT, outputDirectory, appPortHadListenerBeforePi);
  if (portReclamation.listener_after_pi) {
    const message = `${portReclamation.diagnostic}; pids=${portReclamation.process_ids.join(",") || "none"}`;
    if (portReclamation.reclaimed) console.log(message);
    else console.warn(message);
  }

  const usage = collectUsageFromJsonLines(await readFile(eventFile, "utf8"));
  const partial = await readPartialResult(outputDirectory);
  const canVerifyApp = pi.exitCode === 0 && usage.model_calls > 0;
  const startCommand = rootStartCommand(REPOSITORY_ROOT, outputDirectory);
  let verification = unavailableAppVerification(
    canVerifyApp ? "app verification had not completed" : "Pi did not complete with audited model usage",
  );
  let result = composeResult(partial, usage, pi.exitCode, verification, portReclamation, startCommand);
  const appResultPath = path.join(outputDirectory, "result.json");
  const rootResultPath = path.join(REPOSITORY_ROOT, "result.json");
  const requiredResultPaths = [appResultPath, rootResultPath];
  let resultPaths = await writeResult(
    outputDirectory,
    result,
    [rootResultPath],
  );
  if (canVerifyApp) {
    verification = await verifyGeneratedApp(outputDirectory, artifactDirectory, { displayRoot: REPOSITORY_ROOT });
    result = composeResult(partial, usage, pi.exitCode, verification, portReclamation, startCommand);
    resultPaths = await writeResult(outputDirectory, result, [rootResultPath]);
  }
  const missingResultPaths = missingRequiredResultPaths(resultPaths, requiredResultPaths);
  const validationErrors = await validateResultObject(result);
  if (validationErrors.length > 0) {
    for (const error of validationErrors) console.error(`- ${error}`);
    process.exitCode = 1;
    return;
  }

  console.log(`Result written to ${resultPaths.join(" and ")}`);
  console.log(`Audit artifacts written to ${artifactDirectory}`);
  for (const missingResultPath of missingResultPaths) {
    console.error(`Required result destination was not written: ${missingResultPath}`);
  }
  if (pi.timedOut) console.error("Pi exceeded CHALLENGE_TIMEOUT_MS and was terminated.");
  if (runRequiresFailureExit(pi.exitCode, result.status, missingResultPaths)) process.exitCode = 1;
}

/**
 * Additive harness seam. Nothing below is reachable unless `--agent python`
 * (or `CHALLENGE_AGENT=python`, the default) resolves a Python interpreter;
 * every other path keeps the starter's `runPi` behaviour byte for byte.
 */

/** Head start the Python harness must keep over the runner's own SIGTERM timer. */
export const HARNESS_SHUTDOWN_MARGIN_MS = 30_000;

function harnessInterpreterNames(): string[] {
  return process.platform === "win32" ? ["python3.exe", "python.exe"] : ["python3"];
}

function extractedHarnessInterpreter(repositoryRoot: string): string {
  return path.join(
    repositoryRoot,
    "artifacts",
    "python",
    `${process.platform}-${process.arch}`,
    "bin",
    process.platform === "win32" ? "python.exe" : "python3",
  );
}

/** python-build-standalone names its archives with the GNU architecture, not Node's. */
function standaloneArchitecture(architecture: string): string {
  if (architecture === "x64") return "x86_64";
  if (architecture === "arm64") return "aarch64";
  return architecture;
}

function vendoredHarnessArchive(repositoryRoot: string): string {
  return path.join(
    repositoryRoot,
    "vendor",
    "python",
    `cpython-${standaloneArchitecture(process.arch)}-unknown-linux-gnu-install_only.tar.gz`,
  );
}

/** Human-readable list of the locations `resolveHarnessInterpreter` probes, in order. */
export function harnessInterpreterProbeSummary(repositoryRoot: string, env: NodeJS.ProcessEnv): string {
  return [
    `HARNESS_PYTHON=${env.HARNESS_PYTHON ?? "<unset>"}`,
    `${harnessInterpreterNames().join("|")} on PATH`,
    extractedHarnessInterpreter(repositoryRoot),
    vendoredHarnessArchive(repositoryRoot),
  ].join(", ");
}

function isExecutableFile(candidate: string): boolean {
  try {
    accessSync(candidate, fileConstants.X_OK);
    return statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function isRegularFile(candidate: string): boolean {
  try {
    return statSync(candidate).isFile();
  } catch {
    return false;
  }
}

/** Milliseconds allowed for the one-shot `import harness` probe of a candidate. */
const HARNESS_PROBE_TIMEOUT_MS = 10_000;

/**
 * The executable bit alone proves nothing: a pyenv/venv shim whose target was
 * removed still passes `accessSync(X_OK)` and then makes `spawn` emit `error`,
 * which rejects `runHarness` out of an unguarded `main()` and leaves *no*
 * `result.json` at either required path. Probing with the child's own
 * environment also proves the candidate can actually import the package, so an
 * interpreter that starts but cannot see `harness/` falls back to `runPi`
 * instead of producing a failed run.
 */
function canRunHarnessModule(
  candidate: string,
  repositoryRoot: string,
  env: NodeJS.ProcessEnv,
): boolean {
  if (!isExecutableFile(candidate)) return false;
  const probe = spawnSync(candidate, ["-c", "import harness"], {
    cwd: repositoryRoot,
    env: harnessChildEnvironment(env, repositoryRoot, false),
    killSignal: "SIGKILL",
    shell: false,
    stdio: "ignore",
    timeout: HARNESS_PROBE_TIMEOUT_MS,
    windowsHide: true,
  });
  return probe.error === undefined && probe.status === 0;
}

/**
 * Resolves the interpreter that runs `-m harness`: `HARNESS_PYTHON`, then
 * `python3` on `PATH`, then a previously extracted standalone build, then a
 * one-shot extraction of the vendored `.tar.gz` (the judged image has neither
 * xz nor zstd). Every candidate must survive an `import harness` probe. Returns
 * `undefined` instead of throwing so the caller can fall back to `runPi`; it
 * must therefore run before any `wx` stream is opened.
 */
export function resolveHarnessInterpreter(
  repositoryRoot: string,
  env: NodeJS.ProcessEnv,
  isExecutable: (candidate: string) => boolean = (candidate) =>
    canRunHarnessModule(candidate, repositoryRoot, env),
): string | undefined {
  try {
    const explicit = env.HARNESS_PYTHON;
    if (explicit !== undefined && explicit !== "" && isExecutable(explicit)) return explicit;

    for (const entry of (env.PATH ?? "").split(path.delimiter)) {
      if (entry === "") continue;
      for (const name of harnessInterpreterNames()) {
        const candidate = path.join(entry, name);
        if (isExecutable(candidate)) return candidate;
      }
    }

    const extracted = extractedHarnessInterpreter(repositoryRoot);
    if (isExecutable(extracted)) return extracted;

    const archive = vendoredHarnessArchive(repositoryRoot);
    if (!isRegularFile(archive)) return undefined;

    const installRoot = path.dirname(path.dirname(extracted));
    const staging = `${installRoot}.staging-${process.pid}`;
    rmSync(staging, { force: true, recursive: true });
    mkdirSync(staging, { recursive: true });
    const extraction = spawnSync("tar", ["-xzf", archive, "--strip-components=1", "-C", staging], {
      shell: false,
    });
    if (extraction.status !== 0) {
      rmSync(staging, { force: true, recursive: true });
      return undefined;
    }
    renameSync(staging, installRoot);
    return isExecutable(extracted) ? extracted : undefined;
  } catch {
    return undefined;
  }
}

/** Pi's exact, case-sensitive set (`dist/cli/args.js`); anything else warns and falls back. */
const VALID_THINKING_LEVELS: readonly string[] = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];

/**
 * An invalid `--thinking` makes Pi print a warning, ignore the flag and use its
 * own configured default (`medium`), so an empty or misspelled organizer value
 * would silently turn thinking on for a judged run — the largest measured cost
 * lever. Anything unrecognised becomes `off`; a valid organizer choice is never
 * overridden.
 */
export function normalizeThinkingLevel(raw: string | undefined): string {
  const candidate = (raw ?? "").trim().toLowerCase();
  if (candidate === "") return "off";
  if (VALID_THINKING_LEVELS.includes(candidate)) return candidate;
  process.stderr.write(`[harness] ignoring invalid CHALLENGE_THINKING "${raw}"; using "off"\n`);
  return "off";
}

/**
 * Arguments for `<interpreter> -m harness`. The harness deadline is strictly
 * smaller than the runner's so Python can close its Pi sessions by EOF before
 * the runner escalates to SIGTERM.
 */
export function buildHarnessArguments(
  ideaFile: string,
  sessionRoot: string,
  appDirectory: string,
  timeoutMs: number,
  repositoryRoot: string,
  env: NodeJS.ProcessEnv,
): string[] {
  const args = [
    "-m",
    "harness",
    "--idea-file",
    path.resolve(ideaFile),
    "--session-root",
    path.resolve(sessionRoot),
    "--cwd",
    path.resolve(appDirectory),
    "--timeout-ms",
    String(Math.max(1_000, timeoutMs - HARNESS_SHUTDOWN_MARGIN_MS)),
    "--repo-root",
    path.resolve(repositoryRoot),
    "--thinking",
    normalizeThinkingLevel(env.CHALLENGE_THINKING),
  ];
  if (env.CHALLENGE_PROVIDER) args.push("--provider", env.CHALLENGE_PROVIDER);
  if (env.CHALLENGE_MODEL) args.push("--model", env.CHALLENGE_MODEL);
  return args;
}

/**
 * Environment for the harness child. Every organizer-supplied key survives
 * verbatim; only the Python bootstrap variables are forced. `PI_CODING_AGENT_DIR`
 * is never invented — a relative one is merely resolved against the repository
 * root, because the harness child runs with a different working directory.
 */
export function harnessChildEnvironment(
  parentEnv: NodeJS.ProcessEnv,
  repositoryRoot: string,
  colorized: boolean,
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...parentEnv };
  env.PI_OFFLINE = "1";
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONDONTWRITEBYTECODE = "1";
  env.PYTHONNOUSERSITE = "1";
  const inheritedPythonPath = parentEnv.PYTHONPATH;
  env.PYTHONPATH =
    inheritedPythonPath === undefined || inheritedPythonPath === ""
      ? repositoryRoot
      : [repositoryRoot, inheritedPythonPath].join(path.delimiter);
  if (colorized) env.HARNESS_COLOR = "1";
  else delete env.HARNESS_COLOR;
  const agentDirectory = parentEnv.PI_CODING_AGENT_DIR;
  if (agentDirectory !== undefined && agentDirectory !== "" && !path.isAbsolute(agentDirectory)) {
    env.PI_CODING_AGENT_DIR = path.resolve(repositoryRoot, agentDirectory);
  }
  delete env.PYTHONSAFEPATH;
  delete env.PYTHONHOME;
  delete env.PYTHONSTARTUP;
  return env;
}

/**
 * `runPi` for the harness interpreter: same event/stderr files, same `wx`
 * semantics, same process-group timeout escalation. Interpreter, arguments and
 * environment are parameters so every behaviour here is testable without Python.
 */
export async function runHarness(
  interpreter: string,
  args: string[],
  cwd: string,
  eventFile: string,
  stderrFile: string,
  timeoutMs: number,
  env: NodeJS.ProcessEnv,
): Promise<CommandResult> {
  const events = createWriteStream(eventFile, { flags: "wx" });
  const errors = createWriteStream(stderrFile, { flags: "wx" });
  let lineBuffer = "";
  let harnessChild: ReturnType<typeof spawn> | undefined;

  try {
    return await new Promise<CommandResult>((resolve, reject) => {
      const child = spawn(interpreter, args, {
        cwd,
        detached: usesDetachedProcessGroup(),
        env,
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      });
      harnessChild = child;
      let timedOut = false;
      let killTimer: NodeJS.Timeout | undefined;
      const timeout = setTimeout(() => {
        timedOut = true;
        signalProcessTree(child, "SIGTERM");
        killTimer = setTimeout(() => signalProcessTree(child, "SIGKILL"), 5_000);
      }, timeoutMs);

      child.stdout.on("data", (chunk: Buffer) => {
        events.write(chunk);
        lineBuffer += chunk.toString("utf8");
        const lines = lineBuffer.split(/\r?\n/u);
        lineBuffer = lines.pop() ?? "";
        for (const line of lines) summarizeEventLine(line);
      });
      child.stderr.pipe(errors);
      child.stderr.pipe(process.stderr);
      child.once("error", (error) => {
        clearTimeout(timeout);
        if (killTimer) clearTimeout(killTimer);
        reject(error);
      });
      child.once("close", (code) => {
        clearTimeout(timeout);
        if (killTimer) clearTimeout(killTimer);
        if (lineBuffer !== "") summarizeEventLine(lineBuffer);
        resolve({ exitCode: timedOut ? 124 : (code ?? 1), timedOut });
      });
    });
  } finally {
    if (harnessChild) await terminateProcessTree(harnessChild);
    await Promise.all([
      new Promise<void>((resolve) => events.end(resolve)),
      new Promise<void>((resolve) => errors.end(resolve)),
    ]);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
