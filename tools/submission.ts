/**
 * Contract C10: `npm run submission` — copies a reference run's raw audit
 * artifacts into `submission/` for committing (Ravi asked for raw logs in the
 * repository, orchestrator addendum). Copies `artifacts/runs/<id>/`
 * recursively, plus both `result.json` files (repository root and
 * `output/app/`), into `submission/<id>/`, preserving each source's
 * repository-relative path so the two `result.json` copies never collide.
 * Never deletes anything; a missing `result.json` at either path is a
 * warning, not a failure — the run directory alone is still worth copying.
 *
 * Usage: `npm run submission -- [runId]` (default: the latest
 * `artifacts/runs/<id>`).
 */
import { cpSync, existsSync, mkdirSync, readdirSync, copyFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SOURCE_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SOURCE_DIRECTORY, "..");

/** The most recently created `artifacts/runs/<id>` directory name (ISO ids sort chronologically). */
export function latestRunId(repositoryRoot: string): string {
  const runsRoot = path.join(repositoryRoot, "artifacts", "runs");
  const entries = readdirSync(runsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
  const latest = entries.at(-1);
  if (latest === undefined) throw new Error(`No run directories found under ${runsRoot}`);
  return latest;
}

export interface SubmissionResult {
  runDir: string;
  destination: string;
  copiedRootResult: boolean;
  copiedAppResult: boolean;
  /** Set when the two result.json files were deliberately not copied. */
  resultFilesSkipped?: string;
}

/**
 * Copies `<repositoryRoot>/artifacts/runs/<runId>/` recursively into
 * `<repositoryRoot>/submission/<runId>/`, then the two `result.json` files at
 * their own repository-relative paths under the same destination. Pure with
 * respect to every other path in the repository: nothing is deleted, and
 * only `submission/<runId>/` is written.
 */
export function buildSubmission(repositoryRoot: string, runId: string): SubmissionResult {
  const runDir = path.join(repositoryRoot, "artifacts", "runs", runId);
  if (!existsSync(runDir)) throw new Error(`Run directory not found: ${runDir}`);

  const destination = path.join(repositoryRoot, "submission", runId);
  mkdirSync(destination, { recursive: true });
  cpSync(runDir, destination, { recursive: true });

  // result.json (root and output/app) always describe the LATEST run: the
  // runner rewrites both on every challenge. Copying them next to an older
  // run's artifacts would pair one run's telemetry with another run's logs,
  // and `verify:telemetry` would then fail on the committed reference run.
  const latest = latestRunId(repositoryRoot);
  if (runId !== latest) {
    return {
      runDir,
      destination,
      copiedRootResult: false,
      copiedAppResult: false,
      resultFilesSkipped: `result.json files belong to the latest run (${latest}), not ${runId}; re-run the challenge or pass the latest run id`,
    };
  }

  const rootResultPath = path.join(repositoryRoot, "result.json");
  const copiedRootResult = existsSync(rootResultPath);
  if (copiedRootResult) copyFileSync(rootResultPath, path.join(destination, "result.json"));

  const appResultPath = path.join(repositoryRoot, "output", "app", "result.json");
  const copiedAppResult = existsSync(appResultPath);
  if (copiedAppResult) {
    const appResultDestinationDir = path.join(destination, "output", "app");
    mkdirSync(appResultDestinationDir, { recursive: true });
    copyFileSync(appResultPath, path.join(appResultDestinationDir, "result.json"));
  }

  return { runDir, destination, copiedRootResult, copiedAppResult };
}

function main(): void {
  const runId = process.argv[2] ?? latestRunId(REPOSITORY_ROOT);
  const result = buildSubmission(REPOSITORY_ROOT, runId);
  console.log(`Copied ${result.runDir} to ${result.destination}`);
  if (result.resultFilesSkipped) {
    console.warn(result.resultFilesSkipped);
    return;
  }
  if (result.copiedRootResult) {
    console.log(`Copied result.json to ${path.join(result.destination, "result.json")}`);
  } else {
    console.warn(`result.json not found at the repository root; skipped`);
  }
  if (result.copiedAppResult) {
    console.log(`Copied output/app/result.json to ${path.join(result.destination, "output", "app", "result.json")}`);
  } else {
    console.warn(`output/app/result.json not found; skipped`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
