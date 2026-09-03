# AgentCofounder starter

A forkable baseline for the AgentCofounder challenge. It gives every team the same pinned Pi runtime, neutral web application seed, execution command, telemetry collector, and public contract while leaving the actual agent strategy participant-owned.

This repository installs Pi as a local dependency at exactly `@earendil-works/pi-coding-agent@0.84.1`. Do not use the floating shell installer and do not run `pi update` during the challenge.

## Repository boundary

- `solution/` is the main participant surface: change the prompt, extension, skill, or replace the runner strategy.
- `app-template/` is the neutral application seed copied into a fresh generated workspace for every run.
- `contract-public/` contains the replaceable public idea, domain-neutral journey guidance, and the result schema.
- `src/` is the baseline runner and auditable result assembly.
- `output/app/` is disposable generated application code and is reset before every run.
- `artifacts/runs/` contains Pi JSON events, session JSONL files, stderr, and the run input.

Official hidden prompts, hidden tests, model credentials, and final scoring code must remain outside participant repositories.

> **Organizer release requirement:** `contract-public/development-idea.txt` is a development placeholder. Replace it with the finalized public prompt before sharing this repository with participants. Never place hidden judging material in this file.

## Prerequisites

- Node.js 22.19.x. The repository deliberately rejects other major versions.
- npm 10.9.3, matching the committed lockfiles and container image.
- Provider authentication supported by Pi, or organizer-provided provider/model environment variables.

## Setup

```bash
npm ci --ignore-scripts
npm --prefix app-template ci --ignore-scripts
npm run check
```

Provider-specific credentials are read by Pi. The optional challenge variables select the organizer's runtime configuration:

```bash
export CHALLENGE_PROVIDER="provider-name"
export CHALLENGE_MODEL="model-id"
export CHALLENGE_THINKING="off"
```

Never commit credentials. `.env.example` documents variable names, but the runner intentionally does not load `.env` files.

The default thinking level is `off` to avoid multiplying output-token cost in the efficiency ranking. Raise it only when measurements show the extra reasoning improves completion quality.

The strict Node engine is intentional. `npm ci` fails on Node 23+ (including Node 26); use `.nvmrc` or the provided container rather than regenerating the lockfile with a newer runtime.

The Docker build does **not** run the check suite (organizer ruling, 2026-09-03: the judged image build is not gated on it); `npm run check` still runs in CI. The image declares port 3000 for organizer-controlled browser evaluation; publishing that port still requires an explicit container port mapping or shared container network.

## Build, run, serve (the documented commands)

**Organizer ruling (2026-09-03):** the judged environment is *our* Dockerfile
and runtime, built and run for `linux/arm64` (Apple Silicon) — there is no
organizer-supplied image. For the BYO track, the organizers run the command we
document; that command is `scripts/judge.sh`. Build-time network access is
open; runtime network access is closed except the model gateway.

### `scripts/judge.sh` (the documented command)

```bash
scripts/judge.sh [--platform linux/arm64|linux/amd64] [--image NAME] [--serve] [--idea-file PATH]
```

It builds the image with `docker buildx`, creates a container with `output/`
and `artifacts/` bind-mounted to the host, runs the challenge, copies
`result.json` back to the repository root, prints its `status` /
`model_calls` / `points` fields, and removes the container. Pass `--serve` to
follow the run by serving `output/app` on `http://localhost:3000`. Pass
`--idea-file` to bind-mount a host idea file into the container read-only in
place of the repository's default. Run `scripts/judge.sh --help` for the full
flag reference, including the host-uid-1000 assumption behind the bind mounts
and the `chown` fallback if your host user differs.

### The equivalent manual Docker commands

Omit `--env-file .env` below if you have not created a local `.env` (see `.env.example`).

