"""The demo narration: one plain-English line per stage, on stderr only.

Phase 6's demo is a screen recording of this process's stderr, and the
technical ``role · text`` lines are unreadable to the audience the demo is
for. :func:`harness.log.narrate` adds one marked, jargon-free line at each
stage boundary; these tests pin the three properties that make it safe:

- it writes to **stderr** (and the file sink) and never to stdout, which is
  the Pi event stream and has to stay byte-exact (``test_stdout_integrity``);
- ``HARNESS_NARRATE=0`` silences it, so a tool that only wants the technical
  lines can have them;
- the lines a real run produces actually appear, in the order the run happens,
  for both bodies (missions, and the single-session fallback).

The two end-to-end tests drive the real CLI as a subprocess with the same four
doubles ``test_main_wiring.MissionsModeTest`` uses -- fake Pi, fake gateway
scripted from ``fixtures/spec-books.json``, stub tsc/vitest/vite. Their helpers
are duplicated here on purpose rather than imported: importing another test
module would run its ``unittest.main``-less module body under two names and
couple this file to fixtures it does not own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest
from typing import List
from unittest import mock

from harness import log as log_mod
from harness.tests import support

REPO_ROOT = support.REPO_ROOT

#: What :func:`harness.log.narrate` prefixes every line with. The tests read
#: the narration back off stderr by this marker, exactly as a viewer's eye
#: picks it out of the technical lines.
MARKER = "▶ "


def _narration(stderr: str) -> List[str]:
    """Every narration line in ``stderr``, in order, marker stripped."""
    return [
        line.split(MARKER, 1)[1].strip()
        for line in stderr.splitlines()
        if MARKER in line
    ]


def _assert_in_order(case: unittest.TestCase, lines: List[str], expected: List[str]) -> None:
    """Every ``expected`` fragment appears in ``lines``, in this order."""
    remaining = list(lines)
    for fragment in expected:
        for index, line in enumerate(remaining):
            if fragment in line:
                remaining = remaining[index + 1:]
                break
        else:
            case.fail("{0!r} not found after the previous line in {1}".format(fragment, lines))


# -- doubles (copied from test_main_wiring.MissionsModeTest) -----------------


def _copy_app_template(dest_parent: pathlib.Path) -> pathlib.Path:
    dest = dest_parent / "app"
    shutil.copytree(
        REPO_ROOT / "app-template",
        dest,
        ignore=shutil.ignore_patterns("node_modules"),
        symlinks=True,
    )
    return dest


def _executable(path: pathlib.Path, script: str) -> pathlib.Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_vitest_stub(path: pathlib.Path, tests_run) -> pathlib.Path:
    """A stand-in for ``node_modules/.bin/vitest``: every named test passes."""
    assertion_results = [{"fullName": name, "status": "passed"} for name in tests_run]
    return _executable(
        path,
        (
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "output_file = None\n"
            "for arg in sys.argv[1:]:\n"
            "    if arg.startswith('--outputFile='):\n"
            "        output_file = arg.split('=', 1)[1]\n"
            "data = {0}\n"
            "if output_file:\n"
            "    with open(output_file, 'w', encoding='utf-8') as handle:\n"
            "        json.dump(data, handle)\n"
            "sys.exit(0)\n"
        ).format(
            json.dumps(
                {
                    "numTotalTests": len(assertion_results),
                    "numPassedTests": len(assertion_results),
                    "numFailedTests": 0,
                    "testResults": [{"assertionResults": assertion_results}],
                }
            )
        ),
    )


def _write_tsc_stub(path: pathlib.Path) -> pathlib.Path:
    return _executable(path, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")


def _write_red_then_green_tsc_stub(path: pathlib.Path, marker: pathlib.Path) -> pathlib.Path:
    """Red on its first invocation only: round 1 red, one repair, round 2 green.

    ``observe()`` spawns a fresh tsc every round, so the marker file is how one
    process tells the next that the error has already been reported.
    """
    return _executable(
        path,
        (
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "MARKER = {marker!r}\n"
            "if os.path.exists(MARKER):\n"
            "    sys.exit(0)\n"
            "open(MARKER, 'w').close()\n"
            "sys.stdout.write(\"src/app-config.ts(12,5): error TS2322: \"\n"
            "                 \"Type 'string' is not assignable to type 'number'.\\n\")\n"
            "sys.exit(1)\n"
        ).format(marker=str(marker)),
    )


def _write_vite_stub(path: pathlib.Path) -> pathlib.Path:
    return _executable(
        path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('vite v5.0.0 building for production...\\nbuilt in 412ms\\n')\n"
        "sys.exit(0)\n",
    )


def _plan_titles() -> List[str]:
    """The normalised journey titles the scripted spec produces."""
    from harness.analyst import normalize_spec
    from harness.plan import derive_plan

    raw = json.loads(
        (support.TESTS_DIR / "fixtures" / "spec-books.json").read_text(encoding="utf-8")
    )
    return [test["title"] for test in derive_plan(normalize_spec(raw))["tests"]]


def _stdout_records(stdout: bytes):
    """Every stdout line parsed as JSON; raises on the first line that is not."""
    records = []
    for number, line in enumerate(stdout.splitlines(), start=1):
        text = line.decode("utf-8", "replace")
        if not text.strip():
            continue
        try:
            records.append(json.loads(text))
        except ValueError as exc:
            raise AssertionError(
                "stdout line {0} is not a Pi event: {1!r} ({2})".format(number, text[:200], exc)
            )
    return records


# -- 1. the helper itself ----------------------------------------------------


class NarrateTest(unittest.TestCase):
    """``narrate`` writes one marked stderr line, and nothing on stdout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        log_mod.close_file_sink()
        self._tmp.cleanup()

    def _capture(self, text: str, env=None):
        out, err = io.StringIO(), io.StringIO()
        environment = {"HARNESS_COLOR": "0", "HARNESS_NARRATE": "1"}
        environment.update(env or {})
        with mock.patch.dict(os.environ, environment):
            with contextlib.redirect_stdout(out):
                with contextlib.redirect_stderr(err):
                    log_mod.narrate(text)
        return out.getvalue(), err.getvalue()

    def test_narrate_writes_one_marked_line_to_stderr(self):
        stdout, stderr = self._capture("Writing the app and its tests…")
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, MARKER + "Writing the app and its tests…\n")

    def test_narrate_writes_nothing_to_stdout_even_with_colour_on(self):
        # Colour is an escape sequence around the same line -- still stderr,
        # still one line, still nothing on the Pi event stream.
        stdout, stderr = self._capture("All 10 tests pass", env={"HARNESS_COLOR": "1"})
        self.assertEqual(stdout, "")
        self.assertIn("All 10 tests pass", stderr)
        self.assertIn(MARKER, stderr)
        self.assertEqual(len(stderr.splitlines()), 1)

    def test_harness_narrate_0_silences_it(self):
        stdout, stderr = self._capture("Reading the product idea…", env={"HARNESS_NARRATE": "0"})
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_narration_enabled_follows_the_environment(self):
        with mock.patch.dict(os.environ, {"HARNESS_NARRATE": "0"}):
            self.assertFalse(log_mod.narration_enabled())
        with mock.patch.dict(os.environ, {"HARNESS_NARRATE": "1"}):
            self.assertTrue(log_mod.narration_enabled())
        env = dict(os.environ)
        env.pop("HARNESS_NARRATE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(log_mod.narration_enabled(), "narration is on by default")

    def test_narration_reaches_the_file_sink_like_any_other_line(self):
        sink = self.root / "harness.log"
        log_mod.set_file_sink(sink)
        try:
            self._capture("Production build passed")
        finally:
            log_mod.close_file_sink()
        self.assertIn(MARKER + "Production build passed", sink.read_text(encoding="utf-8"))

    def test_a_log_line_is_unchanged_by_the_shared_writer(self):
        # ``log`` and ``narrate`` now share one writer; the technical line's
        # shape is pinned by many other tests and must not have moved.
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HARNESS_COLOR": "0"}):
            with contextlib.redirect_stdout(out):
                with contextlib.redirect_stderr(err):
                    log_mod.log("harness", "mode · missions (spec with 10 journey(s))")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "harness · mode · missions (spec with 10 journey(s))\n")


