"""End-to-end wiring tests for ``harness/__main__.py``: credentials/budget/
analyst ordering, the report watcher, the prefix check, and the budget gate's
refusal path.

Most tests here drive the real ``python3 -m harness`` CLI as a subprocess
through :mod:`harness.tests.support` (fake Pi, no network, no tokens spent).
Two things cannot be exercised that way and instead call
:func:`harness.__main__.run` in-process:

- the budget gate's refusal branch, because every subprocess test sets
  ``HARNESS_PI_BIN`` (support's fake Pi), and the gate is deliberately inert
  whenever that test double is in play -- see ``budget_gate_active``'s
  docstring -- so a real Pi binary would be the only other way to reach it;
- "direct client unavailable", because shadowing ``harness.gateway`` out of a
  subprocess's import path does not work: ``python -m harness`` always
  resolves the package from its own directory first (``sys.path[0]`` is the
  interpreter's cwd, ahead of anything in ``PYTHONPATH``), and that cwd is
  fixed to the repository root by ``support.spawn_harness``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import signal
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from harness.tests import support

REPO_ROOT = support.REPO_ROOT


def _copy_app_template(dest_parent: pathlib.Path) -> pathlib.Path:
    """A private, writable copy of ``app-template`` -- never write into the real one."""
    dest = dest_parent / "app"
    shutil.copytree(
        REPO_ROOT / "app-template",
        dest,
        ignore=shutil.ignore_patterns("node_modules"),
        symlinks=True,
    )
    # The stub vitest binary never needs a real node_modules; only report.observe's
    # default (unused when HARNESS_VITEST_BIN is set) would.
    return dest


def _write_vitest_stub(path: pathlib.Path, tests_run) -> None:
    """A stand-in for ``node_modules/.bin/vitest`` that writes a canned JSON report.

    ``tests_run`` is a list of full test names, all reported as passed.
    """
    assertion_results = [{"fullName": name, "status": "passed"} for name in tests_run]
    script = (
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
                "numTotalTests": len(tests_run),
                "numPassedTests": len(tests_run),
                "numFailedTests": 0,
                "testResults": [{"assertionResults": assertion_results}],
            }
        )
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _executable(path: pathlib.Path, script: str) -> pathlib.Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_tsc_stub(path: pathlib.Path, errors=(), marker=None) -> pathlib.Path:
    """A stand-in for ``node_modules/.bin/tsc``.

    With ``marker`` set, the stub is red on its **first** invocation only and
    green afterwards -- the marker file is how one process tells the next that
    the injected error has already been reported, since ``observe()`` spawns a
    fresh tsc every round. That is the "injected type error repaired within the
    cap" fixture: round 1 red, a Repairer mission, round 2 green.
    """
    return _executable(
        path,
        (
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "MARKER = {marker!r}\n"
            "ERRORS = {errors!r}\n"
            "red = bool(ERRORS)\n"
            "if red and MARKER:\n"
            "    if os.path.exists(MARKER):\n"
            "        red = False\n"
            "    else:\n"
            "        open(MARKER, 'w').close()\n"
            "if red:\n"
            "    sys.stdout.write('\\n'.join(ERRORS) + '\\n')\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        ).format(marker=str(marker) if marker else None, errors=list(errors)),
    )


def _write_vite_stub(path: pathlib.Path, ok: bool = True) -> pathlib.Path:
    """A stand-in for ``node_modules/.bin/vite``; only ``vite build`` is ever run."""
    return _executable(
        path,
        (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.write({0!r})\n"
            "sys.exit({1})\n"
        ).format(
            "vite v5.0.0 building for production...\n\N{CHECK MARK} built in 412ms\n"
            if ok
            else "error during build:\nRollupError: Could not resolve './missing.js'\n",
            0 if ok else 1,
        ),
    )


def _write_vitest_stub_mixed(path: pathlib.Path, passed, failed) -> pathlib.Path:
    """``_write_vitest_stub`` with failures: the shape ``observe()`` repairs against."""
    assertion_results = [{"fullName": name, "status": "passed"} for name in passed]
    assertion_results += [
        {
            "fullName": name,
            "status": "failed",
            "failureMessages": ["Unable to find an element with the text: " + name],
        }
        for name in failed
    ]
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
            "sys.exit(0 if not data['numFailedTests'] else 1)\n"
        ).format(
            json.dumps(
                {
                    "numTotalTests": len(assertion_results),
                    "numPassedTests": len(passed),
                    "numFailedTests": len(failed),
                    "testResults": [{"assertionResults": assertion_results}],
                }
            )
        ),
    )


def _events(stdout: bytes):
    """Every JSON record the harness forwarded on stdout, malformed lines dropped."""
    records = []
    for line in stdout.splitlines():
        try:
            records.append(json.loads(line.decode("utf-8")))
        except ValueError:
            continue
    return records


def _prompts(path: pathlib.Path):
    """``FAKE_PI_PROMPT_LOG``'s records, in the order the fake Pi appended them."""
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _plan_titles():
    """The journey titles ``derive_plan`` produces for the scripted spec.

    Read through the real normaliser rather than the fixture's raw JSON: the
    vitest stub has to report the titles the Tester was asked for, and those
    are the *normalised* ones (deduped, stripped) that end up in ``plan.json``.
    """
    from harness.analyst import normalize_spec
    from harness.plan import derive_plan

    raw = json.loads((support.TESTS_DIR / "fixtures" / "spec-books.json").read_text(encoding="utf-8"))
    return [test["title"] for test in derive_plan(normalize_spec(raw))["tests"]]


class SubprocessWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"

    def tearDown(self):
        self._tmp.cleanup()

    # -- credentials / direct-client logging --------------------------------

    def test_no_credentials_in_the_environment_logs_none_found(self):
        # support.harness_environment() already pops BERGET_API_KEY etc.
        _, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000)
        self.assertIn("credentials · none found", stderr)

    def test_harness_direct_0_disables_the_direct_client(self):
        _, _, stderr = support.run_harness(
            self.run_dir, timeout_ms=60_000, env_extra={"HARNESS_DIRECT": "0"}
        )
        self.assertIn("direct client disabled (HARNESS_DIRECT=0)", stderr)
        self.assertNotIn("analyst ·", stderr)

    def test_analyst_deadline_is_bounded_not_the_full_run_budget(self):
        """A slow/hung gateway must not eat the run's wall-clock budget before
        the Builder mission is even attempted (harness-semantics finding
        "analyst-unbounded-deadline"). The Analyst's own deadline is derived
        as a small, fixed slice of the run's budget; with a gateway response
        scripted slower than that slice, the call must give up long before
        the (deliberately short) ``--timeout-ms`` -- not block for the full
        scripted delay, which is what the old, unbounded ``client.json_schema``
        call (no ``deadline`` argument at all) would have done.
        """
        from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

        with FakeGatewayServer() as server:
            server.script(
                [ScriptedResponse(status=200, body=ok_response(content="{}"), delay=15.0)]
            )
            started = time.monotonic()
            code, _, stderr = support.run_harness(
                self.run_dir,
                timeout_ms=8_000,
                env_extra={
                    "BERGET_API_KEY": "fake-test-key",
                    "HARNESS_GATEWAY_URL": server.base_url,
                },
                wait_s=15.0,
            )
            elapsed = time.monotonic() - started

        # The old, unbounded behaviour would have blocked for the full 15s
        # scripted gateway delay -- well past the 8s --timeout-ms -- before
        # the Builder mission was even attempted. The bounded deadline must
        # make the Analyst give up long before that, and the fake-Pi-backed
        # Builder mission (fast, unaffected by the gateway) still completes.
        self.assertLess(elapsed, 10.0, stderr)
        self.assertEqual(server.requests, [], stderr)
        self.assertIn("analyst ·", stderr)
        self.assertIn("no spec (continuing without one)", stderr)
        self.assertEqual(code, 0, stderr)

    # -- report + budget + prefix, end to end --------------------------------

    def test_green_tests_produce_a_report_and_a_budget_snapshot(self):
        app_dir = _copy_app_template(self.root)
        stub = self.root / "vitest-stub.py"
        _write_vitest_stub(stub, ["creates a record", "lists records", "filters records"])

        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=60_000,
            cwd=app_dir,
            env_extra={
                "FAKE_PI_GREEN_TESTS": "1",
                "FAKE_PI_OUTPUT_TOKENS": "777",
                "HARNESS_VITEST_BIN": str(stub),
            },
        )
        self.assertEqual(code, 0, stderr)

        report_path = app_dir / "report.partial.json"
        self.assertTrue(
            support.wait_for(lambda: report_path.is_file(), timeout=10.0),
            "report.partial.json was never written",
        )
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "partial")
        journeys = {entry["journey"] for entry in payload["tests_run"]}
        self.assertEqual(journeys, {"creates a record", "lists records", "filters records"})
        for entry in payload["tests_run"]:
            self.assertEqual(entry["command"], "npm test")
            self.assertEqual(entry["result"], "passed")

        budget_path = self.run_dir / "harness" / "budget.json"
        self.assertTrue(budget_path.is_file())
        snapshot = json.loads(budget_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["peak_output"], 777)
        self.assertEqual(snapshot["cumulative_output"], 777)
        labels = [p["label"] for p in snapshot["predictions"]]
        self.assertIn("builder", labels)
        builder = next(p for p in snapshot["predictions"] if p["label"] == "builder")
        self.assertEqual(builder["actual_output"], 777)
        self.assertIn("budget ·", stderr)

    def test_a_model_written_report_survives_a_green_observation(self):
        # Measured 2026-09-03 (run 2026-09-03T16-18-04-403Z): the model wrote its
        # `success` report, then re-ran its tests; the green re-run triggered an
        # observation that overwrote the model's report with a harness `partial`.
        # The fake Pi writes its report at startup, then reports green tests, so
        # the watcher's observation lands after the "model" wrote -- and must lose.
        app_dir = _copy_app_template(self.root)
        stub = self.root / "vitest-stub.py"
        _write_vitest_stub(stub, ["creates a record"])

        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=60_000,
            cwd=app_dir,
            env_extra={
                "FAKE_PI_WRITE_REPORT": "1",
                "FAKE_PI_GREEN_TESTS": "1",
                "HARNESS_VITEST_BIN": str(stub),
            },
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads((app_dir / "report.partial.json").read_text(encoding="utf-8"))
        # The model's prose survives the green observation ...
        self.assertEqual(payload["summary"], "fake pi report", stderr)
        self.assertIn("written by the model; leaving it untouched", stderr)
        # ... and its empty tests_run is repaired from the vitest JSON at shutdown.
        self.assertEqual([e["journey"] for e in payload["tests_run"]], ["creates a record"], stderr)
        self.assertIn("repaired tests_run from vitest (1 entries)", stderr)

    def test_a_run_with_no_green_tests_still_writes_budget_json_but_no_report(self):
        app_dir = _copy_app_template(self.root)
        code, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000, cwd=app_dir)
        self.assertEqual(code, 0, stderr)
        self.assertFalse((app_dir / "report.partial.json").is_file())
        self.assertTrue((self.run_dir / "harness" / "budget.json").is_file())

    def test_prefix_check_reads_a_preseeded_payload_log_and_warns_on_drift(self):
        harness_dir = self.run_dir / "harness"
        harness_dir.mkdir(parents=True)
        payload_log = harness_dir / "payload.jsonl"
        with payload_log.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"tools": 4, "system_sha256": "aaa"}) + "\n")
            handle.write(json.dumps({"tools": 4, "system_sha256": "bbb"}) + "\n")

        code, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000)
        self.assertEqual(code, 0, stderr)
        self.assertIn("prefix · tools=4: 2 distinct system prompt hash(es)", stderr)

    def test_prefix_check_is_silent_when_no_payload_log_exists(self):
        code, _, stderr = support.run_harness(self.run_dir, timeout_ms=60_000)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("prefix ·", stderr)

    def test_internal_deadline_expiry_still_reaches_report_and_budget(self):
        # No OS signal is sent here (only the harness's own internal deadline
        # fires) -- that fast SIGTERM path is covered in test_lifecycle.py.
        # This only needs report finalization and the budget snapshot to run
        # to completion rather than being skipped or crashing.
        code, _, stderr = support.run_harness(
            self.run_dir, timeout_ms=1_000, env_extra={"FAKE_PI_HANG": "1"}, wait_s=90.0
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn("budget ·", stderr)
        self.assertTrue((self.run_dir / "harness" / "budget.json").is_file())


class BudgetGateRefusalTest(unittest.TestCase):
    """The ``can_start(12000)`` refusal path (C6): real code, forced active.

    Every subprocess test above always sets ``HARNESS_PI_BIN`` (support's fake
    Pi), which deliberately makes ``budget_gate_active()`` return ``False`` --
    the wall-clock prediction means nothing against a test double that answers
    instantly. So this exercises the gate the only way that is both realistic
    and fast: call ``harness.__main__.run`` in-process with the gate forced on
    and a deadline no real mission could meet.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_refuses_and_exits_1_when_the_gate_blocks_the_builder_mission(self):
        from harness import __main__ as main_mod

        session_root = self.root / "sessions"
        env = {
            "HARNESS_PI_BIN": str(support.FAKE_PI),
            "HARNESS_DIRECT": "0",
            "BERGET_API_KEY": "",
            "CHALLENGE_API_KEY": "",
            "OPENAI_API_KEY": "",
            "HARNESS_COLOR": "",
        }
        args = main_mod.build_parser().parse_args(
            [
                "--idea-file", str(REPO_ROOT / "contract-public" / "development-idea.txt"),
                "--session-root", str(session_root),
                "--cwd", str(REPO_ROOT / "app-template"),
                "--timeout-ms", "1000",  # 1s: no real mission's predicted finish fits
                "--repo-root", str(REPO_ROOT),
            ]
        )
        stderr = io.StringIO()
        try:
            with mock.patch.object(main_mod, "budget_gate_active", return_value=True):
                with mock.patch.dict(os.environ, env):
                    with contextlib.redirect_stderr(stderr):
                        code = main_mod.run(args)
        finally:
            main_mod.close_file_sink()

        self.assertEqual(code, main_mod.EXIT_FAILURE)
        self.assertIn("budget · cannot start Builder mission", stderr.getvalue())
        # No session directory: the mission must never have been spawned.
        self.assertFalse((session_root / main_mod.SESSION_LABEL).is_dir())

    def test_budget_gate_reason_is_none_when_the_mission_comfortably_fits(self):
        from harness import __main__ as main_mod
        from harness.budget import BudgetController
        import time as time_mod

        controller = BudgetController(deadline_monotonic=time_mod.monotonic() + 900.0)
        self.assertIsNone(
            main_mod.budget_gate_reason(controller, main_mod.BUILDER_PREDICTED_OUTPUT_TOKENS)
        )


class MissingDirectClientTest(unittest.TestCase):
    """``gateway``/``credentials``/``analyst`` are owned by a different part of
    this build and may not exist yet while ``__main__.py`` is developed against
    them; harness/__main__.py imports them lazily and must degrade cleanly.

    Now that this checkout has all three, the only way left to exercise that
    branch is to shadow them out of ``sys.modules`` (the documented way to make
    ``import x`` raise ``ImportError`` for one specific name) and reload the
    module in-process -- a real subprocess can't be shadowed this way because
    ``python -m harness`` always resolves the package from its own directory
    first, ahead of anything in ``PYTHONPATH``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_gateway_modules_are_logged_once_and_do_not_crash_the_run(self):
        import importlib

        import harness as harness_pkg
        from harness import __main__ as main_mod

        # ``from . import credentials`` resolves an already-imported submodule by
        # first checking it as an *attribute* of the ``harness`` package object,
        # only falling back to ``sys.modules`` (what ``shadow`` below controls)
        # when that attribute is absent. Test files that ran before this one in
        # discovery order (test_credentials.py, test_gateway.py) already set
        # those attributes by importing them for real, so both have to be
        # hidden for the reload below to actually see an ImportError.
        submodule_names = ("gateway", "credentials", "analyst")
        saved_attrs = {
            name: getattr(harness_pkg, name) for name in submodule_names if hasattr(harness_pkg, name)
        }
        for name in saved_attrs:
            delattr(harness_pkg, name)
        shadow = {"harness." + name: None for name in submodule_names}

        try:
            with mock.patch.dict(sys.modules, shadow):
                reloaded = importlib.reload(main_mod)
                try:
                    self.assertFalse(reloaded.DIRECT_CLIENT_AVAILABLE)

                    session_root = self.root / "sessions"
                    args = reloaded.build_parser().parse_args(
                        [
                            "--idea-file", str(REPO_ROOT / "contract-public" / "development-idea.txt"),
                            "--session-root", str(session_root),
                            "--cwd", str(REPO_ROOT / "app-template"),
                            "--timeout-ms", "60000",
                            "--repo-root", str(REPO_ROOT),
                        ]
                    )
                    env = {
                        "HARNESS_PI_BIN": str(support.FAKE_PI),
                        "BERGET_API_KEY": "",
                        "CHALLENGE_API_KEY": "",
                        "OPENAI_API_KEY": "",
                    }
                    stderr = io.StringIO()
                    with mock.patch.dict(os.environ, env):
                        with contextlib.redirect_stderr(stderr):
                            code = reloaded.run(args)
                    self.assertEqual(code, reloaded.EXIT_SUCCESS)
                    self.assertIn("direct client unavailable", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())
                finally:
                    reloaded.close_file_sink()
        finally:
            for name, value in saved_attrs.items():
                setattr(harness_pkg, name, value)
            # Reload once more so every other test in this process (and every
            # test module that imports harness.__main__ afterward) sees the
            # real thing again, not the shadowed reload's frozen snapshot.
            importlib.reload(main_mod)


if __name__ == "__main__":
    unittest.main()