```bash
docker buildx build --platform linux/arm64 --load -t agentcofounder:arm64 .
mkdir -p output artifacts
docker create --platform linux/arm64 --name agentcofounder-run \
  --env-file .env \
  -e CHALLENGE_PROVIDER -e CHALLENGE_MODEL -e CHALLENGE_THINKING -e CHALLENGE_TIMEOUT_MS \
  -e BERGET_API_KEY -e CHALLENGE_API_KEY -e OPENAI_API_KEY \
  -v "$PWD/output:/challenge/output" \
  -v "$PWD/artifacts:/challenge/artifacts" \
  agentcofounder:arm64
docker start -a agentcofounder-run          # exit code is informational; result.json is authoritative
docker cp agentcofounder-run:/challenge/result.json ./result.json
docker rm agentcofounder-run
```

The `ENTRYPOINT` (`scripts/entrypoint.sh`) resolves the model-gateway
credential (first non-empty of `BERGET_API_KEY`, `CHALLENGE_API_KEY`,
`OPENAI_API_KEY`), logs only which name it used, and execs
`npm run challenge -- "$@"`.

### Serve the generated app

```bash
npm run serve       # npm --prefix output/app run dev, bound to 0.0.0.0:3000
```

or, without a local Node install, `docker run --rm -p 3000:3000 -v "$PWD/output:/challenge/output" --entrypoint npm agentcofounder:arm64 run serve`.

### Environment variables

| Variable | Purpose |
|---|---|
| `CHALLENGE_PROVIDER` | Provider name for the run (organizer-controlled during judging). |
| `CHALLENGE_MODEL` | Model id for the run (organizer-controlled during judging; default `zai-org/GLM-5.2`). |
| `CHALLENGE_THINKING` | Pi thinking level (default `off`, to avoid multiplying output-token cost). |
| `CHALLENGE_TIMEOUT_MS` | Wall-clock budget for the whole challenge run (default `900000`). |
| `BERGET_API_KEY` | Canonical model-gateway credential. |
| `CHALLENGE_API_KEY` | Alias for `BERGET_API_KEY` (first non-empty of the three wins). |
| `OPENAI_API_KEY` | Alias for `BERGET_API_KEY` (lowest-priority; first non-empty of the three wins). |
| `HARNESS_GATEWAY_URL` | Base URL for direct-gateway reasoning calls (default `https://api.berget.ai/v1`). |
| `HARNESS_DIRECT` | Set `0` to disable the direct-gateway Analyst seed call; it never blocks the Pi session on failure either way (default `1`). |
| `HARNESS_PYTHON` | Explicit Python interpreter for the `--agent python` orchestrator; falls back to `python3` on `PATH`, then `runPi` (default unset). |
| `HARNESS_THINKING_GUARD` | Set `0` to disable `solution/extensions/thinking-guard.ts`, which adds an explicit `enable_thinking:false` to the outgoing payload when nothing else on the wire already disables thinking (default `1`). |

Never commit credentials. `.env.example` documents every variable name above;
`scripts/entrypoint.sh` and `scripts/judge.sh` read `.env` only through Docker's
`--env-file`, never by loading it into harness code.

### Telemetry contract

Every model call — Pi's own and every direct-gateway call, retries and
orchestration-level calls included — is counted. `events.jsonl` carries every
Pi event plus one synthetic `message_end` per direct-gateway attempt, tagged
`"source":"direct-gateway"`. For the BYO track the per-invocation files are the
audit artifact, not `events.jsonl`: Pi session JSONL under
`artifacts/runs/<id>/sessions/<n>-<role>/`, and every direct-gateway attempt's
verbatim request/response logged to `artifacts/runs/<id>/harness/direct-calls.jsonl`.
`result.json` keeps `telemetry_source: "pi-json-event-stream"` (the schema
const) and, whenever `direct-calls.jsonl` exists, adds
`telemetry_sources: ["pi-json-event-stream","direct-gateway"]` and
`direct_call_count`.

```bash
npm run verify:telemetry     # re-derives call totals from the per-invocation files
                              # and diffs them against result.json's call_log; exits
                              # non-zero on the first mismatch
npm run submission           # copies a reference run's artifacts/runs/<id>/ and both
                              # result.json files into submission/ for committing
```