# -- 2. a full missions run --------------------------------------------------


class MissionsNarrationTest(unittest.TestCase):
    """The narration a green missions run produces, end to end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = _copy_app_template(self.root)
        self.server = None

        seed_config = (REPO_ROOT / "app-template" / "src" / "app-config.ts").read_text(
            encoding="utf-8"
        )
        self.config_source = self.root / "written-config.ts"
        self.config_source.write_text(
            seed_config + "\n// written by the fake Builder\n", encoding="utf-8"
        )
        self.tests_source = self.root / "written-tests.tsx"
        self.tests_source.write_text(
            (REPO_ROOT / "app-template" / "src" / "test" / "journeys.template.tsx").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        self.tsc = _write_tsc_stub(self.root / "tsc-stub.py")
        self.vitest = _write_vitest_stub(self.root / "vitest-stub.py", _plan_titles())
        self.vite = _write_vite_stub(self.root / "vite-stub.py")

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    def _gateway(self, spare: int = 20):
        from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

        spec_text = (support.TESTS_DIR / "fixtures" / "spec-books.json").read_text(
            encoding="utf-8"
        )
        server = FakeGatewayServer()
        server.script(
            [ScriptedResponse(status=200, body=ok_response(content=spec_text, completion_tokens=800))]
            + [ScriptedResponse(status=200, body=ok_response(content="{}")) for _ in range(spare)]
        )
        server.start()
        self.server = server
        return server

    def _run(self, **extra):
        self._gateway()
        env = {
            "BERGET_API_KEY": "fake-test-key",
            "HARNESS_GATEWAY_URL": self.server.base_url,
            "HARNESS_NARRATE": "1",
            "HARNESS_SESSION_MODE": "per-mission",
            "FAKE_PI_WRITE_ON_PROMPT": "src/app-config.ts={0};src/journeys.test.tsx={1}".format(
                self.config_source, self.tests_source
            ),
            "HARNESS_TSC_BIN": str(self.tsc),
            "HARNESS_VITEST_BIN": str(self.vitest),
            "HARNESS_VITE_BIN": str(self.vite),
        }
        env.update(extra)
        return support.run_harness(
            self.run_dir, timeout_ms=300_000, cwd=self.app_dir, env_extra=env, wait_s=120.0
        )

    def test_a_green_missions_run_narrates_every_stage_in_order(self):
        code, stdout, stderr = self._run()
        self.assertEqual(code, 0, stderr)

        lines = _narration(stderr)
        _assert_in_order(
            self,
            lines,
            [
                "Reading the product idea",
                "Understood the product:",
                "Writing the app and its tests",
                "tests pass",
                "Production build passed",
                "Done:",
            ],
        )
        # The Analyst's line counts what the spec actually carries.
        understood = next(line for line in lines if line.startswith("Understood the product:"))
        self.assertRegex(understood, r"^Understood the product: \d+ fields?, \d+ user journeys?$")
        self.assertIn(
            "Done: the app builds and every test passes — report written", lines, stderr
        )
        # Readable at a glance: no line long enough to wrap in a recording.
        for line in lines:
            self.assertLessEqual(len(line), 90, line)

        # The technical lines the rest of the suite pins are all still there.
        for pinned in ("analyst · spec produced", "mode · missions", "exit 0"):
            self.assertIn(pinned, stderr)

    def test_the_narration_never_leaks_into_the_pi_event_stream(self):
        _, stdout, stderr = self._run()
        self.assertNotIn(MARKER.strip().encode("utf-8"), stdout)
        records = _stdout_records(stdout)
        self.assertTrue(records, stderr)
        self.assertTrue(all(isinstance(record, dict) for record in records), records[:3])
        self.assertTrue(any(record.get("type") == "message_end" for record in records), records[:3])

    def test_a_repair_round_says_what_is_wrong_and_which_attempt_this_is(self):
        # The one line a viewer has to understand when the run is not green
        # first time: what failed, and that the harness is fixing it.
        _write_red_then_green_tsc_stub(self.tsc, self.root / "tsc-was-red")
        code, _, stderr = self._run()
        self.assertEqual(code, 0, stderr)

        lines = _narration(stderr)
        _assert_in_order(
            self,
            lines,
            [
                "Writing the app and its tests",
                "The code does not typecheck — repairing (attempt 1 of 3)",
                "Production build passed",
                "Done:",
            ],
        )
        self.assertNotIn("tsc", " ".join(lines))  # no jargon where a plain word exists

    def test_harness_narrate_0_leaves_only_the_technical_lines(self):
        _, _, stderr = self._run(HARNESS_NARRATE="0")
        self.assertEqual(_narration(stderr), [])
        self.assertIn("mode · missions", stderr)
        self.assertIn("exit 0", stderr)


# -- 3. the single-session fallback ------------------------------------------


class SingleModeNarrationTest(unittest.TestCase):
    """No key means no spec: the fallback has to explain itself too."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = _copy_app_template(self.root)
        self.vitest = _write_vitest_stub(
            self.root / "vitest-stub.py", ["creates a record", "lists records", "filters records"]
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_fallback_path_narrates_its_start_and_its_finish(self):
        # support.harness_environment() already pops every credential, so the
        # Analyst never runs and ``resolve_mode`` falls back to one session.
        code, stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=60_000,
            cwd=self.app_dir,
            env_extra={"HARNESS_NARRATE": "1", "HARNESS_VITEST_BIN": str(self.vitest)},
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("mode · single (no usable spec)", stderr)

        lines = _narration(stderr)
        _assert_in_order(
            self,
            lines,
            [
                "Reading the product idea",
                "Could not derive a spec — building in one session instead",
                "Writing the whole app and its tests in one session",
                "Done: all 3 tests pass — report written",
            ],
        )
        for line in lines:
            self.assertLessEqual(len(line), 90, line)
        _stdout_records(stdout)  # stdout is still nothing but Pi events


if __name__ == "__main__":
    unittest.main()
