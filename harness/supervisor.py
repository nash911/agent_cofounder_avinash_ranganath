"""The Supervisor: a deterministic repair policy with a model second opinion.

PHASE3_DESIGN.md §6. Every round of the missions loop ends with one
``Observation`` (harness/observe.py) and one question: *what next?* The answer
is a :class:`Decision`, and the thirteen ordered rules in
:meth:`Supervisor._policy` answer it without a model call in every case the
harness can already see the answer to -- a red ``tsc``, a red vitest, an
over-long file, a green run whose production build has not been tried.

A model call is spent only where the deterministic policy has demonstrably run
out of ideas: a repair went by without changing the observation's ``signature``
at all. That is the one place a second opinion is worth ~400 output tokens, and
the only place in the system where thinking is ever turned on -- and only on
the *second* identical model answer, because a model repeating itself with
thinking off is the one piece of evidence that the cheap mode has nothing left.

The optional :class:`Reviewer` (``HARNESS_REVIEWER=1``) is off until measured:
it costs a call after the build is already green, so it has to earn its place
against §8's numbers rather than against an intuition.

Nothing here writes stdout; nothing here blocks except the two bounded gateway
calls, whose deadline comes out of the run's own budget.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .budget import BudgetController
from .log import log, warn

#: Predicted output tokens for one Repairer mission (PHASE3_DESIGN §5's
#: ``REPAIRER 1200``). Duplicated rather than imported from ``harness.missions``
#: so the budget gate here does not depend on that module being importable.
REPAIRER_PREDICTED_OUTPUT_TOKENS = 1200

MODEL_MAX_TOKENS = 400
MODEL_THINKING_TOKEN_BUDGET = 1024
REVIEWER_MAX_TOKENS = 700

#: Wall-clock ceiling for one direct call from here, and what it leaves behind
#: for shutdown. A stalled second opinion must never eat the repair it informs.
MODEL_CALL_MAX_S = 30.0
MODEL_CALL_RESERVE_S = 30.0

#: How much of each generated file the model Supervisor is shown (§6).
FILE_HEAD_LINES = 150

#: Hint caps, mirroring §3's repair-brief limits so ``plan.repair_brief`` never
#: has to truncate what it is handed.
MAX_TSC_LINES = 30
MAX_FAILURES_IN_HINT = 6
MAX_FAILURE_CHARS = 600
MAX_BUILD_TAIL_CHARS = 1500
MAX_HINT_IN_SUMMARY = 400
MAX_JOURNEYS_IN_PROMPT = 12

DEFAULT_CONFIG_PATH = "src/app-config.ts"
DEFAULT_TESTS_PATH = "src/journeys.test.tsx"

#: The model Supervisor's four verbs, mapped to a policy action and the hint
#: prefix that tells ``repair_brief`` which file to point the Repairer at.
_MODEL_ACTIONS = {
    "repair_config": ("repair", "Fix {config} only."),
    "repair_tests": ("repair", "Fix {tests} only."),
    "rewrite_tests": ("repair", "Rewrite {tests} from scratch in one write."),
    "stop": ("stop", ""),
}

MODEL_SYSTEM_PROMPT = (
    "You supervise a two-file code repair loop. One deterministic repair has already left the "
    "failure signature unchanged. Choose the single next action and write a brief the repairing "
    "agent can act on without asking questions. No commentary."
)

MODEL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["repair_config", "repair_tests", "rewrite_tests", "stop"],
            "description": "The next action; stop when no edit can plausibly help.",
        },
        "brief": {"type": "string", "description": "What to change, concretely, in <= 80 words."},
        "rationale": {"type": "string", "description": "Why, in one sentence."},
    },
    "required": ["action", "brief", "rationale"],
    "additionalProperties": False,
}

REVIEWER_SYSTEM_PROMPT = (
    "You review a generated app config and its journey tests against the journeys they must "
    "satisfy. Report only defects a user or a grader would notice. No commentary."
)

_FINDING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "file": {"type": "string"},
        "problem": {"type": "string"},
        "fix": {"type": "string"},
    },
    "required": ["severity", "file", "problem", "fix"],
    "additionalProperties": False,
}

REVIEWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": _FINDING_SCHEMA,
            "description": "Defects found; empty when the pair is faithful to the journeys.",
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


@dataclass
class Decision:
    """One answer to "what next?", and where it came from."""

    action: str  # "repair" | "rerun" | "build" | "done" | "stop"
    role: str  # "builder" | "tester" | "repairer" | ""
    brief_hint: str  # what the brief should emphasise (plan.repair_brief renders it)
    rationale: str
    source: str  # "policy" | "model" | "model-thinking"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "role": self.role,
            "brief_hint": self.brief_hint,
            "rationale": self.rationale,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def reviewer_enabled() -> bool:
    """``HARNESS_REVIEWER=1`` only -- default off until §8 measures its worth."""
    return os.environ.get("HARNESS_REVIEWER", "").strip() == "1"


def high_findings(findings: Any) -> List[Dict[str, Any]]:
    """The ``high`` entries of a Reviewer result; anything malformed is ignored."""
    return [
        f
        for f in (findings or [])
        if isinstance(f, dict) and str(f.get("severity") or "").lower() == "high"
    ]


def _gate_reason(
    controller: BudgetController, predicted_output_tokens: int, accept_partial: bool = False
) -> Optional[str]:
    """``None`` when a repair mission may start, else the refusal reason.

    A local copy of ``harness.__main__.budget_gate_reason`` on purpose: under
    ``python3 -m harness`` the orchestrator is loaded as ``__main__``, so
    importing it by its package name -- even lazily, inside a method -- would
    execute a *second* copy of that module. Two lines are cheaper, and
    ``test_supervisor.py`` asserts the two wrappers agree.
    """
    ok, reason = controller.can_start(predicted_output_tokens, accept_partial=accept_partial)
    return None if ok else reason


def _field(obs: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an ``Observation`` dataclass **or** a plain mapping.

    The policy is written against the field names PHASE3_DESIGN §4 fixes, not
    against ``harness.observe``'s class: the live loop passes the dataclass, a
    replay of ``observe-<n>.json`` passes the same fields as a dict, and both
    must decide identically.
    """
    value = obs.get(name, default) if isinstance(obs, dict) else getattr(obs, name, default)
    return default if value is None else value


