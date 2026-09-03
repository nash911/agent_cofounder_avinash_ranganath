/**
 * Runtime preflight for the harness seam: `npm run check:runtime`.
 *
 * Verifies everything the judged run depends on before a single token is spent —
 * the Node version, the pinned Pi version, a usable Python interpreter, writable
 * artifact and output directories, and (when the organizer environment names a
 * model) that the model id actually resolves.
 *
 * Prints one `OK` / `FAIL` / `SKIP` line per check and exits non-zero on any FAIL.
 * Never prints an environment variable value other than CHALLENGE_PROVIDER,
 * CHALLENGE_MODEL, CHALLENGE_THINKING and CHALLENGE_TIMEOUT_MS.
 */
import { spawnSync } from "node:child_process";
import {
  accessSync,
  constants,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SOURCE_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SOURCE_DIRECTORY, "..", "..");
const REQUIRED_PI_VERSION = "0.84.1";
const LIST_MODELS_TIMEOUT_MS = 20_000;

type CheckStatus = "OK" | "FAIL" | "SKIP";

interface CheckResult {
  status: CheckStatus;
  name: string;
  detail: string;
}

interface InterpreterResolution {
  source: string;
  interpreter?: string;
  archive?: string;
}

function isExecutable(candidate: string): boolean {
  try {
    accessSync(candidate, constants.X_OK);
    return statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function standaloneArchitecture(architecture: string): string {
  if (architecture === "x64") return "x86_64";
  if (architecture === "arm64") return "aarch64";
  return architecture;
}

/**
 * Mirrors `resolveHarnessInterpreter` in src/run-challenge.ts. Deliberately
 * duplicated rather than imported so the preflight keeps working while the
 * runner seam is edited — KEEP THE TWO IN SYNC when the order changes.
 */
function resolveInterpreter(repositoryRoot: string, environment: NodeJS.ProcessEnv): InterpreterResolution {
  try {
    const explicit = environment.HARNESS_PYTHON;
    if (explicit && isExecutable(explicit)) {
      return { source: "HARNESS_PYTHON", interpreter: explicit };
    }

    const executable = process.platform === "win32" ? "python3.exe" : "python3";
    for (const entry of (environment.PATH ?? "").split(path.delimiter)) {
      if (!entry) continue;
      const candidate = path.join(entry, executable);
      if (isExecutable(candidate)) {
        return { source: "PATH", interpreter: candidate };
      }
    }

    const extracted = path.join(
      repositoryRoot,
      "artifacts",
      "python",
      `${process.platform}-${process.arch}`,
      "bin",
      "python3",
    );
    if (isExecutable(extracted)) {
      return { source: "artifacts/python", interpreter: extracted };
    }

    const archive = path.join(
      repositoryRoot,
      "vendor",
      "python",
      `cpython-${standaloneArchitecture(process.arch)}-unknown-linux-gnu-install_only.tar.gz`,
    );
    if (existsSync(archive)) {
      return { source: "vendor/python", archive };
    }

    return { source: "unresolved" };
  } catch {
    return { source: "unresolved" };
  }
}

function parseVersion(raw: string): number[] {
  return raw
    .trim()
    .replace(/^v/, "")
    .split("-")[0]!
    .split(".")
    .map((part) => Number.parseInt(part, 10) || 0);
}

function compareVersions(left: string, right: string): number {
  const a = parseVersion(left);
  const b = parseVersion(right);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference !== 0) return difference < 0 ? -1 : 1;
  }
  return 0;
}

/** Supports only the simple space-separated comparator ranges used by this repo. */
function satisfiesRange(version: string, range: string): boolean {
  const comparators = range.trim().split(/\s+/).filter((entry) => entry.length > 0);
  for (const comparator of comparators) {
    const match = /^(>=|<=|>|<|=)?\s*v?(.+)$/.exec(comparator);
    if (!match) return false;
    const operator = match[1] ?? "=";
    const bound = match[2] ?? "";
    const order = compareVersions(version, bound);
    if (operator === ">=" && order < 0) return false;
    if (operator === ">" && order <= 0) return false;
    if (operator === "<=" && order > 0) return false;
    if (operator === "<" && order >= 0) return false;
    if (operator === "=" && order !== 0) return false;
  }
  return true;
}

function readJsonFile(file: string): Record<string, unknown> {
  return JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
}

function checkNode(): CheckResult {
  const version = process.versions.node;
  try {
    const manifest = readJsonFile(path.join(REPOSITORY_ROOT, "package.json"));
    const engines = manifest.engines as Record<string, string> | undefined;
    const range = engines?.node;
    if (!range) {
      return { status: "FAIL", name: "node", detail: `v${version}; package.json declares no engines.node range` };
    }
    if (!satisfiesRange(version, range)) {
      return { status: "FAIL", name: "node", detail: `v${version} does not satisfy "${range}"` };
    }
    return { status: "OK", name: "node", detail: `v${version} satisfies "${range}"` };
  } catch (error) {
    return { status: "FAIL", name: "node", detail: `v${version}; ${String(error)}` };
  }
}

