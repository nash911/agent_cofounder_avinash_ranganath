import { existsSync, readFileSync } from "node:fs";
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { buildSubmission, latestRunId } from "../tools/submission.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true })));
});

async function temporaryRepository(): Promise<string> {
  const directory = await mkdtemp(path.join(os.tmpdir(), "agent-cofounder-submission-"));
  temporaryDirectories.push(directory);
  return directory;
}

async function seedRun(repositoryRoot: string, runId: string): Promise<void> {
  const runDir = path.join(repositoryRoot, "artifacts", "runs", runId);
  await mkdir(path.join(runDir, "sessions", "1-builder"), { recursive: true });
  await mkdir(path.join(runDir, "harness"), { recursive: true });
  await writeFile(path.join(runDir, "events.jsonl"), '{"type":"agent_start"}\n', "utf8");
  await writeFile(path.join(runDir, "idea.txt"), "Build a tool.\n", "utf8");
  await writeFile(
    path.join(runDir, "sessions", "1-builder", "session.jsonl"),
    '{"type":"session","version":3,"id":"s1","timestamp":"2026-09-03T00:00:00.000Z"}\n',
    "utf8",
  );
  await writeFile(path.join(runDir, "harness", "direct-calls.jsonl"), "", "utf8");
}

describe("latestRunId", () => {
  it("picks the lexicographically last run directory (ISO ids sort chronologically)", async () => {
    const repositoryRoot = await temporaryRepository();
    await seedRun(repositoryRoot, "2026-09-01T00-00-00-000Z");
    await seedRun(repositoryRoot, "2026-09-03T08-50-52-600Z");
    await seedRun(repositoryRoot, "2026-09-02T10-00-00-000Z");

    expect(latestRunId(repositoryRoot)).toBe("2026-09-03T08-50-52-600Z");
  });

  it("throws when no run directories exist", async () => {
    const repositoryRoot = await temporaryRepository();
    await mkdir(path.join(repositoryRoot, "artifacts", "runs"), { recursive: true });
    expect(() => latestRunId(repositoryRoot)).toThrow(/No run directories/u);
  });
});

describe("buildSubmission", () => {
  it("copies artifacts/runs/<id>/ recursively, plus both result.json files, into submission/<id>/", async () => {
    const repositoryRoot = await temporaryRepository();
    const runId = "2026-09-03T08-50-52-600Z";
    await seedRun(repositoryRoot, runId);
    await writeFile(path.join(repositoryRoot, "result.json"), JSON.stringify({ status: "success" }), "utf8");
    await mkdir(path.join(repositoryRoot, "output", "app"), { recursive: true });
    await writeFile(
      path.join(repositoryRoot, "output", "app", "result.json"),
      JSON.stringify({ status: "success", start_command: "npm run dev" }),
      "utf8",
    );

    const outcome = buildSubmission(repositoryRoot, runId);

    expect(outcome.copiedRootResult).toBe(true);
    expect(outcome.copiedAppResult).toBe(true);
    expect(outcome.destination).toBe(path.join(repositoryRoot, "submission", runId));

    // The run directory's own contents landed at the top level of submission/<id>/.
    expect(existsSync(path.join(outcome.destination, "events.jsonl"))).toBe(true);
    expect(existsSync(path.join(outcome.destination, "idea.txt"))).toBe(true);
    expect(existsSync(path.join(outcome.destination, "sessions", "1-builder", "session.jsonl"))).toBe(true);
    expect(existsSync(path.join(outcome.destination, "harness", "direct-calls.jsonl"))).toBe(true);

    // Both result.json files, at their own repository-relative paths, never colliding.
    const rootResultCopy = JSON.parse(readFileSync(path.join(outcome.destination, "result.json"), "utf8")) as {
      status: string;
    };
    expect(rootResultCopy.status).toBe("success");
    const appResultCopy = JSON.parse(
      readFileSync(path.join(outcome.destination, "output", "app", "result.json"), "utf8"),
    ) as { start_command: string };
    expect(appResultCopy.start_command).toBe("npm run dev");

    // Nothing at the source was touched.
    expect(existsSync(path.join(repositoryRoot, "artifacts", "runs", runId, "events.jsonl"))).toBe(true);
    expect(existsSync(path.join(repositoryRoot, "result.json"))).toBe(true);
  });

  it("warns via the return value, but still copies the run, when a result.json is missing", async () => {
    const repositoryRoot = await temporaryRepository();
    const runId = "2026-09-03T09-00-00-000Z";
    await seedRun(repositoryRoot, runId);

    const outcome = buildSubmission(repositoryRoot, runId);

    expect(outcome.copiedRootResult).toBe(false);
    expect(outcome.copiedAppResult).toBe(false);
    expect(existsSync(path.join(outcome.destination, "result.json"))).toBe(false);
    expect(existsSync(path.join(outcome.destination, "events.jsonl"))).toBe(true);
  });

  it("throws rather than copying anything when the run directory does not exist", async () => {
    const repositoryRoot = await temporaryRepository();
    await mkdir(path.join(repositoryRoot, "artifacts", "runs"), { recursive: true });
    expect(() => buildSubmission(repositoryRoot, "does-not-exist")).toThrow(/Run directory not found/u);
    expect(existsSync(path.join(repositoryRoot, "submission"))).toBe(false);
  });

  it("never deletes an existing submission/<id>/ entry it does not overwrite", async () => {
    const repositoryRoot = await temporaryRepository();
    const runId = "2026-09-03T09-10-00-000Z";
    await seedRun(repositoryRoot, runId);
    const destination = path.join(repositoryRoot, "submission", runId);
    await mkdir(destination, { recursive: true });
    await writeFile(path.join(destination, "reviewer-notes.txt"), "kept\n", "utf8");

    buildSubmission(repositoryRoot, runId);

    expect(existsSync(path.join(destination, "reviewer-notes.txt"))).toBe(true);
    const preserved = readFileSync(path.join(destination, "reviewer-notes.txt"), "utf8");
    expect(preserved).toBe("kept\n");
    // The run's own files still landed alongside it.
    const entries = await readdir(destination);
    expect(entries).toContain("events.jsonl");
  });
});
