# Development

## Local setup (no Docker)

- Node **22.19.0** (`.nvmrc`; `engines` requires `>=22.19.0 <23`).
- `npm ci --ignore-scripts` at the root, then
  `npm --prefix app-template ci --ignore-scripts`.
- `python3` ≥ 3.10. The harness is standard library only — nothing to install.
- `cp .env.example .env`, then set one of `BERGET_API_KEY` /
  `CHALLENGE_API_KEY` / `OPENAI_API_KEY`. Leave `PI_CODING_AGENT_DIR`
  commented out unless you run outside Docker, and then only as an absolute path.
- `npm run check` (typecheck, tests, app tests, app build) and
  `python3 -m unittest discover -s harness/tests -t . -p 'test_*.py'`
  (the harness suite; fake Pi, fake gateway, no tokens).

## Environment variables

- `HARNESS_MODE` — `missions` (default) or `single` to force the fallback path.
- `HARNESS_SESSION_MODE` — `combined` (default), `single`, or `per-mission`.
- `HARNESS_REVIEWER` — `1` runs the post-build review call (default off).
- `HARNESS_COVERAGE_REPAIR` — `1` makes an untested journey trigger a repair.
- `HARNESS_DIRECT` — `0` disables the Analyst's direct call (falls back).
- `HARNESS_PI_AUTO_RETRY` — `0` turns off Pi's own transient-error retry.
- `HARNESS_NARRATE` — `0` silences the plain-English stderr narration.
- `HARNESS_PYTHON` — interpreter that runs `-m harness` (default: `python3`).
- `HARNESS_THINKING_GUARD` — `0` disables the extension that pins thinking off.
- `HARNESS_GATEWAY_URL` — base URL for every direct call.
- `HARNESS_PAYLOAD_LOG` — request-payload log path (defaults into the run's
  `harness/`).
- `HARNESS_RESUME_ATTEMPTS` / `_BACKOFF_S` / `_MIN_BUDGET_S` — resume tuning.
- `HARNESS_COLOR` — set by the runner for a TTY; do not set it by hand.

Test-only, never for a real run: `HARNESS_PI_BIN` (points at
`harness/tests/fake_pi.py`), its `FAKE_PI_*` knobs (documented in that file's
docstring), `HARNESS_TSC_BIN` / `HARNESS_VITEST_BIN` / `HARNESS_VITE_BIN`
(toolchain stubs), `HARNESS_TEST_SCRATCH`, and `HARNESS_FAULT=tsc`
(injects a type error after the build, for measurement).

## Eval runner

```bash
python3 -m harness.eval --cases <ideas> --output-root <dir> --report-dir <dir> \
  [--repeats 2] [--agent python|pi] [--baseline eval-*.json] [--label tag]
```

`--cases`, `--output-root` and `--report-dir` must be absolute paths outside
the repository.

## Reproducing a measurement

```bash
HARNESS_SESSION_MODE=combined npm run challenge -- --agent python
npm run verify:telemetry -- artifacts/runs/<id> result.json
```

Artifacts land under `artifacts/runs/<id>/`: `events.jsonl`, `idea.txt`,
`app-test-results.json`, `sessions/<label>/` (raw Pi JSONL), and `harness/`
(`spec.json`, `plan.json`, `observe-*.json`, `supervisor.json`,
`missions.json`, `budget.json`, `direct-calls.jsonl`, `harness.log`). The
generated app is `output/app`; `result.json` is written there and at the
repository root.
