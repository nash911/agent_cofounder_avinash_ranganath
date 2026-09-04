"""The prompt material a mission session is given, checked against its sources.

Missions mode buys its cost reduction by *carrying* what the Phase 2 session
read: ~10k of that run's 26k points were file reads of ``AGENTS.md``,
``src/app-config.ts``, ``src/test/journeys.template.tsx``,
``src/lib/config-types.ts`` and ``src/test/helpers.tsx`` (PHASE3_DESIGN.md §0).
Two of those files are now reproduced inside ``AGENTS.md`` verbatim, which only
works while they *stay* verbatim -- a scaffold edit that does not reach the
embedded copy would hand every mission a stale API and cost a repair round to
discover. These tests are that pin.

The second half pins what the missions prefix must *not* say. It is a different
prompt from ``solution/system-prompt.md``: no report to write, no commands to
run, no ``--agent pi`` epilogue. Every one of those instructions costs a call
the harness now makes for free, and one of them (``npm test``) is the single
most expensive line in the Phase 2 prompt.
"""

from __future__ import annotations

import pathlib
import unittest
from typing import List

from harness import loop
from harness.tests import support

REPO_ROOT = support.REPO_ROOT
AGENTS_MD = REPO_ROOT / "app-template" / "AGENTS.md"
SEED_CONFIG = REPO_ROOT / "app-template" / "src" / "app-config.ts"
JOURNEY_TEMPLATE = REPO_ROOT / "app-template" / "src" / "test" / "journeys.template.tsx"
MISSIONS_PROMPT = REPO_ROOT / "solution" / "system-prompt.missions.md"
SYSTEM_PROMPT = REPO_ROOT / "solution" / "system-prompt.md"
JOURNEYS_MD = REPO_ROOT / "contract-public" / "journeys.md"


def _fenced_block(text: str, heading: str, language: str) -> str:
    """The one fenced ``language`` block that follows ``heading``.

    Deliberately literal: it walks the lines rather than using a regex, so a
    stray fence inside the block would fail loudly instead of matching a
    shorter, wrong span.
    """
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        raise AssertionError("heading not found in AGENTS.md: {0}".format(heading))
    opener = "```" + language
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("```"):
            if lines[index] != opener:
                raise AssertionError(
                    "first fence after {0!r} is {1!r}, not {2!r}".format(
                        heading, lines[index], opener
                    )
                )
            for end in range(index + 1, len(lines)):
                if lines[end] == "```":
                    return "\n".join(lines[index + 1 : end])
            raise AssertionError("unterminated {0} fence after {1!r}".format(language, heading))
    raise AssertionError("no fenced block after {0!r}".format(heading))


def _prose(text: str) -> str:
    """The prompt with its fenced blocks removed.

    The embedded seed config is verbatim scaffold source and its docblock
    mentions ``report.partial.json``; only the *instructions* around it are
    what a mission is told to do, so the fences are dropped before checking
    what the prompt asks for.
    """
    kept: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return "\n".join(kept)


class EmbeddedScaffoldTest(unittest.TestCase):
    """``AGENTS.md`` reproduces the two files no mission may need to read."""

    def setUp(self):
        self.agents = AGENTS_MD.read_text(encoding="utf-8")

    def test_worked_example_is_the_seed_config_byte_for_byte(self):
        embedded = _fenced_block(self.agents, "## Worked example: `src/app-config.ts`", "ts")
        self.assertEqual(embedded, SEED_CONFIG.read_text(encoding="utf-8").rstrip("\n"))

    def test_journey_template_block_is_the_template_byte_for_byte(self):
        embedded = _fenced_block(self.agents, "## Journey test template", "tsx")
        self.assertEqual(embedded, JOURNEY_TEMPLATE.read_text(encoding="utf-8").rstrip("\n"))

    def test_exactly_one_generated_application_contract_heading(self):
        # ``test/run-challenge.test.ts`` asserts the same thing about the
        # composed Pi prompt; the appendix must not add a second H1 (or an H2
        # spelled the same way, which that test rejects too).
        lines = self.agents.splitlines()
        self.assertEqual(lines.count("# Generated application contract"), 1)
        self.assertEqual(lines.count("## Generated application contract"), 0)

    def test_no_mission_is_told_to_read_the_files_that_are_now_inline(self):
        # The Phase 2 wording ("read it first") is what the embedded blocks
        # replace; leaving it in would buy the read back at full price.
        self.assertNotIn("read it first", self.agents)
        self.assertIn("reproduced below", self.agents)


