# Architecture

BYO Framework track (custom orchestration above the pinned Pi runtime). One
plain-English idea in; a working React app, its journey tests and a validated
`result.json` out.

```
idea → Analyst (one direct json_schema call) → harness/spec.json
     → Architect (a pure function)           → harness/plan.json + briefs
     → one write-only Pi session             → src/app-config.ts, src/journeys.test.tsx
     → observe() → Supervisor policy → repair sessions → report.partial.json
```

**Analyst** (`harness/analyst.py`). One direct gateway call, strict
`json_schema`, thinking off. It turns the idea into a spec: fields with exact
labels and options, filters, badges, stats, actions with plain-English rules,
journeys with steps and expectations. `normalize_spec` makes that spec safe
to render (dedupes names, coerces kinds, forces a required text title field);
`spec_is_usable` decides whether the missions pipeline runs.

**Architect** (`harness/plan.py`, `harness/specstrings.py`). A pure function, no
model call: the spec fully determines the plan — one config file, one test file,
one test per journey. It renders the briefs: the config as data plus
rendering rules, the journeys plus a cheat sheet of every visible string, and
the repair/rerun briefs.

**Build session** (`harness/missions.py`, `harness/loop.py`). One Pi RPC session
with the `write` tool only, no skill, thinking forced off. Both files are
written in one turn from the brief alone; the session cannot read files or
re-edit its output, so self-review is impossible. The seed config and
journey template are already in the prefix, verbatim.

**observe()** (`harness/observe.py`, `harness/proc.py`). tsc → vitest JSON →
`vite build`, each bounded and stop-aware, plus line-count, seed-diff and
journey-coverage checks. No tokens.

**Supervisor** (`harness/supervisor.py`). A deterministic policy: rerun a
mission that wrote nothing; repair on tsc, vitest or build red with a brief
naming the file, the error and the rule; build once green; done. Repair cap 3,
no-progress cap 2 — both evaluated only when the latest observation still needs
a repair, so an app that goes green on the last allowed repair is still built
and reported `success`. A model Supervisor (a direct call, never a Pi session)
is consulted only on a stall, with thinking on only for a repeated stall.
Repairs use fresh `read,write,edit` sessions.

**Report.** The harness composes `report.partial.json` from the spec, the plan
and the real vitest results after every green observation and at the end; the
model never invents the numbers.

**Fallback.** No key, an unusable spec, or `HARNESS_MODE=single` → one
all-in-one Pi session for the whole run.

## Session strategy

Same idea, model and machine (measured 2026-09-04): per-mission
(Builder ∥ Tester, parallel) 23,806 / 21,585 / 17,570 points; single session
(two prompts) 14,610 / 15,011; combined (one prompt) 17,113 with read/edit
allowed and 16,652 write-only on a fully cold prefix — the judged condition.
Combined write-only is the default (`HARNESS_SESSION_MODE=combined`): three
model calls, no self-review turn; the others are one flag away.

## Budget

`harness/budget.py` gates every mission before it starts, refusing one that
cannot finish inside the deadline with the shutdown margin, or that would push
cumulative output past the 18,000-token ceiling. `missions.MISSION_MAX_S`
then caps each mission's wall clock by role (combined 360 s, builder and tester
300 s, repairer 180 s), clamped by whatever is left: a wedged repair is cut at
its cap, not left to eat the run.

## Telemetry

Every direct HTTP attempt, retries included, emits a synthetic `message_end`
record tagged `source: direct-gateway` into the event stream Pi writes, so
the organizers' parser counts direct calls like Pi calls, and appends its raw
request and response to `harness/direct-calls.jsonl`.
`npm run verify:telemetry -- artifacts/runs/<id> result.json` re-derives every
headline number from those calls. Each Pi session's prompt prefix is
hashed; more than one distinct hash means the prefix drifted.