def _lines(value: Any) -> List[str]:
    return [str(item) for item in (value or []) if str(item).strip()]


def _mapping(obs: Any, name: str) -> Dict[str, Any]:
    value = _field(obs, name, {})
    return value if isinstance(value, dict) else {}


def _journey_lines(spec: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """The journeys as ``- title: expectation`` bullets (spec first, plan as fallback)."""
    journeys = spec.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        journeys = plan.get("tests") if isinstance(plan.get("tests"), list) else []
    out: List[str] = []
    for journey in journeys[:MAX_JOURNEYS_IN_PROMPT]:
        if not isinstance(journey, dict):
            continue
        title = str(journey.get("title") or "").strip()
        if not title:
            continue
        expect = str(journey.get("expect") or "").strip()
        out.append("- {0}{1}".format(title, ": " + expect if expect else ""))
    return "\n".join(out) if out else "- (no journeys in the spec)"


def _file_text(app_dir: Optional[pathlib.Path], relative: str, limit: Optional[int] = None) -> str:
    """A generated file's text (optionally its first ``limit`` lines), or a marker."""
    if app_dir is None:
        return "(file contents unavailable)"
    try:
        text = (app_dir / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(file missing)"
    return text if limit is None else "\n".join(text.splitlines()[:limit])


def _render_findings(findings: List[Dict[str, Any]]) -> str:
    return "\n".join(
        "- [{0}] {1}: {2} -> {3}".format(
            f.get("severity"), f.get("file") or "?", f.get("problem"), f.get("fix")
        )
        for f in findings
    )


def _mission_record(result: Any) -> Optional[Dict[str, Any]]:
    """A JSON-safe digest of a ``MissionResult`` (duck-typed: dataclass or dict)."""
    if result is None:
        return None

    def get(name: str, default: Any = None) -> Any:
        return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)

    return {
        "role": str(get("role", "") or ""),
        "label": str(get("label", "") or ""),
        "settled": bool(get("settled", False)),
        "success": bool(get("success", False)),
        "interrupted": bool(get("interrupted", False)),
        "timed_out": bool(get("timed_out", False)),
        "error": get("error"),
        "stop_reason": get("stop_reason"),
        "output_tokens": int(get("output_tokens", 0) or 0),
        "wall_s": round(float(get("wall_s", 0.0) or 0.0), 3),
        "resume_attempts": int(get("resume_attempts", 0) or 0),
        "skipped_reason": get("skipped_reason"),
    }


class Supervisor:
    """Owns the repair policy, the caps, and the decision history for one run."""

    def __init__(
        self,
        *,
        spec: Optional[Dict[str, Any]],
        plan: Optional[Dict[str, Any]],
        controller: Optional[BudgetController],
        gate_active: bool,
        client: Any = None,
        repair_cap: int = 3,
        no_progress_cap: int = 2,
        harness_dir: Any = None,
        coverage_repair: bool = False,
        stop_event: Any = None,
        app_dir: Any = None,
    ) -> None:
        self.spec: Dict[str, Any] = spec if isinstance(spec, dict) else {}
        self.plan: Dict[str, Any] = plan if isinstance(plan, dict) else {}
        self.controller = controller
        self.gate_active = bool(gate_active)
        self.client = client
        self.repair_cap = int(repair_cap)
        self.no_progress_cap = int(no_progress_cap)
        self.harness_dir = pathlib.Path(harness_dir) if harness_dir is not None else None
        self.coverage_repair = bool(coverage_repair)
        self.stop_event = stop_event
        self.app_dir = pathlib.Path(app_dir) if app_dir is not None else None

        files = self.plan.get("files")
        files = files if isinstance(files, dict) else {}
        self.config_path = str(files.get("config") or DEFAULT_CONFIG_PATH)
        self.tests_path = str(files.get("tests") or DEFAULT_TESTS_PATH)

        self.repairs = 0
        self.reruns: Dict[str, int] = {"builder": 0, "tester": 0}
        self.no_progress = 0
        #: How many times rule 11 has asked for a production build (the latch)
        #: and how many observations arrived with no typecheck at all (8a).
        self.builds = 0
        self.tsc_skipped = 0
        self.model_calls = 0
        self.model_thinking_calls = 0
        self.review_ran = False
        self.review_findings: List[Dict[str, Any]] = []

        self._history: List[Dict[str, Any]] = []
        self._observations: List[Dict[str, Any]] = []
        self._last_signature: Optional[str] = None
        self._last_action: Optional[str] = None
        self._last_model_action: Optional[str] = None
        self._thinking_next = False

    # -- public API --------------------------------------------------------

    def decide(self, observation: Any) -> Decision:
        """The next action for ``observation``; never raises, always answers."""
        try:
            decision = self._policy(observation)
        except Exception as exc:  # noqa: BLE001 -- a supervisor crash must not end the run
            warn("supervisor · policy failed ({0}: {1}); stopping".format(type(exc).__name__, exc))
            decision = Decision("stop", "", "", "supervisor error: {0}".format(exc), "policy")
        self._last_action = decision.action
        self._history.append(self._entry(decision))
        log(
            "harness",
            "supervisor · {0}{1} [{2}] {3}".format(
                decision.action,
                " " + decision.role if decision.role else "",
                decision.source,
                decision.rationale,
            ),
        )
        return decision

    def record(self, decision: Decision, result: Any = None) -> None:
        """Attach the mission ``result`` (a ``MissionResult`` or ``None``) to ``decision``."""
        entry = None
        for candidate in reversed(self._history):
            if (
                candidate["action"] == decision.action
                and candidate["role"] == decision.role
                and candidate["mission"] is None
            ):
                entry = candidate
                break
        if entry is None:
            entry = self._entry(decision)
            self._history.append(entry)
        entry["mission"] = _mission_record(result)

    def summary(self) -> Dict[str, Any]:
        """The JSON body of ``harness/supervisor.json`` -- what the loop did, and why."""
        final = self._history[-1] if self._history else None
        return {
            "decisions": [dict(entry) for entry in self._history],
            "observations": [dict(entry) for entry in self._observations],
            "repairs": self.repairs,
            "repair_cap": self.repair_cap,
            "reruns": dict(self.reruns),
            "no_progress": self.no_progress,
            "no_progress_cap": self.no_progress_cap,
            "builds": self.builds,
            "tsc_skipped": self.tsc_skipped,
            "model_calls": self.model_calls,
            "model_thinking_calls": self.model_thinking_calls,
            "coverage_repair": self.coverage_repair,
            "review": {
                "enabled": reviewer_enabled(),
                "ran": self.review_ran,
                "findings": [dict(f) for f in self.review_findings],
            },
            "final_action": final["action"] if final else "",
            "final_rationale": final["rationale"] if final else "",
        }

    # -- the policy (PHASE3_DESIGN §6, first match wins) --------------------

    def _needs_repair(self, obs: Any) -> bool:
        """Whether this observation would make rules 7-9 issue a repair.

        Mirrors the three repair triggers exactly: an over-limit file, a
        typecheck that RAN and was red, or a vitest run that is not green. A
        typecheck that never ran is not a repair situation (rule 8a degrades
        instead), and a green observation needs nothing. This is what gates the
        hard caps: they must not stop a run whose app is already green.
        """
        if _lines(_field(obs, "over_limit", [])):
            return True
        if _field(obs, "tsc_ran", False) and not _field(obs, "tsc_ok", False):
            return True
        vitest = _mapping(obs, "vitest")
        if vitest and not vitest.get("green"):
            return True
        return False

    def _policy(self, obs: Any) -> Decision:
        self._note_observation(obs)

        # 1 -- shutdown beats everything: no new mission once SIGTERM landed.
        if self.stop_event is not None and self.stop_event.is_set():
            return self._stop("shutdown requested")

        # 2/3 -- a mission that wrote nothing gets exactly one more chance.
        if self._config_unchanged(obs) and self.reruns["builder"] == 0:
            return self._rerun(
                "builder",
                "{0} is still the seed file. Write the whole file in one write.".format(self.config_path),
                "{0} unchanged from the seed".format(self.config_path),
            )
        if self._tests_missing(obs) and self.reruns["tester"] == 0:
            return self._rerun(
                "tester",
                "{0} does not exist. Write the whole file in one write.".format(self.tests_path),
                "{0} missing".format(self.tests_path),
            )

        # 4/5/6 -- the caps and the model escalation only apply while the app is
        # still broken. A green observation reached ON the cap-th repair must be
        # built and finished, never thrown away as `partial` (measured
        # 2026-09-04, jobhunt: the 3rd repair turned the app green with 11/11
        # tests, but the cap stopped the run before the build branch and the run
        # reported `partial`). The caps stop us starting ANOTHER repair on a
        # still-broken app; they never reject one that just went green.
        if self._needs_repair(obs):
            # 4/5 -- the two hard caps.
            if self.repairs >= self.repair_cap:
                return self._stop("repair cap reached ({0})".format(self.repair_cap))
            if self.no_progress >= self.no_progress_cap:
                return self._stop(
                    "no progress: signature unchanged after {0} repairs".format(self.no_progress)
                )

            # 6 -- one wasted repair and a gateway: buy a second opinion.
            if self.no_progress >= 1 and self.client is not None:
                model_decision = self._ask_model(obs)
                if model_decision is not None:
                    return self._issue(model_decision)

        # 7 -- an over-long file fails the runner's own check before anything else.
        over_limit = _lines(_field(obs, "over_limit", []))
        if over_limit:
            names = ", ".join(over_limit)
            return self._repair(
                "Over the 150-line limit: {0}. Shorten the named file(s) without dropping "
                "behaviour.".format(names),
                "files over the line limit: {0}".format(names),
            )

        # 8 -- tsc red: the fast path. The hint is the compiler's lines, nothing else.
        vitest = _mapping(obs, "vitest")
        if _field(obs, "tsc_ran", False):
            if not _field(obs, "tsc_ok", False):
                return self._repair(self._tsc_hint(obs), "tsc is red")
        else:
            # 8a -- observe.py's contract: a spawn failure or a timeout is *no*
            # typecheck, not a red one. There are no errors for a Repairer to
            # fix, so the run degrades instead of repairing: vitest and the
            # build get the last word, and the report can only say `partial`
            # (loop.final_status keeps `success` behind a green typecheck).
            # Only when nothing ran at all is there nothing left to decide on.
            detail = (_lines(_field(obs, "tsc_errors", [])) or ["reason unknown"])[0]
            if int(vitest.get("total") or 0) <= 0:
                return self._stop("typecheck did not run and no test ran: {0}".format(detail))
            self.tsc_skipped += 1
            if self.tsc_skipped == 1:
                warn("supervisor · typecheck did not run ({0}); the tests decide".format(detail))

        # 9 -- vitest red.
        if not vitest.get("green"):
            return self._repair(
                self._vitest_hint(vitest),
                "vitest is red ({0} failing)".format(int(vitest.get("failed") or 0)),
            )

        # Rules 10-13 are the green branch. Green is derived from the three
        # checks above rather than read off the observation, so a hand-built or
        # replayed observation can never fall off the end of the policy.
        missing = _lines(_mapping(obs, "coverage").get("missing"))

        # 10 -- coverage, only behind the flag (informational otherwise).
        if missing and self.coverage_repair:
            return self._repair(
                "These journeys have no test: {0}. Add one `it` per missing journey, using the "
                "title verbatim.".format("; ".join(missing)),
                "{0} journey(s) untested".format(len(missing)),
            )

        # 11 -- green but the production build has not been tried. Asked for
        # once only: a `vite build` that cannot spawn (or has no budget left)
        # reports `build_ran=False` again next round, and without the latch the
        # policy re-issues `build` forever -- measured at 12 rounds of a full
        # tsc + vitest pair each, ending on the loop's own round cap.
        if not _field(obs, "build_ran", False):
            if self.builds == 0:
                self.builds += 1
                return Decision("build", "", "", "green; production build not run yet", "policy")
            return Decision(
                "done", "", "", "green; the production build could not run", "policy"
            )

        # 12 -- the build failed: a real defect, counted like any other repair.
        if _field(obs, "build_ok", None) is not True:
            tail = str(_field(obs, "build_tail", "") or "")[-MAX_BUILD_TAIL_CHARS:]
            return self._repair(
                "`vite build` failed. Last output:\n{0}".format(tail), "production build failed"
            )

        # 13 -- green and built. The Reviewer, when enabled, gets the last word.
        review_decision = self._maybe_review()
        if review_decision is not None:
            return self._issue(review_decision)
        return Decision("done", "", "", "green and the production build passed", "policy")

    # -- decision constructors ---------------------------------------------

    def _stop(self, rationale: str) -> Decision:
        return Decision("stop", "", "", rationale, "policy")

    def _repair(self, hint: str, rationale: str) -> Decision:
        return self._issue(Decision("repair", "repairer", hint, rationale, "policy"))

    def _rerun(self, role: str, hint: str, rationale: str) -> Decision:
        return self._issue(Decision("rerun", role, hint, rationale, "policy"))

    def _issue(self, decision: Decision) -> Decision:
        """Budget-gate a mission-spending decision and count it."""
        if decision.action not in ("repair", "rerun"):
            return decision
        refusal = self._gate_refusal()
        if refusal is not None:
            return Decision(
                "stop",
                "",
                "",
                "budget refused a {0}: {1}".format(decision.action, refusal),
                decision.source,
            )
        if decision.action == "repair":
            self.repairs += 1
        elif decision.role in self.reruns:
            self.reruns[decision.role] += 1
        return decision

    def _gate_refusal(self) -> Optional[str]:
        if not self.gate_active or self.controller is None:
            return None
        return _gate_reason(self.controller, REPAIRER_PREDICTED_OUTPUT_TOKENS, False)

    # -- bookkeeping -------------------------------------------------------

    def _entry(self, decision: Decision) -> Dict[str, Any]:
        return {
            "n": len(self._history) + 1,
            "action": decision.action,
            "role": decision.role,
            "source": decision.source,
            "rationale": decision.rationale,
            "hint": decision.brief_hint[:MAX_HINT_IN_SUMMARY],
            "signature": self._last_signature or "",
            "mission": None,
        }

    def _note_observation(self, obs: Any) -> None:
        """Fold one observation into the history and update the no-progress streak.

        Only a *repair* can waste a round: a rerun writes a file that was never
        written, and a build decision does not touch the code at all, so
        neither resets nor increments the streak.
        """
        signature = str(_field(obs, "signature", "") or "")
        if self._last_action == "repair":
            same = self._last_signature is not None and signature == self._last_signature
            self.no_progress = self.no_progress + 1 if same else 0
        self._last_signature = signature

        vitest = _mapping(obs, "vitest")
        self._observations.append(
            {
                "n": len(self._observations) + 1,
                "signature": signature,
                "green": bool(_field(obs, "green", False)),
                "tsc_ok": bool(_field(obs, "tsc_ok", False)),
                "tsc_errors": _lines(_field(obs, "tsc_errors", []))[:5],
                "vitest": {
                    "green": bool(vitest.get("green")),
                    "passed": int(vitest.get("passed") or 0),
                    "failed": int(vitest.get("failed") or 0),
                    "failures": [
                        str(f.get("name") or "")
                        for f in (vitest.get("failures") or [])
                        if isinstance(f, dict)
                    ][:MAX_FAILURES_IN_HINT],
                },
                "build_ran": bool(_field(obs, "build_ran", False)),
                "build_ok": _field(obs, "build_ok", None),
                "over_limit": _lines(_field(obs, "over_limit", [])),
                "coverage_missing": _lines(_mapping(obs, "coverage").get("missing")),
                "elapsed_s": round(float(_field(obs, "elapsed_s", 0.0) or 0.0), 3),
            }
        )

    def _file_entry(self, obs: Any, path: str) -> Optional[Dict[str, Any]]:
        entry = _mapping(obs, "files").get(path)
        return entry if isinstance(entry, dict) else None

    def _config_unchanged(self, obs: Any) -> bool:
        entry = self._file_entry(obs, self.config_path)
        # No information about the file at all -> the rule cannot fire.
        return entry is not None and not entry.get("changed_from_seed")

    def _tests_missing(self, obs: Any) -> bool:
        entry = self._file_entry(obs, self.tests_path)
        return entry is not None and not entry.get("exists")

    # -- hints -------------------------------------------------------------

    def _tsc_hint(self, obs: Any) -> str:
        lines = _lines(_field(obs, "tsc_errors", []))[:MAX_TSC_LINES]
        if not lines:
            lines = ["tsc failed without reporting a parsable error line"]
        return "TypeScript errors from `tsc --noEmit`, fix exactly these:\n{0}".format("\n".join(lines))

    def _vitest_hint(self, vitest: Dict[str, Any]) -> str:
        failures = [f for f in (vitest.get("failures") or []) if isinstance(f, dict)]
        if not failures:
            return (
                "vitest reported no passing tests and no failure detail. The test file must "
                "contain at least one passing `it`."
            )
        rendered = "\n".join(
            "- {0}\n  {1}".format(
                str(f.get("name") or "").strip(),
                str(f.get("message") or "").strip()[:MAX_FAILURE_CHARS],
            )
            for f in failures[:MAX_FAILURES_IN_HINT]
        )
        return "Failing journey tests, with the first assertion message:\n{0}".format(rendered)

    # -- the model Supervisor ----------------------------------------------

    def _ask_model(self, obs: Any) -> Optional[Decision]:
        """One ``json_schema`` call for a second opinion; ``None`` falls through.

        Thinking is on only when the previous model answer chose the same action
        as the one before it -- see the module docstring for why that is the
        only evidence worth 1,024 thinking tokens.
        """
        thinking = self._thinking_next
        try:
            self.model_calls += 1
            if thinking:
                self.model_thinking_calls += 1
            obj, result = self.client.json_schema(
                [
                    {"role": "system", "content": MODEL_SYSTEM_PROMPT},
                    {"role": "user", "content": self._model_user_message(obs)},
                ],
                name="supervisor_decision",
                schema=MODEL_SCHEMA,
                label="supervisor-thinking" if thinking else "supervisor",
                max_tokens=MODEL_MAX_TOKENS,
                temperature=0,
                thinking=thinking,
                thinking_token_budget=MODEL_THINKING_TOKEN_BUDGET if thinking else None,
                deadline=self._call_deadline(),
            )
        except Exception as exc:  # noqa: BLE001 -- the policy is always the fallback
            warn("supervisor · model call failed ({0}: {1})".format(type(exc).__name__, exc))
            return None

        if not isinstance(obj, dict):
            warn(
                "supervisor · no usable model decision (status {0}, error {1})".format(
                    getattr(result, "status", "?"), getattr(result, "error", None)
                )
            )
            return None
        raw_action = obj.get("action")
        brief = obj.get("brief")
        if raw_action not in _MODEL_ACTIONS or not isinstance(brief, str):
            warn("supervisor · model decision rejected: {0!r}".format(obj)[:300])
            return None

        # Only a decision we actually accept counts as "the previous decision".
        self._thinking_next = raw_action == self._last_model_action
        self._last_model_action = raw_action

        action, prefix = _MODEL_ACTIONS[raw_action]
        rationale = "model supervisor: {0}".format(
            str(obj.get("rationale") or "").strip() or "no rationale given"
        )
        source = "model-thinking" if thinking else "model"
        if action == "stop":
            return Decision("stop", "", "", rationale, source)
        hint = prefix.format(config=self.config_path, tests=self.tests_path)
        return Decision(
            "repair", "repairer", "{0} {1}".format(hint, brief.strip()).strip(), rationale, source
        )

    def _model_user_message(self, obs: Any) -> str:
        head = "## {0} (first {1} lines)"
        parts = [
            "## Journeys the app must satisfy",
            _journey_lines(self.spec, self.plan),
            head.format(self.config_path, FILE_HEAD_LINES),
            _file_text(self.app_dir, self.config_path, FILE_HEAD_LINES),
            head.format(self.tests_path, FILE_HEAD_LINES),
            _file_text(self.app_dir, self.tests_path, FILE_HEAD_LINES),
            "## The last two observations",
        ]
        parts.extend(json.dumps(entry, ensure_ascii=False) for entry in self._observations[-2:])
        parts.append("## Decisions so far")
        parts.extend(
            "- {0} {1} ({2}): {3}".format(
                entry["action"], entry["role"] or "-", entry["source"], entry["rationale"]
            )
            for entry in self._history[-6:]
        )
        if not self._history:
            parts.append("- (none)")
        parts.append("Pick one action. The two files above are the only files that may change.")
        return "\n".join(part for part in parts if part)

    def _call_deadline(self) -> float:
        """A bounded deadline for one direct call, inside the run's own budget."""
        now = time.monotonic()
        cap = now + MODEL_CALL_MAX_S
        if self.controller is not None:
            cap = min(cap, self.controller.deadline_monotonic - MODEL_CALL_RESERVE_S)
        return max(now + 1.0, cap)

    # -- the Reviewer ------------------------------------------------------

    def _maybe_review(self) -> Optional[Decision]:
        """One review, at most, after the first green build. ``None`` means "done"."""
        if self.review_ran or self.client is None or not reviewer_enabled():
            return None
        self.review_ran = True
        reviewer = Reviewer(
            client=self.client,
            spec=self.spec,
            plan=self.plan,
            app_dir=self.app_dir,
            harness_dir=self.harness_dir,
        )
        self.review_findings = reviewer.review(deadline=self._call_deadline())
        highs = high_findings(self.review_findings)
        if not highs:
            return None
        return Decision(
            "repair",
            "repairer",
            "The reviewer found {0} high-severity problem(s):\n{1}".format(
                len(highs), _render_findings(highs)
            ),
            "reviewer: {0} high finding(s)".format(len(highs)),
            "model",
        )


class Reviewer:
    """The optional post-build review (``HARNESS_REVIEWER=1``).

    Separate from :class:`Supervisor` because it answers a different question:
    not "what is broken?" (the observation already says) but "is this pair
    faithful to the journeys?" -- which only a model can see. Never raises; a
    failed review is simply no findings, and ``review.json`` is written either
    way so §8 can compare runs with and without it.
    """

    def __init__(
        self,
        *,
        client: Any,
        spec: Optional[Dict[str, Any]] = None,
        plan: Optional[Dict[str, Any]] = None,
        app_dir: Any = None,
        harness_dir: Any = None,
        max_tokens: int = REVIEWER_MAX_TOKENS,
    ) -> None:
        self.client = client
        self.spec: Dict[str, Any] = spec if isinstance(spec, dict) else {}
        self.plan: Dict[str, Any] = plan if isinstance(plan, dict) else {}
        self.app_dir = pathlib.Path(app_dir) if app_dir is not None else None
        self.harness_dir = pathlib.Path(harness_dir) if harness_dir is not None else None
        self.max_tokens = int(max_tokens)

        files = self.plan.get("files")
        files = files if isinstance(files, dict) else {}
        self.config_path = str(files.get("config") or DEFAULT_CONFIG_PATH)
        self.tests_path = str(files.get("tests") or DEFAULT_TESTS_PATH)

    def review(self, deadline: Optional[float] = None) -> List[Dict[str, Any]]:
        """The findings, ``[]`` on any failure. Writes ``<harness_dir>/review.json``."""
        obj: Any = None
        result: Any = None
        try:
            obj, result = self.client.json_schema(
                [
                    {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                    {"role": "user", "content": self._bundle()},
                ],
                name="review",
                schema=REVIEWER_SCHEMA,
                label="reviewer",
                max_tokens=self.max_tokens,
                temperature=0,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 -- a review must never end the run
            warn("reviewer · call failed ({0}: {1})".format(type(exc).__name__, exc))

        findings: List[Dict[str, Any]] = []
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            for finding in obj["findings"]:
                if isinstance(finding, dict) and finding.get("severity") and finding.get("problem"):
                    findings.append(
                        {
                            "severity": str(finding.get("severity")).lower(),
                            "file": str(finding.get("file") or ""),
                            "problem": str(finding.get("problem") or ""),
                            "fix": str(finding.get("fix") or ""),
                        }
                    )
        elif obj is not None:
            warn("reviewer · unusable reply: {0!r}".format(obj)[:200])
        else:
            warn("reviewer · no findings (status {0})".format(getattr(result, "status", "?")))

        self._write(findings)
        log("harness", "reviewer · {0} finding(s)".format(len(findings)))
        return findings

    def _bundle(self) -> str:
        parts = ["## Journeys", _journey_lines(self.spec, self.plan)]
        for path in (self.config_path, self.tests_path):
            parts.append("## {0}".format(path))
            parts.append(_file_text(self.app_dir, path))
        parts.append(
            "Report only problems that would break a journey or show the user a wrong string. "
            "Severity high means a journey is wrong or missing."
        )
        return "\n".join(parts)

    def _write(self, findings: List[Dict[str, Any]]) -> None:
        if self.harness_dir is None:
            return
        try:
            self.harness_dir.mkdir(parents=True, exist_ok=True)
            (self.harness_dir / "review.json").write_text(
                json.dumps({"findings": findings}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            warn("reviewer · could not write review.json: {0}".format(exc))
