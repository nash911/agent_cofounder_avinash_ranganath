"""Missions mode: Builder ∥ Tester, then observe → decide → repair (§7).

``harness/__main__.py`` keeps the run's framing -- validation, signals,
credentials, the budget controller, the Analyst, the prefix check, the budget
snapshot and the exit code -- and hands the middle of the run to exactly one of
two bodies: the Phase 2 all-in-one session (``run_single_session``, unchanged)
or :func:`run_missions` here.

Why this is a separate module rather than more of ``__main__``: the two bodies
share nothing but :class:`RunContext`, and ``__main__`` is already the largest
file in the harness. It also keeps the direction of imports one-way --
``__main__`` imports this, never the reverse. Importing ``harness.__main__`` by
package name from a module that ``python3 -m harness`` has already loaded as
``__main__`` would execute a *second* copy of it, which is why :class:`Supervisor`
duplicates its budget gate too.

The loop itself is deliberately small. Every question about what to do next is
:meth:`harness.supervisor.Supervisor.decide`'s; every question about what the
app currently *is* is :func:`harness.observe.observe`'s; every brief is
:mod:`harness.plan`'s. What is left here is the order, the budget, and the
report.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .budget import BudgetController
from .log import log, narrate, warn
from .missions import (
    PREDICTED_OUTPUT_TOKENS,
    REPAIRER_PREDICTED_OUTPUT_TOKENS,
    SHUTDOWN_RESERVE_S,
    MissionRunner,
    MissionSpec,
)
from .observe import Observation, observe as observe_app
from .plan import (
    CONFIG_FILE, builder_brief, combined_brief, derive_plan, repair_brief, rerun_brief, tester_brief,
)
from .supervisor import Supervisor
from . import report as report_mod

#: The mission prefix's own half of the append-system prompt. The other half is
#: the app's ``AGENTS.md`` (which now reproduces the seed config and the journey
#: template in full, so no mission needs a ``read``). ``journeys.md`` is
#: deliberately absent: its coverage checklist moved into the Analyst's prompt.
MISSIONS_PROMPT_FILE = "system-prompt.missions.md"

#: ``changed_from_seed`` and the over-150-line check compare against the pristine
#: scaffold, which is the repository's own ``app-template`` -- the app directory
#: is a copy of it.
SEED_DIR_NAME = "app-template"

#: A hard stop on the loop, independent of the caps the Supervisor enforces
#: (repair 3, no-progress 2, one rerun per role). Nothing should ever reach it;
#: it exists so a policy bug cannot turn into an infinite loop against a
#: 900 s deadline.
MAX_ROUNDS = 12

#: Observation budgets. The build round needs tsc + vitest + ``vite build``,
#: which is why it gets the larger slice (harness/observe.py caps each step
#: at 60/60/90 s and gives each one whatever is left of this).
OBSERVE_TIMEOUT_S = 90.0
BUILD_OBSERVE_TIMEOUT_S = 150.0

#: Below this much wall clock left (after the shutdown reserve) there is no
#: point starting another observation: it would be cut short and its verdict
#: would be about the clock, not the app.
MIN_OBSERVE_S = 5.0

#: ``HARNESS_SESSION_MODE`` (§1): a fresh session per mission, or one session
#: for the whole run with the missions as consecutive prompts.
SESSION_MODES = ("per-mission", "single", "combined")
#: Measured 2026-09-04 on the public idea (docs/measurements.md, Phase 3):
#: per-mission 23.8k/21.6k/17.6k points, single 14.6k/15.0k, combined the
#: cheapest once its session is write-only. The parallel transport pays a
#: second partial prefix and a second closing turn for an independence the
#: specification already provides.
DEFAULT_SESSION_MODE = "combined"


@dataclass
class RunContext:
    """Everything a run body needs, resolved once by ``__main__.run``.

    Built in ``run()`` so both bodies see exactly the same validated paths,
    deadline, controller and signal state; neither body resolves configuration
    of its own beyond the env flags that are specific to it.
    """

    args: argparse.Namespace
    idea: str
    repository_root: pathlib.Path
    app_directory: pathlib.Path
    session_root: pathlib.Path
    harness_directory: pathlib.Path
    pi_binary: pathlib.Path
    extensions: List[pathlib.Path]
    child_env: Dict[str, str]
    append_system: str
    thinking: str
    deadline: float
    stop_event: threading.Event
    signalled: List[str]
    controller: BudgetController
    gate_active: bool
    #: The run's resolved provider/model (``__main__.resolve_provider_model``),
    #: passed explicitly to every Pi session so ``--provider``/``--model`` are
    #: always on the command line. ``run()`` always fills both in; the empty
    #: defaults exist only so a test can build a context without them.
    provider: str = ""
    model: str = ""
    spec: Optional[Dict[str, Any]] = None
    client: Any = None
    restore_signals: Callable[[], None] = lambda: None
    usage_observer: Optional[Callable[[Dict[str, Any]], None]] = None


# -- configuration ----------------------------------------------------------


def session_mode() -> str:
    """``HARNESS_SESSION_MODE``; an unknown value warns and falls back."""
    raw = (os.environ.get("HARNESS_SESSION_MODE") or "").strip().lower()
    if not raw:
        return DEFAULT_SESSION_MODE
    if raw in SESSION_MODES:
        return raw
    warn('ignoring invalid HARNESS_SESSION_MODE "{0}"; using "{1}"'.format(raw, DEFAULT_SESSION_MODE))
    return DEFAULT_SESSION_MODE


def coverage_repair_enabled() -> bool:
    """``HARNESS_COVERAGE_REPAIR=1`` turns an untested journey into a repair (§4)."""
    return os.environ.get("HARNESS_COVERAGE_REPAIR", "").strip() == "1"


def build_missions_system_prompt(
    repository_root: pathlib.Path, app_directory: pathlib.Path
) -> str:
    """``solution/system-prompt.missions.md`` + the app's ``AGENTS.md``.

    Byte-identical for every mission session in the run, which is the whole
    point: the provider caches the prefix after the first request answers, and
    every later mission pays cache-read prices for it (§9 forbids a mission
    session whose prefix differs from another's).
    """
    parts: List[str] = []
    prompt_path = repository_root / "solution" / MISSIONS_PROMPT_FILE
    if prompt_path.is_file():
        parts.append(prompt_path.read_text(encoding="utf-8").strip())
    else:
        warn("no {0} at {1}; missions run without it".format(MISSIONS_PROMPT_FILE, prompt_path))
    agents_path = app_directory / "AGENTS.md"
    if agents_path.is_file():
        parts.append(agents_path.read_text(encoding="utf-8").strip())
    else:
        warn("no AGENTS.md at {0}; missions run without it".format(agents_path))
    return "\n\n".join(parts)


# -- the run ----------------------------------------------------------------


def run_missions(context: RunContext) -> bool:
    """Drive the whole missions-mode run. Returns "any mission was usable".

    The return value is the exit code's input and nothing else: a run that
    produced a red app but a real assistant turn still exits 0, exactly as the
    single-session path does (the runner grades the app, not the harness).
    """
    harness_dir = context.harness_directory
    spec: Dict[str, Any] = context.spec if isinstance(context.spec, dict) else {}
    plan = derive_plan(spec)
    _write_json(harness_dir / "plan.json", plan)

    mode = session_mode()
    runner = MissionRunner(
        pi_binary=context.pi_binary,
        app_directory=context.app_directory,
        harness_directory=harness_dir,
        session_root=context.session_root,
        append_system=build_missions_system_prompt(
            context.repository_root, context.app_directory
        ),
        extensions=list(context.extensions),
        provider=context.provider,
        model=context.model,
        thinking=context.thinking,
        env=dict(context.child_env),
        stop_event=context.stop_event,
        controller=context.controller,
        gate_active=context.gate_active,
        on_event=context.usage_observer,
        deadline=context.deadline,
        # "combined" writes both files from one prompt in a session that has
        # only the `write` tool (measured 2026-09-04, python-combined-a: with
        # read/edit available the model added an edit, a read and a closing
        # turn after its two writes, ~3.7k points of self-review). Repairs
        # need read/edit, so they get fresh sessions: per-mission transport.
        session_mode="per-mission" if mode == "combined" else mode,
    )
    supervisor = Supervisor(
        spec=spec,
        plan=plan,
        controller=context.controller,
        gate_active=context.gate_active,
        client=context.client,
        harness_dir=harness_dir,
        coverage_repair=coverage_repair_enabled(),
        stop_event=context.stop_event,
        app_dir=context.app_directory,
    )
    log(
        "harness",
        # The runner's own level, not the run's: missions mode forces thinking
        # off (PHASE3_DESIGN §9) whatever CHALLENGE_THINKING asked for.
        "missions · session-mode={0} · {1} journey(s) · thinking={2}".format(
            mode, len(plan.get("tests") or []), runner.thinking
        ),
    )
    narrate("Writing the app and its tests…")

    observation: Optional[Observation] = None
    try:
        if mode == "combined":
            runner.run(_mission("combined", combined_brief(spec, plan), tools=COMBINED_TOOLS))
        else:
            runner.run_parallel(
                [
                    _mission("builder", builder_brief(spec, plan)),
                    _mission("tester", tester_brief(spec, plan)),
                ]
            )
        _inject_fault(context)
        observation = _supervise(context, runner, supervisor, spec, plan)
    finally:
        # Every session is closed before the harness stops handling signals:
        # after this point a SIGTERM must find nothing of ours still running.
        runner.close()
        context.restore_signals()

    _write_final_report(context, spec, plan, observation)
    narrate(outcome_narration(context.signalled, observation))
    _write_json(harness_dir / "supervisor.json", supervisor.summary())
    _write_json(
        harness_dir / "missions.json",
        {
            "session_mode": mode,
            "sessions": runner.sessions(),
            "missions": [result.as_dict() for result in runner.results],
        },
    )
    return any(result.success for result in runner.results)


#: ``HARNESS_FAULT=tsc`` (PHASE3_DESIGN §8, measurement only): once the Builder
#: and Tester have settled, append a line that cannot typecheck to the config so
#: a real run exercises the tsc fast path and the Repairer inside the cap.
#: Never set in a judged run; it exists to measure the repair loop against the
#: real model rather than the fake Pi.
FAULT_ENV = "HARNESS_FAULT"
FAULT_LINE = '\nexport const harnessInjectedFault: number = "not a number";\n'


def _inject_fault(context: RunContext) -> None:
    fault = (os.environ.get(FAULT_ENV) or "").strip().lower()
    if not fault:
        return
    if fault != "tsc":
        warn("ignoring unknown {0}={1!r}".format(FAULT_ENV, fault))
        return
    target = context.app_directory / CONFIG_FILE
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(FAULT_LINE)
        log("harness", "fault injected: type error appended to {0} ({1}=tsc)".format(CONFIG_FILE, FAULT_ENV))
    except OSError as exc:
        warn("could not inject fault into {0}: {1}".format(target, exc))


def _supervise(
    context: RunContext,
    runner: MissionRunner,
    supervisor: Supervisor,
    spec: Dict[str, Any],
    plan: Dict[str, Any],
) -> Optional[Observation]:
    """observe → decide → (repair|rerun|build) until ``done``/``stop``.

    ``run_build`` is sticky once the Supervisor has asked for a build: rule 12
    turns a failed build into a repair, and the round after that repair has to
    try the build again or the loop could never reach ``done``.
    """
    seed_dir = context.repository_root / SEED_DIR_NAME
    observation: Optional[Observation] = None
    # The last observation that actually judged the app. A round whose tsc
    # could not spawn and whose vitest therefore found nothing must not be the
    # one the final report is composed from -- it would overwrite a complete
    # report with an empty one.
    reportable: Optional[Observation] = None
    run_build = False

    for round_number in range(1, MAX_ROUNDS + 1):
        if context.stop_event.is_set():
            log("harness", "missions · shutting down; no further observation")
            break
        budget = context.deadline - time.monotonic() - SHUTDOWN_RESERVE_S
        if budget < MIN_OBSERVE_S:
            warn("missions · {0:.0f}s left; stopping before the next observation".format(max(0.0, budget)))
            break

        cap = BUILD_OBSERVE_TIMEOUT_S if run_build else OBSERVE_TIMEOUT_S
        log(
            "harness",
            "missions · round {0}{1} · {2:.0f}s left".format(
                round_number, " (with build)" if run_build else "", budget
            ),
        )
        observation = observe_app(
            context.app_directory,
            context.harness_directory,
            seed_dir=seed_dir,
            spec=plan,
            run_build=run_build,
            timeout_s=min(cap, budget),
            stop_event=context.stop_event,
        )
        if observation_has_evidence(observation):
            reportable = observation
        if observation.green:
            # Written on every green round so a run killed on the deadline still
            # leaves a valid report. The status is the same function the final
            # write uses, so the two can never disagree: the build is what
            # separates "the tests pass" from "the app ships", and untested
            # journeys keep it `partial` either way.
            _write_report(context, spec, plan, observation, final_status(observation))

        decision = supervisor.decide(observation)
        # After the decision, not before it: what the round *means* to a viewer
        # is the finding plus what happens next, and the repair attempt number
        # only exists once the Supervisor has issued the repair.
        narrate(round_narration(observation, decision, supervisor))
        if decision.action in ("done", "stop"):
            break
        if decision.action == "build":
            run_build = True
            continue

        mission = _repair_mission(decision, spec, plan, observation, supervisor)
        if mission is None:
            break
        result = runner.run(mission)
        supervisor.record(decision, result)
        if result.skipped_reason:
            log("harness", "missions · {0} never ran ({1}); stopping".format(
                mission.role, result.skipped_reason
            ))
            break
    else:
        warn("missions · round cap reached ({0}); stopping".format(MAX_ROUNDS))

    if reportable is not None and reportable is not observation:
        warn("missions · last observation judged nothing; reporting the last one that did")
    return reportable if reportable is not None else observation


#: The combined mission can only write: no `read` to re-inspect its own
#: output, no `edit` to second-guess it. Its prefix therefore differs from a
#: repair session's (the tool list is part of the prompt), which costs one
#: cold prefix per repair -- the minority case -- against a self-review turn
#: or two in most runs.
COMBINED_TOOLS = "write"


def _mission(role: str, brief: str, tools: Optional[str] = None) -> MissionSpec:
    spec = MissionSpec(
        role=role,
        brief=brief,
        predicted_output=PREDICTED_OUTPUT_TOKENS.get(role, REPAIRER_PREDICTED_OUTPUT_TOKENS),
    )
    if tools is not None:
        spec.tools = tools
    return spec


def _repair_mission(
    decision: Any,
    spec: Dict[str, Any],
    plan: Dict[str, Any],
    observation: Optional[Observation],
    supervisor: Supervisor,
) -> Optional[MissionSpec]:
    """The mission a ``repair``/``rerun`` decision asks for, or ``None``."""
    if decision.action == "rerun":
        return _mission(
            decision.role, rerun_brief(decision.role, plan, spec, decision.rationale)
        )
    if decision.action == "repair":
        return _mission(
            "repairer",
            repair_brief(
                observation.as_dict() if observation is not None else {},
                plan,
                spec,
                # ``repairs`` was already incremented when the decision was
                # issued, so it is this repair's own 1-based attempt number.
                attempt=max(1, supervisor.repairs),
                hint=decision.brief_hint,
                cap=supervisor.repair_cap,
            ),
        )
    warn("missions · unknown decision action {0!r}; stopping".format(decision.action))
    return None


# -- the narration ----------------------------------------------------------
#
# Plain English for the demo recording, composed only from data the loop
# already holds (the Observation, the Decision, the Supervisor's counters). No
# model call, no extra I/O, no state of its own: these are pure functions whose
# only effect is the one stderr line ``narrate`` writes.


def _vitest_counts(observation: Optional[Observation]) -> Dict[str, int]:
    vitest = observation.vitest if observation is not None and isinstance(observation.vitest, dict) else {}
    return {
        "total": int(vitest.get("total") or 0),
        "failed": int(vitest.get("failed") or 0),
    }


def _finding(observation: Optional[Observation]) -> str:
    """What this round found, in the words a non-technical viewer would use."""
    if observation is None:
        return "Nothing could be checked"
    if observation.tsc_ran and not observation.tsc_ok:
        return "The code does not typecheck"
    counts = _vitest_counts(observation)
    if counts["failed"]:
        return "{0} of {1} tests {2}".format(
            counts["failed"], counts["total"], "fails" if counts["failed"] == 1 else "fail"
        )
    if observation.build_ran and observation.build_ok is False:
        return "The production build failed"
    if observation.build_ran and observation.build_ok:
        return "Production build passed"
    if observation.green and counts["total"]:
        return "All {0} tests pass".format(counts["total"]) if counts["total"] != 1 else "The one test passes"
    return "The tests did not run"


def _next_step(decision: Any, supervisor: Supervisor) -> str:
    """What the run does about it, appended to the finding."""
    action = getattr(decision, "action", "")
    if action == "repair":
        # ``repairs`` was incremented when the decision was issued, so it is
        # this repair's own 1-based attempt number -- the same number the
        # Repairer's brief carries.
        return " — repairing (attempt {0} of {1})".format(
            max(1, supervisor.repairs), supervisor.repair_cap
        )
    if action == "rerun":
        return " — asking the {0} to write it again".format(getattr(decision, "role", "") or "model")
    if action == "build":
        return " — trying a production build next"
    return ""


def round_narration(
    observation: Optional[Observation], decision: Any, supervisor: Supervisor
) -> str:
    """One line for one observe round: what was found, and what happens next."""
    return _finding(observation) + _next_step(decision, supervisor)


def _stop_reason(observation: Optional[Observation]) -> str:
    """Why a run that is not a clean success stops, without the jargon."""
    if observation is None:
        return "nothing could be checked"
    if observation.tsc_ran and not observation.tsc_ok:
        return "the code still does not typecheck"
    counts = _vitest_counts(observation)
    if counts["failed"]:
        return "{0} of {1} tests still {2}".format(
            counts["failed"], counts["total"], "fails" if counts["failed"] == 1 else "fail"
        )
    if observation.build_ran and observation.build_ok is False:
        return "the production build failed"
    if not counts["total"]:
        return "no tests ran"
    if not observation.build_ran:
        return "the production build was never checked"
    if ((observation.coverage or {}).get("missing") or []):
        return "some journeys have no test"
    return "the app is not finished"


def outcome_narration(signalled: List[str], observation: Optional[Observation]) -> str:
    """The last narration line of a missions run: how it ended, in plain words."""
    if signalled:
        return "Stopping: the run was told to shut down — no final report"
    status = final_status(observation)
    if status == "success":
        return "Done: the app builds and every test passes — report written"
    return "Stopping: {0} — report written as {1}".format(_stop_reason(observation), status)


# -- the report -------------------------------------------------------------


def report_spec(spec: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """What ``report.compose_report`` should read for a missions run.

    ``summary`` is the Analyst's (it is the only prose in the system written
    about *this* idea), but ``implemented_features`` and ``assumptions`` are the
    Architect's: the v2 spec no longer carries features, and the plan's
    assumptions already fold in the spec's own plus one line per omitted
    pattern and per named constant.
    """
    return {
        "summary": spec.get("summary") or spec.get("tagline") or "",
        "implemented_features": list(plan.get("implemented_features") or []),
        "assumptions": list(plan.get("assumptions") or []),
    }


def final_status(observation: Optional[Observation]) -> str:
    """§7.5: ``success`` iff green, built and every journey tested.

    Three distinctions the first cut got wrong, each measured:

    - ``tsc_ok`` false with ``tsc_ran`` false is *no* typecheck, not a red one
      (observe.py's own contract). A tsc that could not spawn or did not
      finish says nothing about the app, so it cannot make the report
      ``failed`` -- it makes it ``partial``, because nothing judged the types.
    - a build that never ran is not a failed build (``build_ok`` stays
      ``None``); only ``False`` is a real failure.
    - §7.5 counts incomplete coverage as ``partial``: a green run whose
      journeys were not all tested must not claim ``success`` for them.
    """
    if observation is None:
        return "failed"
    if observation.tsc_ran and not observation.tsc_ok:
        return "failed"
    if observation.build_ran and observation.build_ok is False:
        return "failed"
    if observation.green and observation.build_ok is True:
        missing = (observation.coverage or {}).get("missing") or []
        return "partial" if missing else "success"
    return "partial"


def observation_has_evidence(observation: Optional[Observation]) -> bool:
    """Did this observation actually judge the app at all?

    An observation where tsc could not spawn, vitest ran no test and no build
    was attempted is a statement about the tool-chain, not about the app.
    Composing the final report from one of those replaced a complete,
    ten-journey report with ``status: failed`` and ``tests_run: []``.
    """
    if observation is None:
        return False
    vitest = observation.vitest if isinstance(observation.vitest, dict) else {}
    return bool(
        observation.tsc_ran
        or observation.build_ran
        or int(vitest.get("total") or 0) > 0
        or vitest.get("failures")
    )


def _write_report(
    context: RunContext,
    spec: Dict[str, Any],
    plan: Dict[str, Any],
    observation: Optional[Observation],
    status: str,
) -> bool:
    vitest = observation.vitest if observation is not None else report_mod.empty_observation()
    written = report_mod.write_report(
        context.app_directory,
        report_spec(spec, plan),
        vitest,
        context.idea,
        harness_dir=context.harness_directory,
        status=status,
    )
    if written:
        log(
            "report",
            "report.partial.json · status={0} · {1} journey(s) reported".format(
                status, len(vitest.get("names") or []) + len(vitest.get("failures") or [])
            ),
        )
    return written


def _write_final_report(
    context: RunContext,
    spec: Dict[str, Any],
    plan: Dict[str, Any],
    observation: Optional[Observation],
) -> None:
    """The last word on the run.

    Written even when the app is red -- a ``partial``/``failed`` report that
    names the journeys that ran is worth more to the runner than no report at
    all -- but never when an OS signal is in flight: the runner escalates
    SIGTERM to SIGKILL after 5 s and this is one more write in that window.
    """
    if context.signalled:
        log("report", "final report skipped (signal received); shutdown must stay fast")
        return
    _write_report(context, spec, plan, observation, final_status(observation))


def _write_json(path: pathlib.Path, payload: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        warn("could not write {0}: {1}".format(path.name, exc))