### A note on timings

Timings recorded in this repository (`docs/measurements.md`) were measured on
`linux/amd64`; the image itself targets and is judged on `linux/arm64`. Build
and run linux/arm64 under `docker buildx` with QEMU for a functional check —
expect it to run several times slower than native arm64 hardware.

## Run the public challenge

The runner uses `contract-public/development-idea.txt` by default. During template development it contains a placeholder; organizers must replace that file with the finalized public prompt before participant distribution.

```bash
npm run challenge
```

Use `--idea-file /path/to/idea.txt` to override the default for organizer testing or hidden evaluation.

For a setup-only check that does not call a model:

```bash
npm run challenge -- --prepare-only
```

After a complete run:

```bash
cd output/app
npm run dev
```

The app must be available at `http://localhost:3000`. In another terminal, validate the machine-readable result:

```bash
npm run validate:result -- output/app/result.json
```

## Result and telemetry ownership

The model writes `report.partial.json`, containing the product summary, assumptions, features, and tests. The runner writes `result.json` after parsing Pi's completed `message_end` events. This prevents the model from inventing headline token totals.

The runner appends the canonical domain-neutral journey guidance from `contract-public/journeys.md` to Pi's built-in system prompt. The protected-paths extension removes only Pi's documentation-reference block, retaining its tool list and usage guidance without steering the model toward package internals. The challenge guidance prevents implied behaviors from being dropped for simplicity while explicitly rejecting unrelated substitute features; the input idea remains authoritative.

The runner independently executes the pinned Vitest binary, requires at least one completed passing test with no skipped or todo tests, runs `npm run build`, starts the application, probes the published `http://localhost:3000` URL only while the spawned server is alive, and terminates the full process group. Product-journey records remain in the specification-defined `tests_run` field; `success` requires at least one such journey and no failed entries. Independent Vitest, build, and startup evidence is recorded in `harness_checks`. The runner also owns `app_url` and a location-aware `start_command`, so harmless formatting differences in the partial report cannot invalidate a run.

The runner records whether port 3000 was occupied before Pi starts. If Pi leaves a listener behind, cleanup only targets same-user listener processes whose working directory is the generated app; Linux uses `/proc`, while macOS uses bounded, non-blocking `lsof` calls. A listener that predates Pi is never reclaimed. The `port_reclamation` result field records whether cleanup was considered, attempted, and successful, plus the affected process IDs.

A provisional result is written before app verification starts. Verification failures degrade a completed model run to `partial`; Pi startup or telemetry failures remain `failed`. Equivalent final results are emitted at the generated app root (`output/app/result.json`) and repository root (`result.json`); only `start_command` differs so each command works from the directory containing its result. Failure to write either required destination makes the harness exit non-zero. Port 3000 must be free on both IPv4 and IPv6 loopback addresses before verification begins.

The raw event stream and Pi session files are retained for audit. Official judging must independently recompute usage and compare it with `result.json`; the participant-controlled report is never the final scoring authority.

`reasoning_tokens` and `cost_total` are included as additional audit fields. No efficiency score is calculated here because the public specification must first define the cache-write weighting and whether ranking uses the custom token formula or Pi's monetary cost.

## Develop the harness

The starter deliberately makes one autonomous Pi invocation. Possible participant improvements include:

- a shorter or more reliable prompt;
- specialized extensions or tools;
- reusable but domain-neutral application primitives;
- test-and-repair orchestration;
- deliberate prompt caching;
- a different Pi integration through its SDK or RPC mode.

Do not add a challenge idea's domain vocabulary or expected records to reusable code. The official judging idea will be different.

## Security

Pi and participant extensions execute with the permissions of the current process. The included extension rejects direct `write` and `edit` calls outside the generated app, but shell commands and symlink tricks can bypass an in-process guard. It is not a sandbox. Official evaluation must run each frozen submission in an isolated container or VM with a read-only harness mount and bounded CPU, memory, disk, time, and network access.

See `docs/organizer-checklist.md` before publishing the template or running a judged submission.