function checkPiVersion(): CheckResult {
  const manifestPath = path.join(
    REPOSITORY_ROOT,
    "node_modules",
    "@earendil-works",
    "pi-coding-agent",
    "package.json",
  );
  try {
    const version = String(readJsonFile(manifestPath).version ?? "");
    if (version !== REQUIRED_PI_VERSION) {
      return { status: "FAIL", name: "pi", detail: `installed ${version || "unknown"}, required ${REQUIRED_PI_VERSION}` };
    }
    return { status: "OK", name: "pi", detail: `${version}` };
  } catch {
    return { status: "FAIL", name: "pi", detail: `cannot read ${path.relative(REPOSITORY_ROOT, manifestPath)}` };
  }
}

function checkPython(resolution: InterpreterResolution): CheckResult {
  if (resolution.archive) {
    return {
      status: "OK",
      name: "python",
      detail: `vendored archive ${path.relative(REPOSITORY_ROOT, resolution.archive)} (extracted on first use)`,
    };
  }
  const interpreter = resolution.interpreter;
  if (!interpreter) {
    return {
      status: "FAIL",
      name: "python",
      detail: "no interpreter from HARNESS_PYTHON, PATH, artifacts/python or vendor/python",
    };
  }
  const probe = spawnSync(interpreter, ["-c", "import sys;print(sys.version)"], {
    encoding: "utf8",
    timeout: LIST_MODELS_TIMEOUT_MS,
    shell: false,
  });
  if (probe.error || probe.status !== 0) {
    return {
      status: "FAIL",
      name: "python",
      detail: `${interpreter} (${resolution.source}) failed to report a version`,
    };
  }
  const reported = probe.stdout.trim().replace(/\s+/g, " ");
  return { status: "OK", name: "python", detail: `${interpreter} (${resolution.source}) ${reported}` };
}

function checkWritableDirectory(relative: string): CheckResult {
  const directory = path.join(REPOSITORY_ROOT, relative);
  const probe = path.join(directory, `.check-runtime-${process.pid}.tmp`);
  try {
    mkdirSync(directory, { recursive: true });
    writeFileSync(probe, "check-runtime\n", { flag: "wx" });
    unlinkSync(probe);
    return { status: "OK", name: `writable ${relative}/`, detail: directory };
  } catch (error) {
    return { status: "FAIL", name: `writable ${relative}/`, detail: `${directory}: ${String(error)}` };
  }
}

function checkModelResolves(): CheckResult {
  const provider = process.env.CHALLENGE_PROVIDER ?? "";
  const model = process.env.CHALLENGE_MODEL ?? "";
  if (!provider && !model) {
    return { status: "SKIP", name: "model", detail: "CHALLENGE_PROVIDER and CHALLENGE_MODEL unset" };
  }
  if (!model) {
    return {
      status: "SKIP",
      name: "model",
      detail: `CHALLENGE_PROVIDER=${provider}; CHALLENGE_MODEL unset, no model id to assert`,
    };
  }
  const piBinary = path.join(
    REPOSITORY_ROOT,
    "node_modules",
    ".bin",
    process.platform === "win32" ? "pi.cmd" : "pi",
  );
  if (!existsSync(piBinary)) {
    return { status: "FAIL", name: "model", detail: `${path.relative(REPOSITORY_ROOT, piBinary)} is missing` };
  }
  const listing = spawnSync(piBinary, ["--offline", "--no-extensions", "--list-models", model], {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    timeout: LIST_MODELS_TIMEOUT_MS,
    shell: false,
    env: { ...process.env, PI_OFFLINE: "1" },
  });
  if (listing.error) {
    return { status: "FAIL", name: "model", detail: `--list-models ${model} did not run (${listing.error.name})` };
  }
  // Pi exits 0 even when nothing resolves (measured), so assert on stdout.
  if (!listing.stdout.includes(model)) {
    return {
      status: "FAIL",
      name: "model",
      detail: `CHALLENGE_PROVIDER=${provider} CHALLENGE_MODEL=${model} did not appear in --list-models output`,
    };
  }
  return { status: "OK", name: "model", detail: `CHALLENGE_PROVIDER=${provider} CHALLENGE_MODEL=${model} resolves` };
}

function checkChallengeEnvironment(): CheckResult {
  const thinking = process.env.CHALLENGE_THINKING ?? "off (default)";
  const timeout = process.env.CHALLENGE_TIMEOUT_MS ?? "900000 (default)";
  return { status: "OK", name: "challenge env", detail: `thinking=${thinking} timeout_ms=${timeout}` };
}

function report(result: CheckResult): void {
  const line = `${result.status.padEnd(4)} ${result.name}: ${result.detail}`;
  if (result.status === "FAIL") console.error(line);
  else console.log(line);
}

function main(): void {
  const resolution = resolveInterpreter(REPOSITORY_ROOT, process.env);
  const results: CheckResult[] = [
    checkNode(),
    checkPiVersion(),
    checkPython(resolution),
    checkWritableDirectory("artifacts"),
    checkWritableDirectory("output"),
    checkChallengeEnvironment(),
    checkModelResolves(),
  ];

  for (const result of results) report(result);

  const failures = results.filter((result) => result.status === "FAIL");
  if (failures.length > 0) {
    console.error(`check:runtime FAILED (${failures.length} of ${results.length} checks)`);
    process.exitCode = 1;
    return;
  }
  console.log(`check:runtime OK (${results.length} checks)`);
}

main();
