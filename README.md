# AgentCofounder — a spec-driven, budget-aware build harness

Solo entry (Avinash Ranganath) on the **BYO Framework** track: custom
orchestration above the pinned Pi runtime. Judged model `zai-org/GLM-5.2` on
Berget, thinking off.

A non-technical product idea goes in; a small, tested, accessible, persistent
browser application comes out, with a report of what was built and the
decisions taken. The trick is to spend model tokens only where a model is
needed. One direct call turns the idea into a precise specification. The
application itself is a pre-built, typed scaffold that renders from a single
configuration file, so the model writes ~60 lines of configuration and ~80
lines of journey tests, nothing else. The harness then typechecks, tests and
builds the result for free, repairs from a precise brief when something is red,
and writes the report itself. Every model call — Pi sessions and direct calls,
retries included — is logged raw and reconciled against `result.json`.

## Requirements

Docker with `buildx` (Docker Desktop on Apple Silicon is fine). Nothing else on
the host. Internet at build time; at run time, only the model gateway.

## Run

```bash
export BERGET_API_KEY=...   # or CHALLENGE_API_KEY / OPENAI_API_KEY
scripts/judge.sh            # build linux/arm64 image, run one challenge, print status/model_calls/points
scripts/judge.sh --serve    # serve the generated app on http://localhost:3000
```

The manual equivalent:

```bash
docker buildx build --platform linux/arm64 --load -t agentcofounder:arm64 .
mkdir -p output artifacts
docker create --platform linux/arm64 --name acf-run \
  -e BERGET_API_KEY -e CHALLENGE_PROVIDER -e CHALLENGE_MODEL \
  -e CHALLENGE_THINKING -e CHALLENGE_TIMEOUT_MS \
  -v "$PWD/output:/challenge/output" \
  -v "$PWD/artifacts:/challenge/artifacts" \
  agentcofounder:arm64
docker start -a acf-run
docker cp acf-run:/challenge/result.json ./result.json
docker rm acf-run
docker run --rm -p 3000:3000 -v "$PWD/output:/challenge/output" \
  --entrypoint npm agentcofounder:arm64 run serve
```

The container exit code is informational; `result.json` is authoritative.

## Environment variables

- `BERGET_API_KEY` — model-gateway credential. `CHALLENGE_API_KEY` and
  `OPENAI_API_KEY` are aliases, resolved in that order; only the name used is
  ever logged, never the value.
- `CHALLENGE_PROVIDER` — default `berget`.
- `CHALLENGE_MODEL` — default `zai-org/GLM-5.2`.
- `CHALLENGE_THINKING` — default `off`.
- `CHALLENGE_TIMEOUT_MS` — default `900000`.

Missions always run with thinking off by design (measured on the unmodified
starter: thinking on blew the 900 s budget); `CHALLENGE_THINKING` applies to
the fallback single-session path. Developer `HARNESS_*` variables are documented in `.env.example` and
`docs/DEVELOPMENT.md`.

## Where the results are

`result.json` at the repository root and at `output/app/result.json`. Evidence
for a run is `artifacts/runs/<id>/`:

- `artifacts/runs/<id>/events.jsonl` — every Pi event, plus one synthetic
  `message_end` per direct call.
- `artifacts/runs/<id>/sessions/<n>-<role>/` — the Pi session JSONL per session.
- `artifacts/runs/<id>/harness/direct-calls.jsonl` — every direct-gateway
  attempt, request and response, verbatim.
- `artifacts/runs/<id>/harness/spec.json` — the derived specification.
- `artifacts/runs/<id>/app-test-results.json` — the runner's own vitest report.

`submission/2026-09-04T20-20-10-136Z/` is the committed reference run: the
public idea on the final code.

## Verify

```bash
npm run verify:telemetry -- artifacts/runs/2026-09-04T20-20-10-136Z result.json
npm run validate:result -- result.json
```

`verify:telemetry` proves that every logged model call — Pi and direct, retries
included — reconciles with `result.json`'s `call_log`.

Without Node on the host, `validate:result` runs inside the image:

```bash
docker run --rm -v "$PWD/result.json:/challenge/result.json:ro" \
  --entrypoint npm agentcofounder:arm64 run validate:result -- result.json
```

`verify:telemetry` lives in `tools/`, which `.dockerignore` keeps out of the
image, so it needs a host with Node 22.

## Test

```bash
npm run check
python3 -m unittest discover -s harness/tests -t . -p 'test_*.py'
```

## Results

Points = input + 3 × output + 0.1 × cache read; lower is better. Public idea,
GLM-5.2.

| Run | Points | Model calls | Agent phase |
|---|---|---|---|
| Unmodified starter baseline | 79,976 | 26 | 562 s (wall) |
| This harness, reference run | 31,065 | 6 | ~3 min, no repair, cold prompt cache |

Combined mode on the public idea bands at 14.6k–29k points, 3–11 calls.
Blind holdout — five unseen ideas, two runs each — passed 9 of 10 gates on the
first sweep. A second holdout of five ideas written by a third party exposed
six shape-level gaps (computed values, actions over every record, currency
units, date arithmetic, deletions, invented dialogs); the fixes are scaffold
primitives and brief rules, none naming a case. On the final code all ten
holdout ideas plus the organizers' practice prompt pass the gate: 9 of 11 in
one clean sweep at a mean of 31.6k points, the two misses passing on a rerun
after the last two fixes. The same original holdout on the smaller
`Qwen/Qwen3.8-27B-FP8` passed 3 of 5 gates with every run inside the 900 s
budget. Holdout text stays outside the repository.

The judged linux/arm64 image ran one full challenge under QEMU emulation on
the pre-holdout code: `success`, 3 model calls, 13,440 points. Emulated wall
time is not reported; a native arm64 timing was not taken.

## How it works

- **Scaffold** (`app-template/`) — a complete typed application (form, list,
  filters, badges, stats, action dialogs, undo, search, sort, dark mode,
  versioned localStorage) rendered from one config file.
- **Analyst** — one direct schema-constrained call turns the idea into the
  specification; every visible string is decided once.
- **Builder+Tester** — one write-only Pi session writes the config and the
  journey tests, nothing else.
- **Observe** — typecheck, tests, build: bounded, free of model tokens.
- **Supervisor** — a deterministic policy with repair caps and per-mission wall
  caps; a model is escalated to only on a stall.
- **Report** — harness-authored from the specification and the real test
  results.
- **Telemetry** — every call logged raw; `result.json` carries
  `telemetry_sources` and `direct_call_count`.

The rest is in `docs/ARCHITECTURE.md`.

## Security

No credentials in the repository. `.env` reaches the container only through
Docker's `--env-file`, never loaded by harness code. Holdout ideas live outside
the tree. Pi and participant extensions execute with the permissions of the
current process: the in-process guard rejects writes outside the generated app,
but shell commands and symlinks can bypass it. It is not a sandbox — judge each
submission in an isolated container or VM.
