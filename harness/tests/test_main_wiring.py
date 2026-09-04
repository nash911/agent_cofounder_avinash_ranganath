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

:class:`MissionsModeTest` adds Phase 3's other body (PHASE3_DESIGN.md §7). It
needs one more test double than the single-session tests: a spec, which only
the Analyst can produce, so an in-process :class:`~harness.tests.fake_gateway.
FakeGatewayServer` scripted with ``fixtures/spec-books.json`` stands in for the
provider while ``HARNESS_TSC_BIN``/``HARNESS_VITEST_BIN``/``HARNESS_VITE_BIN``
stand in for the three commands ``observe()`` runs. Nothing here touches the
network or spends a token: the gateway is a loopback socket owned by the test.
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


class MissionsModeTest(unittest.TestCase):
    """Missions mode end to end (§7): Analyst → plan → Builder ∥ Tester → loop.

    Every run here is the real CLI in a subprocess with four doubles: the fake
    Pi (missions), the fake gateway (the Analyst's spec and the model
    Supervisor's second opinion), and stub tsc/vitest/vite binaries
    (``observe()``). The app directory is a private copy of ``app-template``,
    so ``changed_from_seed`` and the 150-line check compare against the real
    scaffold without ever writing into it.
    """

    #: The one journey the vitest stub reports as failing in the no-progress
    #: fixture; its name is half of the observation's signature, so keeping it
    #: identical round after round is what "no progress" *means*.
    STUCK_JOURNEY = "Lend a book"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = _copy_app_template(self.root)
        self.prompt_log = self.root / "prompts.jsonl"
        self.server = None

        # What the fake Builder and the fake Tester "write". The config is the
        # seed plus one line: byte-identical would read as `changed_from_seed`
        # false and earn the Builder a rerun instead of a verdict.
        seed_config = (REPO_ROOT / "app-template" / "src" / "app-config.ts").read_text(encoding="utf-8")
        self.config_source = self.root / "written-config.ts"
        self.config_source.write_text(seed_config + "\n// written by the fake Builder\n", encoding="utf-8")
        self.tests_source = self.root / "written-tests.tsx"
        self.tests_source.write_text(
            (REPO_ROOT / "app-template" / "src" / "test" / "journeys.template.tsx").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        self.tsc = self.root / "tsc-stub.py"
        self.vitest = self.root / "vitest-stub.py"
        self.vite = self.root / "vite-stub.py"
        _write_tsc_stub(self.tsc)
        _write_vitest_stub(self.vitest, _plan_titles())
        _write_vite_stub(self.vite)

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def _gateway(self, spare: int = 20):
        """The scripted provider: one spec, then replies no Supervisor accepts.

        The spare replies are valid JSON that fails the decision schema, which
        is exactly the "any failure → fall through to the policy" path §6
        specifies -- so a stalled run still terminates deterministically
        instead of hanging on an empty queue.
        """
        from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

        spec_text = (support.TESTS_DIR / "fixtures" / "spec-books.json").read_text(encoding="utf-8")
        server = FakeGatewayServer()
        server.script(
            [ScriptedResponse(status=200, body=ok_response(content=spec_text, completion_tokens=800))]
            + [ScriptedResponse(status=200, body=ok_response(content="{}")) for _ in range(spare)]
        )
        server.start()
        self.server = server
        return server

    def _env(self, **extra):
        env = {
            "BERGET_API_KEY": "fake-test-key",
            "HARNESS_GATEWAY_URL": self.server.base_url,
            "FAKE_PI_PROMPT_LOG": str(self.prompt_log),
            "FAKE_PI_WRITE_ON_PROMPT": "src/app-config.ts={0};src/journeys.test.tsx={1}".format(
                self.config_source, self.tests_source
            ),
            "HARNESS_TSC_BIN": str(self.tsc),
            "HARNESS_VITEST_BIN": str(self.vitest),
            "HARNESS_VITE_BIN": str(self.vite),
        }
        env.update(extra)
        return env

    def _run(self, timeout_ms: int = 300_000, wait_s: float = 120.0, **extra):
        # The default transport is "combined" (2026-09-04); these tests pin the
        # per-mission wiring unless a test asks for another mode explicitly.
        extra.setdefault("HARNESS_SESSION_MODE", "per-mission")
        self._gateway()
        return support.run_harness(
            self.run_dir,
            timeout_ms=timeout_ms,
            cwd=self.app_dir,
            env_extra=self._env(**extra),
            wait_s=wait_s,
        )

    def _harness_json(self, name: str):
        return json.loads((self.run_dir / "harness" / name).read_text(encoding="utf-8"))

    def _report(self):
        return json.loads((self.app_dir / "report.partial.json").read_text(encoding="utf-8"))

    def _sessions(self):
        root = self.run_dir / "sessions"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    # -- the happy path ----------------------------------------------------

    def test_missions_mode_runs_builder_and_tester_and_reports_success(self):
        code, stdout, stderr = self._run()
        self.assertEqual(code, 0, stderr)
        self.assertIn("mode · missions", stderr)

        # Two sessions, one per mission, numbered in start order.
        self.assertEqual(self._sessions(), ["1-builder", "2-tester"], stderr)

        # Both files exist and the config is no longer the seed.
        config = self.app_dir / "src" / "app-config.ts"
        self.assertTrue((self.app_dir / "src" / "journeys.test.tsx").is_file(), stderr)
        self.assertNotEqual(
            config.read_bytes(),
            (REPO_ROOT / "app-template" / "src" / "app-config.ts").read_bytes(),
        )

        # The harness authored the report itself: no mission was asked for one.
        report = self._report()
        self.assertEqual(report["status"], "success", report)
        self.assertEqual(
            {entry["journey"] for entry in report["tests_run"]}, set(_plan_titles()), report
        )
        self.assertTrue(all(e["result"] == "passed" for e in report["tests_run"]), report)
        # `summary` is the Analyst's; features and assumptions are the plan's
        # (the v2 spec carries neither).
        self.assertIn("borrower", report["summary"])
        self.assertTrue(report["implemented_features"], report)

        for name in ("spec.json", "plan.json", "supervisor.json", "missions.json", "budget.json"):
            self.assertTrue((self.run_dir / "harness" / name).is_file(), name)

        supervisor = self._harness_json("supervisor.json")
        self.assertEqual(supervisor["final_action"], "done", supervisor)
        self.assertEqual(supervisor["repairs"], 0, supervisor)
        self.assertEqual(supervisor["model_calls"], 0, supervisor)

        missions = self._harness_json("missions.json")
        self.assertEqual(missions["session_mode"], "per-mission")
        self.assertEqual([m["role"] for m in missions["missions"]], ["builder", "tester"])
        self.assertTrue(all(m["success"] for m in missions["missions"]), missions)

        # events.jsonl carries both mission sessions' assistant turns plus the
        # Analyst's one synthetic direct-gateway record (C1).
        records = _events(stdout)
        assistant = [
            r for r in records
            if r.get("type") == "message_end"
            and isinstance(r.get("message"), dict)
            and r["message"].get("role") == "assistant"
        ]
        direct = [r for r in assistant if r["message"].get("source") == "direct-gateway"]
        piped = [r for r in assistant if r["message"].get("provider") == "fake-provider"]
        self.assertEqual(len(direct), 1, stderr)
        self.assertEqual(len(piped), 2, stderr)

    def test_the_analyst_is_given_the_public_journeys_coverage_checklist(self):
        # §3: journeys.md's "Behaviors to implement and test when implied" list
        # is the Analyst's checklist now -- it is the only place in missions
        # mode that guidance is paid for, and if it never arrives the spec
        # silently loses the implied journeys the runner grades on.
        self._run()
        analyst = self.server.requests[0]["body"]
        system = analyst["messages"][0]["content"]
        self.assertIn("Coverage checklist", system)
        self.assertIn("Preserve required data across a browser refresh.", system)
        self.assertEqual(analyst["messages"][1]["role"], "user")

    def test_each_mission_gets_its_own_brief_and_the_missions_tool_set(self):
        _, _, stderr = self._run()
        prompts = _prompts(self.prompt_log)
        self.assertEqual(len(prompts), 2, prompts)
        self.assertEqual(len({entry["pid"] for entry in prompts}), 2, "missions shared a session")

        by_file = {}
        for entry in prompts:
            self.assertIn("--tools", entry["argv"])
            self.assertEqual(entry["argv"][entry["argv"].index("--tools") + 1], "read,write,edit")
            self.assertNotIn("--skill", entry["argv"])  # §9: never a skill in missions mode
            by_file[entry["text"].splitlines()[0]] = entry["text"]

        builder = next(text for head, text in by_file.items() if "app-config.ts" in head)
        tester = next(text for head, text in by_file.items() if "journeys.test.tsx" in head)
        # The Builder never sees the journey list and the Tester never sees the
        # config outline: that split is what keeps both briefs ~700 tokens.
        self.assertIn("The application, as data", builder)
        self.assertNotIn("One `it` per journey", builder)
        self.assertIn("Journeys, one `it` each", tester)

    # -- the repair loop ---------------------------------------------------

    def test_an_injected_type_error_is_repaired_within_the_cap(self):
        _write_tsc_stub(
            self.tsc,
            errors=[
                "src/app-config.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'."
            ],
            marker=self.root / "tsc-was-red",
        )
        code, _, stderr = self._run()
        self.assertEqual(code, 0, stderr)

        self.assertEqual(self._sessions(), ["1-builder", "2-tester", "3-repairer"], stderr)
        supervisor = self._harness_json("supervisor.json")
        self.assertEqual(supervisor["repairs"], 1, supervisor)
        self.assertLessEqual(supervisor["repairs"], supervisor["repair_cap"])
        self.assertEqual(supervisor["final_action"], "done", supervisor)
        self.assertEqual(self._report()["status"], "success")

        # The Repairer's brief quotes the compiler's own line, not a paraphrase.
        repair = _prompts(self.prompt_log)[-1]["text"]
        self.assertIn("error TS2322", repair)
        self.assertIn("attempt 1 of 3", repair)

    def test_two_rounds_without_progress_stop_the_loop_and_report_partial(self):
        titles = _plan_titles()
        _write_vitest_stub_mixed(
            self.vitest, [t for t in titles if t != titles[0]], [self.STUCK_JOURNEY]
        )
        code, _, stderr = self._run()
        self.assertEqual(code, 0, stderr)

        supervisor = self._harness_json("supervisor.json")
        self.assertEqual(supervisor["final_action"], "stop", supervisor)
        self.assertIn("no progress", supervisor["final_rationale"], supervisor)
        self.assertEqual(supervisor["no_progress"], supervisor["no_progress_cap"], supervisor)
        self.assertLessEqual(supervisor["repairs"], supervisor["repair_cap"], supervisor)
        # Rule 6 buys exactly one second opinion before the cap stops the loop.
        self.assertGreaterEqual(supervisor["model_calls"], 1, supervisor)

        report = self._report()
        self.assertEqual(report["status"], "partial", report)
        failed = [e for e in report["tests_run"] if e["result"] == "failed"]
        self.assertEqual([e["journey"] for e in failed], [self.STUCK_JOURNEY], report)

    # -- the session-strategy flag -----------------------------------------

    def test_session_mode_single_sends_every_mission_into_one_session(self):
        _write_tsc_stub(
            self.tsc,
            errors=["src/app-config.ts(3,1): error TS2304: Cannot find name 'nope'."],
            marker=self.root / "tsc-was-red",
        )
        code, _, stderr = self._run(HARNESS_SESSION_MODE="single")
        self.assertEqual(code, 0, stderr)

        self.assertEqual(self._sessions(), ["1-agent"], stderr)
        prompts = _prompts(self.prompt_log)
        self.assertEqual(len(prompts), 3, [p["text"][:40] for p in prompts])
        self.assertEqual(len({entry["pid"] for entry in prompts}), 1, "more than one session")
        self.assertEqual([entry["n"] for entry in prompts], [1, 2, 3])
        self.assertEqual(self._harness_json("missions.json")["session_mode"], "single")

    def test_session_mode_combined_sends_both_missions_in_one_prompt(self):
        # 2026-09-04: measured single mode at 14.6k points vs 21.6k per-mission;
        # "combined" folds the Builder and Tester briefs into one prompt so the
        # session pays one closing turn and one brief instead of two.
        code, _, stderr = self._run(HARNESS_SESSION_MODE="combined")
        self.assertEqual(code, 0, stderr)

        self.assertEqual(self._sessions(), ["1-combined"], stderr)
        prompts = _prompts(self.prompt_log)
        self.assertEqual(len(prompts), 1, [p["text"][:40] for p in prompts])
        argv = prompts[0].get("argv") or []
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "write", argv)
        text = prompts[0]["text"]
        self.assertIn("### Part 1 -- `src/app-config.ts`", text)
        self.assertIn("### Part 2 -- `src/journeys.test.tsx`", text)
        self.assertIn("Exactly two tool calls", text)
        self.assertNotIn("first and only tool call", text)
        self.assertEqual(self._harness_json("missions.json")["session_mode"], "combined")
        report = json.loads((self.app_dir / "report.partial.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "success", report)

    # -- the fallbacks -----------------------------------------------------

    def test_no_api_key_falls_back_to_the_single_session_path(self):
        # No gateway, no key: the Analyst never runs, so there is no spec and
        # missions mode has no contract to hand a Builder.
        code, _, stderr = support.run_harness(
            self.run_dir, timeout_ms=60_000, cwd=self.app_dir
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("mode · single (no usable spec)", stderr)
        self.assertEqual(self._sessions(), ["1-builder"], stderr)
        self.assertFalse((self.run_dir / "harness" / "plan.json").is_file())

    def test_harness_mode_single_forces_the_phase_2_path_even_with_a_spec(self):
        # HARNESS_MODE=single reserves the single session's own 12,000 output
        # tokens after the Analyst, so the budget has to be a judged-run one.
        code, _, stderr = self._run(timeout_ms=900_000, HARNESS_MODE="single")
        self.assertEqual(code, 0, stderr)
        self.assertIn("mode · single (HARNESS_MODE=single)", stderr)
        self.assertTrue((self.run_dir / "harness" / "spec.json").is_file(), stderr)
        self.assertEqual(self._sessions(), ["1-builder"], stderr)
        self.assertFalse((self.run_dir / "harness" / "plan.json").is_file(), stderr)
        # The harness-authored fallback report still carries the features the
        # v2 spec dropped: the single path derives the plan for them too.
        self.assertTrue(self._report()["implemented_features"], self._report())

    # -- shutdown ----------------------------------------------------------

    def test_sigterm_during_the_parallel_phase_exits_within_five_seconds(self):
        self._gateway()
        process = support.spawn_harness(
            self.run_dir,
            timeout_ms=600_000,
            cwd=self.app_dir,
            env_extra=self._env(FAKE_PI_SETTLE_DELAY="30", HARNESS_SESSION_MODE="per-mission"),
        )
        try:
            # Both missions are in flight once both prompts have been logged
            # (the fake Pi logs before it sleeps out its settle delay).
            self.assertTrue(
                support.wait_for(lambda: len(_prompts(self.prompt_log)) >= 2, timeout=60.0),
                "both missions never started",
            )
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=10.0)
            elapsed = time.monotonic() - started
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.assertLess(elapsed, 5.0, "shutdown took {0:.1f}s".format(elapsed))
        self.assertIn(code, (0, 1))
        stderr = (self.run_dir / "harness.stderr.log").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("Traceback", stderr)

    def test_sigterm_during_an_observation_exits_within_five_seconds(self):
        # Measured before observe() was stop-aware: a SIGTERM one second into
        # an observation whose tsc slept 12 s took 11.05 s to shut down -- past
        # the runner's SIGTERM-to-SIGKILL grace, so supervisor.json,
        # missions.json and budget.json were never written.
        marker = self.root / "tsc-started"
        _executable(
            self.tsc,
            "#!/usr/bin/env python3\n"
            "import time\n"
            "open({marker!r}, 'w').close()\n"
            "time.sleep(30)\n".format(marker=str(marker)),
        )
        self._gateway()
        process = support.spawn_harness(
            self.run_dir, timeout_ms=600_000, cwd=self.app_dir, env_extra=self._env()
        )
        try:
            self.assertTrue(
                support.wait_for(marker.is_file, timeout=90.0), "the observation never started"
            )
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=10.0)
            elapsed = time.monotonic() - started
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.assertLess(elapsed, 5.0, "shutdown took {0:.1f}s".format(elapsed))
        self.assertIn(code, (0, 1))
        for name in ("supervisor.json", "missions.json", "budget.json"):
            self.assertTrue((self.run_dir / "harness" / name).is_file(), name)


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
