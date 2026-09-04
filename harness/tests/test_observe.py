"""Unit tests for :mod:`harness.observe` and ``report.py``'s additive parts.

Every external command (``tsc``, ``vitest``, ``vite``) is a small Python stub
written by the test and pointed at through the module's own ``HARNESS_*_BIN``
overrides, so nothing here needs ``node_modules``, a network, or a model. The
stubs deliberately mimic the real binaries' *observable* behaviour only:
stdout text, exit code, and (for vitest) the JSON report file.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from typing import Dict, Optional, Sequence, Tuple
from unittest import mock

from harness import observe as observe_mod
from harness import report
from harness.tests import support

SEED_CONFIG = 'import { defineApp } from "./lib/config-types.js";\nexport const appConfig = {};\n'


def _write_executable(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _write_tsc_stub(
    path: pathlib.Path,
    *,
    lines: Sequence[str] = (),
    exit_code: int = 0,
    sleep_s: float = 0.0,
    marker: Optional[pathlib.Path] = None,
) -> None:
    """A stand-in for ``node_modules/.bin/tsc``: prints ``lines``, exits ``exit_code``."""
    _write_executable(
        path,
        (
            "import sys, time\n"
            "marker = {marker!r}\n"
            "if marker:\n"
            "    open(marker, 'a').close()\n"
            "time.sleep({sleep})\n"
            "sys.stdout.write({text!r})\n"
            "sys.exit({code})\n"
        ).format(
            marker=str(marker) if marker else None,
            sleep=float(sleep_s),
            text="".join(line + "\n" for line in lines),
            code=int(exit_code),
        ),
    )


def _write_vitest_stub(
    path: pathlib.Path,
    *,
    passed: Sequence[str] = (),
    failed: Sequence[Tuple[str, str]] = (),
    marker: Optional[pathlib.Path] = None,
) -> None:
    """A stand-in for ``node_modules/.bin/vitest`` writing a canned JSON report.

    ``passed`` are full test names; ``failed`` are ``(full name, first
    failureMessages entry)`` pairs. Deliberately a private copy of
    ``test_main_wiring.py``'s helper: that one is M5's and only knows about
    passing tests.
    """
    assertions = [{"fullName": name, "status": "passed"} for name in passed]
    for name, message in failed:
        assertions.append(
            {"fullName": name, "status": "failed", "failureMessages": [message]}
        )
    data = {
        "numTotalTests": len(assertions),
        "numPassedTests": len(passed),
        "numFailedTests": len(failed),
        "testResults": [{"assertionResults": assertions}],
    }
    _write_executable(
        path,
        (
            "import json, sys\n"
            "marker = {marker!r}\n"
            "if marker:\n"
            "    open(marker, 'a').close()\n"
            "output_file = None\n"
            "for arg in sys.argv[1:]:\n"
            "    if arg.startswith('--outputFile='):\n"
            "        output_file = arg.split('=', 1)[1]\n"
            "data = json.loads({payload!r})\n"
            "if output_file:\n"
            "    with open(output_file, 'w', encoding='utf-8') as handle:\n"
            "        json.dump(data, handle)\n"
            "sys.exit(1 if data['numFailedTests'] else 0)\n"
        ).format(marker=str(marker) if marker else None, payload=json.dumps(data)),
    )


def _write_vite_stub(
    path: pathlib.Path,
    *,
    lines: Sequence[str] = ("vite v5.0.0 building for production...", "built in 812ms"),
    exit_code: int = 0,
    marker: Optional[pathlib.Path] = None,
) -> None:
    """A stand-in for ``node_modules/.bin/vite``; only ``vite build`` is ever run."""
    _write_executable(
        path,
        (
            "import sys\n"
            "marker = {marker!r}\n"
            "if marker:\n"
            "    open(marker, 'a').close()\n"
            "sys.stdout.write({text!r})\n"
            "sys.exit({code})\n"
        ).format(
            marker=str(marker) if marker else None,
            text="".join(line + "\n" for line in lines),
            code=int(exit_code),
        ),
    )


class ObserveTestCase(unittest.TestCase):
    """A writable app/seed/harness trio plus the three binary overrides."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.app_dir = self.root / "app"
        self.seed_dir = self.root / "seed"
        self.harness_dir = self.root / "harness"
        for base in (self.app_dir, self.seed_dir):
            (base / "src" / "lib").mkdir(parents=True)
            (base / "src" / "app-config.ts").write_text(SEED_CONFIG, encoding="utf-8")
            (base / "src" / "lib" / "collection.ts").write_text(
                "".join("// line {0}\n".format(n) for n in range(200)), encoding="utf-8"
            )
        self.harness_dir.mkdir()

        self.tsc_stub = self.root / "tsc-stub.py"
        self.vitest_stub = self.root / "vitest-stub.py"
        self.vite_stub = self.root / "vite-stub.py"
        _write_tsc_stub(self.tsc_stub)
        _write_vitest_stub(self.vitest_stub, passed=["journeys > adds a record"])
        _write_vite_stub(self.vite_stub)

        patcher = mock.patch.dict(
            os.environ,
            {
                "HARNESS_TSC_BIN": str(self.tsc_stub),
                "HARNESS_VITEST_BIN": str(self.vitest_stub),
                "HARNESS_VITE_BIN": str(self.vite_stub),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        # The observe-<n>.json counter is cached per harness directory; each
        # test gets a fresh one, but clear it so a reused temp path cannot
        # inherit another test's numbering.
        observe_mod._INDEXES.clear()

    def write_app_file(self, relative: str, text: str) -> pathlib.Path:
        path = self.app_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def observe(self, **kwargs) -> observe_mod.Observation:
        options: Dict[str, object] = {"seed_dir": self.seed_dir, "timeout_s": 20.0}
        options.update(kwargs)
        return observe_mod.observe(self.app_dir, self.harness_dir, **options)


class TypecheckTest(ObserveTestCase):
    def test_error_lines_are_parsed_verbatim(self):
        _write_tsc_stub(
            self.tsc_stub,
            lines=[
                "src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
                "  some indented continuation that is not a diagnostic",
                "src/journeys.test.tsx(4,1): error TS2307: Cannot find module './test/helpers.js'.",
                "Found 2 errors in 2 files.",
            ],
            exit_code=2,
        )
        observation = self.observe()
        self.assertTrue(observation.tsc_ran)
        self.assertFalse(observation.tsc_ok)
        self.assertEqual(
            observation.tsc_errors,
            [
                "src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
                "src/journeys.test.tsx(4,1): error TS2307: Cannot find module './test/helpers.js'.",
            ],
        )
        self.assertFalse(observation.green)

    def test_error_lines_are_capped(self):
        lines = [
            "src/app-config.ts({0},1): error TS2322: nope.".format(n)
            for n in range(1, observe_mod.MAX_TSC_ERRORS + 25)
        ]
        _write_tsc_stub(self.tsc_stub, lines=lines, exit_code=2)
        observation = self.observe()
        self.assertEqual(len(observation.tsc_errors), observe_mod.MAX_TSC_ERRORS)

    def test_a_red_typecheck_skips_vitest_entirely(self):
        marker = self.root / "vitest-ran"
        _write_vitest_stub(self.vitest_stub, passed=["journeys > adds a record"], marker=marker)
        _write_tsc_stub(
            self.tsc_stub, lines=["src/app-config.ts(1,1): error TS1005: ';' expected."], exit_code=2
        )
        observation = self.observe()
        self.assertFalse(marker.exists(), "vitest must not be spawned while tsc is red")
        self.assertEqual(observation.vitest, report.empty_observation())
        self.assertFalse(observation.green)

    def test_skip_vitest_on_tsc_error_false_still_runs_vitest(self):
        marker = self.root / "vitest-ran"
        _write_vitest_stub(self.vitest_stub, passed=["journeys > adds a record"], marker=marker)
        _write_tsc_stub(
            self.tsc_stub, lines=["src/app-config.ts(1,1): error TS1005: ';' expected."], exit_code=2
        )
        observation = self.observe(skip_vitest_on_tsc_error=False)
        self.assertTrue(marker.exists())
        self.assertTrue(observation.vitest["green"])
        self.assertFalse(observation.green, "a red typecheck is never green")

    def test_a_timeout_means_tsc_never_ran(self):
        _write_tsc_stub(self.tsc_stub, sleep_s=5.0)
        observation = self.observe(timeout_s=1.0)
        self.assertFalse(observation.tsc_ran)
        self.assertFalse(observation.tsc_ok)
        self.assertEqual(len(observation.tsc_errors), 1)
        self.assertIn("did not finish", observation.tsc_errors[0])

    def test_a_missing_binary_means_tsc_never_ran(self):
        with mock.patch.dict(os.environ, {"HARNESS_TSC_BIN": str(self.root / "absent")}):
            observation = self.observe()
        self.assertFalse(observation.tsc_ran)
        self.assertFalse(observation.tsc_ok)
        self.assertIn("could not run", observation.tsc_errors[0])

    def test_a_nonzero_exit_with_no_diagnostics_still_reports_something(self):
        _write_tsc_stub(self.tsc_stub, lines=["error TS5058: tsconfig.json not found."], exit_code=1)
        observation = self.observe()
        self.assertTrue(observation.tsc_ran)
        self.assertFalse(observation.tsc_ok)
        self.assertTrue(observation.tsc_errors)


class VitestFailureTest(ObserveTestCase):
    def test_failures_carry_the_name_and_first_message(self):
        _write_vitest_stub(
            self.vitest_stub,
            passed=["journeys > adds a record"],
            failed=[("journeys > lends a book", "Unable to find an element with the text: Out: Sam")],
        )
        observation = self.observe()
        self.assertEqual(observation.vitest["passed"], 1)
        self.assertEqual(observation.vitest["failed"], 1)
        self.assertEqual(
            observation.vitest["failures"],
            [
                {
                    "name": "journeys > lends a book",
                    "message": "Unable to find an element with the text: Out: Sam",
                }
            ],
        )
        self.assertEqual(observation.failing_names(), ["journeys > lends a book"])
        self.assertFalse(observation.green)

    def test_a_long_message_is_cut_to_600_characters(self):
        _write_vitest_stub(self.vitest_stub, failed=[("journeys > x", "y" * 5000)])
        observation = report.observe(self.app_dir, self.harness_dir, timeout_s=20.0)
        self.assertEqual(len(observation["failures"][0]["message"]), 600)

    def test_a_skipped_test_counts_as_a_failure_to_report(self):
        # The runner rejects skipped/todo tests as hard as failing ones, so a
        # non-"passed" status must never be silently dropped.
        output = self.harness_dir / "vitest.json"
        _write_executable(
            self.vitest_stub,
            "import json, sys\n"
            "data = {0}\n".format(
                json.dumps(
                    {
                        "numTotalTests": 1,
                        "numPassedTests": 0,
                        "numFailedTests": 0,
                        "testResults": [
                            {"assertionResults": [{"fullName": "journeys > todo", "status": "todo"}]}
                        ],
                    }
                )
            )
            + "for arg in sys.argv[1:]:\n"
            "    if arg.startswith('--outputFile='):\n"
            "        open(arg.split('=', 1)[1], 'w').write(json.dumps(data))\n",
        )
        result = report.observe(self.app_dir, self.harness_dir, timeout_s=20.0)
        self.assertEqual(result["failures"], [{"name": "journeys > todo", "message": ""}])
        self.assertFalse(result["green"])
        self.assertTrue(output.is_file())

    def test_green_when_tsc_passes_and_every_test_passes(self):
        observation = self.observe()
        self.assertTrue(observation.tsc_ok)
        self.assertTrue(observation.vitest["green"])
        self.assertTrue(observation.green)
        self.assertFalse(observation.build_ran)


class BuildTest(ObserveTestCase):
    def test_the_build_runs_only_when_asked(self):
        marker = self.root / "vite-ran"
        _write_vite_stub(self.vite_stub, marker=marker)
        observation = self.observe()
        self.assertFalse(marker.exists())
        self.assertFalse(observation.build_ran)
        self.assertIsNone(observation.build_ok)
        self.assertEqual(observation.build_tail, "")

    def test_a_green_build_records_its_tail(self):
        _write_vite_stub(
            self.vite_stub, lines=["line {0}".format(n) for n in range(60)] + ["built in 812ms"]
        )
        observation = self.observe(run_build=True)
        self.assertTrue(observation.build_ran)
        self.assertTrue(observation.build_ok)
        tail = observation.build_tail.splitlines()
        self.assertEqual(len(tail), observe_mod.BUILD_TAIL_LINES)
        self.assertEqual(tail[-1], "built in 812ms")

    def test_a_failed_build_is_recorded_but_does_not_change_green(self):
        _write_vite_stub(
            self.vite_stub,
            lines=["error during build:", "src/app-config.ts: Unexpected token"],
            exit_code=1,
        )
        observation = self.observe(run_build=True)
        self.assertTrue(observation.build_ran)
        self.assertFalse(observation.build_ok)
        self.assertIn("Unexpected token", observation.build_tail)
        # green is tsc + tests + file sizes only; the Supervisor reads
        # build_ok separately (policy 12).
        self.assertTrue(observation.green)

    def test_a_missing_vite_binary_never_raises(self):
        with mock.patch.dict(os.environ, {"HARNESS_VITE_BIN": str(self.root / "absent")}):
            observation = self.observe(run_build=True)
        self.assertFalse(observation.build_ran)
        self.assertIsNone(observation.build_ok)
        self.assertIn("could not run", observation.build_tail)


class FileFactsTest(ObserveTestCase):
    def test_an_untouched_config_is_not_changed_from_seed(self):
        observation = self.observe()
        config = observation.files[observe_mod.CONFIG_FILE]
        self.assertTrue(config["exists"])
        self.assertEqual(config["lines"], 2)
        self.assertFalse(config["changed_from_seed"])
        tests = observation.files[observe_mod.TESTS_FILE]
        self.assertFalse(tests["exists"])
        self.assertFalse(tests["changed_from_seed"])

    def test_a_rewritten_config_and_a_new_test_file_are_both_changed(self):
        self.write_app_file("src/app-config.ts", SEED_CONFIG + "// the Builder was here\n")
        self.write_app_file("src/journeys.test.tsx", "it('x', () => {});\n")
        observation = self.observe()
        self.assertTrue(observation.files[observe_mod.CONFIG_FILE]["changed_from_seed"])
        self.assertTrue(observation.files[observe_mod.TESTS_FILE]["exists"])
        self.assertTrue(observation.files[observe_mod.TESTS_FILE]["changed_from_seed"])

    def test_without_a_seed_directory_existing_files_count_as_changed(self):
        observation = self.observe(seed_dir=None)
        self.assertTrue(observation.files[observe_mod.CONFIG_FILE]["changed_from_seed"])

    def test_over_limit_ignores_untouched_seed_files(self):
        # The scaffold ships files over 150 lines; the fixture's collection.ts
        # is 200 lines in both the app and the seed.
        observation = self.observe()
        self.assertEqual(observation.over_limit, [])

    def test_over_limit_flags_a_file_a_mission_made_too_long(self):
        self.write_app_file(
            "src/journeys.test.tsx", "".join("// {0}\n".format(n) for n in range(200))
        )
        self.write_app_file(
            "src/lib/collection.ts", "".join("// changed {0}\n".format(n) for n in range(200))
        )
        observation = self.observe()
        self.assertEqual(
            observation.over_limit, ["src/journeys.test.tsx", "src/lib/collection.ts"]
        )
        self.assertFalse(observation.green, "an over-long file is never green")

    def test_a_file_at_the_limit_is_not_flagged(self):
        self.write_app_file(
            "src/journeys.test.tsx",
            "".join("// {0}\n".format(n) for n in range(observe_mod.MAX_FILE_LINES)),
        )
        self.assertEqual(self.observe().over_limit, [])


class CoverageTest(unittest.TestCase):
    def test_exact_match_ignores_the_describe_prefix_and_case(self):
        result = observe_mod.coverage(
            ["Add a book", "Return a book"],
            ["journeys > Add a book", "journeys > return a BOOK"],
        )
        self.assertEqual(result, {"missing": [], "matched": 2, "total": 2})

    def test_fuzzy_match_catches_a_reworded_title(self):
        result = observe_mod.coverage(
            ["Lend a book to someone"], ["journeys > lends a book to someone"]
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["missing"], [])

    def test_fuzzy_match_survives_the_testers_usual_rewording(self):
        # The ten journeys of the public idea's spec, as a Tester that writes
        # "it(\"adds a book\")" instead of the title verbatim would name them.
        titles = [
            "Add a book", "Reject empty title", "Edit a book", "Delete a book",
            "Lend a book", "Return a book", "Filter to what's out",
            "Filter to what's home", "See how many are lent out", "Refresh persists data",
        ]
        names = [
            "journeys > adds a book", "journeys > rejects an empty title",
            "journeys > edits a book", "journeys > deletes a book",
            "journeys > lends a book", "journeys > returns a book",
            "journeys > filters to what's out", "journeys > filters to what's home",
            "journeys > sees how many are lent out", "journeys > refresh persists data",
        ]
        self.assertEqual(
            observe_mod.coverage(titles, names), {"missing": [], "matched": 10, "total": 10}
        )

    def test_stemming_never_collapses_two_different_words(self):
        result = observe_mod.coverage(
            ["Add a note"], ["journeys > adds a notes"]  # note/notes must still meet
        )
        self.assertEqual(result["matched"], 1)
        result = observe_mod.coverage(["Add a note"], ["journeys > discards a draft"])
        self.assertEqual(result["missing"], ["Add a note"])

    def test_an_unrelated_test_leaves_the_journey_missing(self):
        result = observe_mod.coverage(
            ["Filter to what's out"], ["journeys > recovers from malformed saved data"]
        )
        self.assertEqual(result, {"missing": ["Filter to what's out"], "matched": 0, "total": 1})

    def test_one_test_cannot_cover_two_journeys(self):
        result = observe_mod.coverage(
            ["Add a book", "Add a book"], ["journeys > Add a book"]
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["missing"], ["Add a book"])

    def test_an_exact_match_is_never_stolen_by_a_fuzzy_one(self):
        result = observe_mod.coverage(
            ["Lends a book", "Lend a book"], ["journeys > lend a book", "journeys > lends a book"]
        )
        self.assertEqual(result, {"missing": [], "matched": 2, "total": 2})

    def test_no_journeys_means_nothing_missing(self):
        self.assertEqual(
            observe_mod.coverage([], ["journeys > x"]),
            {"missing": [], "matched": 0, "total": 0},
        )

    def test_expected_titles_reads_a_plan_or_a_spec(self):
        plan = {"tests": [{"title": "Add a book"}, {"title": "Lend a book"}]}
        spec = {"journeys": [{"title": "Add a book"}, {"title": ""}, {"nope": 1}]}
        self.assertEqual(observe_mod.expected_titles(plan), ["Add a book", "Lend a book"])
        self.assertEqual(observe_mod.expected_titles(spec), ["Add a book"])
        self.assertEqual(observe_mod.expected_titles(None), [])
        self.assertEqual(observe_mod.expected_titles({"tests": "junk"}), [])


class CoverageInObservationTest(ObserveTestCase):
    def test_coverage_is_measured_against_passing_and_failing_tests(self):
        _write_vitest_stub(
            self.vitest_stub,
            passed=["journeys > Add a book"],
            failed=[("journeys > Lend a book", "boom")],
        )
        spec = {"journeys": [{"title": "Add a book"}, {"title": "Lend a book"}, {"title": "Delete a book"}]}
        observation = self.observe(spec=spec)
        self.assertEqual(observation.coverage["matched"], 2)
        self.assertEqual(observation.coverage["total"], 3)
        self.assertEqual(observation.coverage["missing"], ["Delete a book"])

    def test_coverage_is_empty_without_a_spec(self):
        self.assertEqual(
            self.observe().coverage, {"missing": [], "matched": 0, "total": 0}
        )


class SignatureTest(ObserveTestCase):
    def test_two_identical_runs_share_a_signature(self):
        _write_vitest_stub(self.vitest_stub, failed=[("journeys > lends a book", "boom")])
        first = self.observe()
        second = self.observe()
        self.assertEqual(first.signature, second.signature)
        self.assertNotEqual(first.signature, "")

    def test_a_different_failing_test_changes_the_signature(self):
        _write_vitest_stub(self.vitest_stub, failed=[("journeys > lends a book", "boom")])
        first = self.observe()
        _write_vitest_stub(self.vitest_stub, failed=[("journeys > returns a book", "boom")])
        self.assertNotEqual(first.signature, self.observe().signature)

    def test_the_message_alone_does_not_change_the_signature(self):
        _write_vitest_stub(self.vitest_stub, failed=[("journeys > lends a book", "boom")])
        first = self.observe()
        _write_vitest_stub(self.vitest_stub, failed=[("journeys > lends a book", "different wording")])
        self.assertEqual(first.signature, self.observe().signature)

    def test_test_order_does_not_change_the_signature(self):
        self.assertEqual(
            observe_mod.signature([], ["b", "a"], []),
            observe_mod.signature([], ["a", "b"], []),
        )

    def test_tsc_errors_and_over_limit_both_feed_the_signature(self):
        base = observe_mod.signature(["e1"], [], [])
        self.assertNotEqual(base, observe_mod.signature(["e2"], [], []))
        self.assertNotEqual(base, observe_mod.signature(["e1"], [], ["src/x.ts"]))

    def test_a_green_run_still_has_a_stable_signature(self):
        self.assertEqual(self.observe().signature, self.observe().signature)


class ArtifactTest(ObserveTestCase):
    def test_each_observation_is_written_and_numbered(self):
        first = self.observe()
        second = self.observe()
        first_path = self.harness_dir / "observe-1.json"
        second_path = self.harness_dir / "observe-2.json"
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(json.loads(first_path.read_text(encoding="utf-8")), first.as_dict())
        self.assertEqual(json.loads(second_path.read_text(encoding="utf-8")), second.as_dict())

    def test_numbering_continues_from_what_is_already_on_disk(self):
        (self.harness_dir / "observe-7.json").write_text("{}\n", encoding="utf-8")
        self.observe()
        self.assertTrue((self.harness_dir / "observe-8.json").is_file())

    def test_as_dict_is_json_serializable_and_complete(self):
        payload = self.observe(run_build=True).as_dict()
        self.assertEqual(
            sorted(payload),
            [
                "build_ok", "build_ran", "build_tail", "coverage", "elapsed_s", "files",
                "green", "over_limit", "signature", "tsc_errors", "tsc_ok", "tsc_ran", "vitest",
            ],
        )
        json.dumps(payload)


class NeverRaisesTest(ObserveTestCase):
    def test_an_app_directory_that_does_not_exist(self):
        observation = observe_mod.observe(
            self.root / "absent", self.harness_dir, seed_dir=self.seed_dir, timeout_s=5.0
        )
        self.assertFalse(observation.green)
        self.assertFalse(observation.tsc_ran)
        self.assertEqual(observation.files[observe_mod.CONFIG_FILE]["exists"], False)

    def test_a_harness_directory_that_cannot_be_created(self):
        blocked = self.root / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        observation = observe_mod.observe(
            self.app_dir, blocked, seed_dir=self.seed_dir, timeout_s=20.0
        )
        # tsc still ran; only the two writes (vitest.json, observe-1.json) failed.
        self.assertTrue(observation.tsc_ran)
        self.assertFalse(observation.green)

    def test_an_unreadable_source_file_is_skipped(self):
        path = self.write_app_file(
            "src/journeys.test.tsx", "".join("// {0}\n".format(n) for n in range(200))
        )
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o644)
        if os.access(str(path), os.R_OK):  # pragma: no cover - running as root
            self.skipTest("cannot make a file unreadable as this user")
        observation = self.observe()
        self.assertEqual(observation.over_limit, [])

    def test_a_seed_directory_that_does_not_exist(self):
        observation = self.observe(seed_dir=self.root / "absent")
        self.assertTrue(observation.files[observe_mod.CONFIG_FILE]["changed_from_seed"])

    def test_a_spec_of_the_wrong_shape_is_ignored(self):
        observation = self.observe(spec={"journeys": "not a list"})
        self.assertEqual(observation.coverage["total"], 0)


class ReportAdditionsTest(unittest.TestCase):
    """``compose_report``'s new ``status`` and the failed ``tests_run`` entries."""

    def _observation(self, **overrides) -> Dict[str, object]:
        base = {
            "green": False,
            "total": 2,
            "passed": 1,
            "failed": 1,
            "names": ["journeys > adds a record"],
            "failures": [{"name": "journeys > lends a book", "message": "boom"}],
        }
        base.update(overrides)
        return base

    def test_status_defaults_to_partial(self):
        payload = report.compose_report(None, self._observation(), "idea")
        self.assertEqual(payload["status"], "partial")

    def test_an_explicit_status_is_used(self):
        for status in report.VALID_STATUSES:
            payload = report.compose_report(None, self._observation(), "idea", status=status)
            self.assertEqual(payload["status"], status)

    def test_an_unknown_status_falls_back_to_partial(self):
        payload = report.compose_report(None, self._observation(), "idea", status="green")
        self.assertEqual(payload["status"], "partial")

    def test_failed_entries_follow_the_passed_ones_in_the_runner_shape(self):
        payload = report.compose_report(None, self._observation(), "idea", status="partial")
        self.assertEqual(
            payload["tests_run"],
            [
                {"command": "npm test", "journey": "journeys > adds a record", "result": "passed"},
                {"command": "npm test", "journey": "journeys > lends a book", "result": "failed"},
            ],
        )
        # Every entry survives the runner's own normalization.
        self.assertEqual(report.valid_tests_run(payload["tests_run"]), payload["tests_run"])

    def test_a_malformed_failure_entry_is_dropped(self):
        payload = report.compose_report(
            None,
            self._observation(failures=["junk", {"message": "no name"}, {"name": ""}]),
            "idea",
        )
        self.assertEqual([e["result"] for e in payload["tests_run"]], ["passed"])

    def test_write_report_passes_the_status_through(self):
        with tempfile.TemporaryDirectory(dir=str(support.scratch_root())) as tmp:
            app_dir = pathlib.Path(tmp)
            self.assertTrue(
                report.write_report(app_dir, None, self._observation(), "idea", status="success")
            )
            payload = json.loads((app_dir / "report.partial.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "success")

    def test_the_empty_observation_carries_an_empty_failures_list(self):
        empty = report.empty_observation()
        self.assertEqual(empty["failures"], [])
        empty["failures"].append("mutated")
        self.assertEqual(report.empty_observation()["failures"], [])


if __name__ == "__main__":
    unittest.main()