class MissionsPromptTest(unittest.TestCase):
    """``solution/system-prompt.missions.md`` is mission-agnostic and cheap."""

    def setUp(self):
        self.prompt = MISSIONS_PROMPT.read_text(encoding="utf-8")

    def test_the_missions_prompt_exists_and_is_short(self):
        # ~40 lines (§7). It is paid for once per session and cached after the
        # first request answers, but it is also the floor on every session's
        # input, so it stays small on purpose.
        self.assertLessEqual(len(self.prompt.splitlines()), 60)
        self.assertTrue(self.prompt.strip())

    def test_it_never_asks_for_a_command_a_report_or_a_process_probe(self):
        for forbidden in ("npm test", "npm run build", "report.partial.json", "pgrep", "result.json"):
            self.assertNotIn(forbidden, self.prompt, forbidden)

    def test_it_is_not_the_agent_pi_control_prompt(self):
        # ``solution/system-prompt.md`` is pinned by test/run-challenge.test.ts
        # and must stay the --agent pi control's prompt, epilogue and all.
        epilogue = "Finish the moment"
        self.assertIn(epilogue, SYSTEM_PROMPT.read_text(encoding="utf-8"))
        self.assertNotIn(epilogue, self.prompt)

    def test_it_tells_the_agent_its_mission_is_the_user_message(self):
        self.assertIn("Your mission is the first user message", self.prompt)
        self.assertIn("Never run a command", self.prompt)


class MissionsPrefixTest(unittest.TestCase):
    """What ``harness.loop`` actually hands every mission session."""

    def setUp(self):
        self.prefix = loop.build_missions_system_prompt(
            REPO_ROOT, REPO_ROOT / "app-template"
        )

    def test_the_prefix_is_the_missions_prompt_plus_agents_md(self):
        self.assertIn(MISSIONS_PROMPT.read_text(encoding="utf-8").strip(), self.prefix)
        self.assertIn(AGENTS_MD.read_text(encoding="utf-8").strip(), self.prefix)

    def test_the_prefix_carries_the_scaffold_api_no_mission_may_read(self):
        self.assertIn("export const appConfig = defineApp({", self.prefix)
        self.assertIn("await addRecord(user, {", self.prefix)

    def test_the_prefix_never_asks_a_mission_for_a_command_or_a_report(self):
        # AGENTS.md is appended *after* the missions prompt, so its bullets are
        # the last instructions a mission reads. Five of them used to
        # contradict it outright ("run them before claiming success" against
        # "Never run a command"; the report.partial.json shape against "never
        # create or edit one" -- measured at 732 output tokens in Phase 2).
        # They now live behind a "single-session runs only" heading, which is
        # what this checks: the mission-facing half must stay clean.
        # 2026-09-04: those bullets now live in solution/system-prompt.md (the
        # single-session prompt), so the whole prefix must be clean, not just
        # its head -- and AGENTS.md no longer needs a "skip this section".
        whole = _prose(self.prefix)
        for forbidden in ("npm test", "npm run build", "report.partial.json", "pgrep", "result.json"):
            self.assertNotIn(forbidden, whole, forbidden)
        self.assertNotIn("## Single-session runs only", self.prefix)

    def test_the_prefix_never_asks_a_mission_for_a_second_test_file(self):
        # The Builder's mission is app-config.ts alone; a bullet telling it to
        # add a test file too produces an unbriefed second file that queries
        # invented strings and turns vitest red.
        whole = _prose(self.prefix)
        self.assertNotIn("Add at least one completed, passing", whole)
        self.assertNotIn("add ONE small component", whole)

    def test_the_prefix_does_not_carry_journeys_md(self):
        # §3: the coverage checklist moved into the Analyst's system prompt.
        # A mission session paying for it again would be ~600 wasted input
        # tokens per session for guidance it cannot act on.
        journeys = JOURNEYS_MD.read_text(encoding="utf-8").strip()
        self.assertNotIn(journeys, self.prefix)
        self.assertNotIn("## Behaviors to implement and test when implied", self.prefix)

    def test_a_missing_prompt_file_degrades_instead_of_raising(self):
        missing = pathlib.Path(self._scratch()) / "nowhere"
        prefix = loop.build_missions_system_prompt(missing, missing)
        self.assertEqual(prefix, "")

    def _scratch(self) -> str:
        return str(support.scratch_root())


if __name__ == "__main__":
    unittest.main()
