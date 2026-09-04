# AgentCofounder — a spec-driven, budget-aware build harness

Solo entry (Avinash Ranganath) for the AgentCofounder challenge, Starter Repo
track. The starter's pinned Pi runtime, runner, telemetry collector and public
contract are kept; the agent strategy on top of them is this repository's
contribution. Judged model: `zai-org/GLM-5.2` on Berget, thinking off.

**In one paragraph.** A non-technical product idea goes in; a small, tested,
accessible, persistent browser application comes out, with a report of what
was built and which decisions were taken. The trick is to spend model tokens
only where a model is needed. One direct call turns the idea into a precise
specification. The application itself is a pre-built, typed scaffold that
renders from a single configuration file, so the model writes ~60 lines of
configuration and ~80 lines of journey tests, nothing else. The harness then
typechecks, tests and builds the result for free, repairs from a precise brief
when something is red, and writes the report itself. Every model call — Pi
sessions and direct calls, retries included — is logged raw and reconciled
against `result.json` by `npm run verify:telemetry`.

## Architecture

```
scripts/judge.sh                       build the linux/arm64 image, run the challenge, serve the app
└─ npm run challenge                   src/run-challenge.ts — the starter pipeline, one additive block
   ├─ prepareOutput()                  copies app-template/ (the scaffold) into output/app, npm ci
   ├─ python3 -m harness               the orchestrator (stdlib Python; stdout = Pi events only)
   │   ├─ Analyst        direct json_schema call → harness/spec.json (fields, filters, badges,
   │   │                 stats, actions, journeys, assumptions — every visible string decided once)
   │   ├─ Architect      a pure function → harness/plan.json and the mission briefs (no model call)
   │   ├─ Builder+Tester one Pi RPC session, one prompt, exactly two writes:
   │   │                 src/app-config.ts then src/journeys.test.tsx (tools: read,write,edit)
   │   ├─ observe()      tsc → vitest JSON → vite build, bounded, free
   │   ├─ Supervisor     a deterministic policy: repair from a precise brief (cap 3, no-progress
   │   │                 cap 2), rerun a mission that wrote nothing, build once green, done;
   │   │                 a model Supervisor only on a stall
   │   └─ report         harness-authored report.partial.json after every green observation
   ├─ collectUsageFromJsonLines()      counts Pi calls AND the synthetic message_end per direct call
   ├─ verifyGeneratedApp()             the starter's independent vitest / build / port-3000 probe
   └─ result.json                      + telemetry_sources, direct_call_count (additive hunk)
npm run serve                          serves output/app on 0.0.0.0:3000 for the browser judge
```

Fallbacks, in order: no API key or no usable spec → the single-session path
(the model reads the scaffold map and does everything itself, exactly the
Phase 2 flow); no `python3` → the runner's own `runPi()` path. Both are
measured and both produce a valid `result.json`.

### The scaffold (`app-template/`)

A complete record application — form, list, filters, badges, stats, actions
with input/confirm dialogs, delete with undo, search, sort, toasts, an error
boundary, versioned localStorage with corrupt-data recovery, a design system
with dark mode and 360–1280 px layouts — rendered from one typed declaration,
`src/app-config.ts` (`defineApp({...})` with full inference of `row.<field>`
in every predicate). `AGENTS.md` is the scaffold map: it embeds the seed
configuration and the journey-test template verbatim, so no mission ever
reads a file. The seed ships zero runnable tests on purpose; the generated
`src/journeys.test.tsx` uses `src/test/helpers.tsx` (queries by accessible
name only). Nothing in the scaffold names a domain.

### The seam

`src/run-challenge.ts` keeps the starter pipeline and adds one block:
`--agent python|pi` (default `python`), an interpreter resolver, and
`runHarness()` which spawns `python3 -m harness --idea-file … --session-root …
--cwd … --timeout-ms … --repo-root … --thinking off` with a timeout strictly
inside the runner's own, pipes the harness's stdout straight into
`events.jsonl`, and closes Pi sessions by stdin EOF (Pi's SIGTERM path skips
its stdout flush). `git diff baseline-starter -- src/ test/ contract-public/`
is additive only. The guarded starter files (`usage.ts`, `result.ts`,
`verify-app.ts`, `prepare-output.ts`, `port-owner.ts`, `process-tree.ts`,
`validate-result.ts`, `test/`, `contract-public/`) are untouched.

### Session strategy, measured

Three transports for the Builder and Tester missions exist behind
`HARNESS_SESSION_MODE`, all with a byte-identical prompt prefix (checked by
the prefix hash in `harness/payload.jsonl`):

| Mode | What it is | Points on the public idea (same night, same model) |
|---|---|---|
| `per-mission` | fresh Pi session per mission, Builder ∥ Tester in parallel, staggered 1.5 s for the prefix cache | 23,806 / 21,585 / 17,570 |
| `single` | one session, the two briefs as consecutive prompts | 14,610 / 15,011 |
| `combined` (default) | one session with only the `write` tool, one prompt, two writes (usually in one turn); repairs in fresh read/write/edit sessions | 17,113 (read/edit still allowed) → 16,652 with a fully cold prefix, 3 calls |

The parallel transport loses on points: the second session pays a partial
prefix and its own closing turn, and the Tester gains nothing from
independence that the specification did not already give it. The winner is
kept as the default; the others stay one flag away.

## Measured results

The full tables, per-call anatomies and the reasoning are in
`docs/measurements.md`. Points = input + 3 × output + 0.1 × cache read, on
the public idea, GLM-5.2, status `success` unless stated:

| Stage | Points | Calls | Agent phase |
|---|---|---|---|
| Starter baseline (2 Sept) | 79,976 | 26 | 562 s |
| Phase 1: Python RPC seam at parity | 87k–192k (same-day controls 113k–177k; day-to-day variance is 2×) | 26–52 | 400–820 s |
| Phase 2 step 1: `afterEach(cleanup)` in the seed | 83,074 / 94,029 | 31–33 | 385–428 s |
| Phase 2: config-driven scaffold + prompt | 26,432 / 24,147 | 9–11 | 94–100 s |
| Phase 3: spec → one write-only session → observe → supervisor | 16,652 (cold prefix), 14,610–17,570 across transports | 3–6 | 42–72 s |

Holdout evaluation (five unseen ideas, two runs each, `python3 -m harness.eval`,
combined mode): **9 of 10 gates passed** at 13.7k–41.8k points; the one
failure and two expensive runs were prompt-layer bugs (an `as const`, a
blanked select, a rule left as a string), each since fixed with an explicit
rule and a repair hint. Full table and the fixes in `docs/measurements.md`.

The same holdout set on the smaller, faster `Qwen/Qwen3.8-27B-FP8` passes
3 of 5 gates in combined mode with every run inside the 900 s budget, showing the
harness is not overfit to one model. That sweep also surfaced and fixed a real
robustness hole — an uncapped repair on a non-caching model could run away — now
bounded by a per-mission wall cap.

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

`report.partial.json` carries the product summary, assumptions, features and tests. In missions mode the harness writes it from the Analyst's specification and the real vitest results (after every green observation, so a run killed on the deadline still leaves a valid `partial`); in the single-session fallback the model writes it and the harness only repairs a malformed `tests_run`. The runner writes `result.json` after parsing every completed `message_end` event — Pi's and the synthetic one emitted per direct call — so no participant code can invent headline token totals.

The runner appends the canonical domain-neutral journey guidance from `contract-public/journeys.md` to Pi's built-in system prompt. The protected-paths extension removes only Pi's documentation-reference block, retaining its tool list and usage guidance without steering the model toward package internals. The challenge guidance prevents implied behaviors from being dropped for simplicity while explicitly rejecting unrelated substitute features; the input idea remains authoritative.

The runner independently executes the pinned Vitest binary, requires at least one completed passing test with no skipped or todo tests, runs `npm run build`, starts the application, probes the published `http://localhost:3000` URL only while the spawned server is alive, and terminates the full process group. Product-journey records remain in the specification-defined `tests_run` field; `success` requires at least one such journey and no failed entries. Independent Vitest, build, and startup evidence is recorded in `harness_checks`. The runner also owns `app_url` and a location-aware `start_command`, so harmless formatting differences in the partial report cannot invalidate a run.

The runner records whether port 3000 was occupied before Pi starts. If Pi leaves a listener behind, cleanup only targets same-user listener processes whose working directory is the generated app; Linux uses `/proc`, while macOS uses bounded, non-blocking `lsof` calls. A listener that predates Pi is never reclaimed. The `port_reclamation` result field records whether cleanup was considered, attempted, and successful, plus the affected process IDs.

A provisional result is written before app verification starts. Verification failures degrade a completed model run to `partial`; Pi startup or telemetry failures remain `failed`. Equivalent final results are emitted at the generated app root (`output/app/result.json`) and repository root (`result.json`); only `start_command` differs so each command works from the directory containing its result. Failure to write either required destination makes the harness exit non-zero. Port 3000 must be free on both IPv4 and IPv6 loopback addresses before verification begins.

The raw event stream and Pi session files are retained for audit. Official judging must independently recompute usage and compare it with `result.json`; the participant-controlled report is never the final scoring authority.

`reasoning_tokens` and `cost_total` are included as additional audit fields. No efficiency score is calculated here because the public specification must first define the cache-write weighting and whether ranking uses the custom token formula or Pi's monetary cost.

## Develop

```bash
npm run check                                   # typecheck, runner tests, scaffold tests + build
python3 -m unittest discover -s harness/tests -t . -p 'test_*.py'   # the harness suite (fake Pi, fake gateway; no tokens)
npm run check:runtime                           # what the image must satisfy (python3.10 floor, Pi 0.84.1, models.json)
npm run verify:telemetry -- artifacts/runs/<id> result.json         # per-invocation files must sum to call_log
npm run submission -- <run id>                  # copy a reference run into submission/
python3 -m harness.eval --cases <abs dir outside the repo> --repeats 2 \
  --output-root <abs dir> --report-dir <abs dir> [--baseline <eval-*.json>]   # holdout sweep
```

Harness flags (all optional): `HARNESS_MODE=missions|single`,
`HARNESS_SESSION_MODE=combined|single|per-mission`, `HARNESS_REVIEWER=1`,
`HARNESS_COVERAGE_REPAIR=1`, `HARNESS_DIRECT=0` (no direct calls),
`HARNESS_PI_AUTO_RETRY=0`. Test-only knobs (`HARNESS_PI_BIN`,
`HARNESS_GATEWAY_URL`, `HARNESS_FAULT=tsc`, `FAKE_PI_*`) are documented in
`harness/tests/fake_pi.py` and never set in a judged run.

Do not add a challenge idea's domain vocabulary or expected records to
reusable code: the scaffold, the prompts and the harness are domain-neutral
by construction (the seed configuration is a deliberately abstract "Record
Tracker"), and the holdout ideas live outside the repository.

## Security

Pi and participant extensions execute with the permissions of the current process. The included extension rejects direct `write` and `edit` calls outside the generated app, but shell commands and symlink tricks can bypass an in-process guard. It is not a sandbox. Official evaluation must run each frozen submission in an isolated container or VM with a read-only harness mount and bounded CPU, memory, disk, time, and network access.

See `docs/organizer-checklist.md` before publishing the template or running a judged submission.
