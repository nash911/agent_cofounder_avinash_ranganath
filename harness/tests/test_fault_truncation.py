"""Fault injection (BUILD_PLAN Phase 5): a ``max_tokens`` truncation.

WHY this exists
---------------
When a judged model is run with a low ``maxTokens`` (dev ``models.json``), a
mission's write can be cut off mid-expression: the model streams the first
part of ``src/app-config.ts`` and then simply stops, leaving a syntactically
broken file -- unbalanced braces, an unterminated string, no closing ``});``.
That is not a hypothetical: it is exactly what a truncated stream leaves on
disk, and it is the single most likely way a real run produces a red app.

The harness's contract in that situation (PHASE3_DESIGN §4/§6/§7.5, and
``observe``/``supervisor``/``loop``'s own docstrings) is *degrade, never
crash*:

- ``observe()`` must run ``tsc``, see a non-zero exit with a real
  ``path(line,col): error TSxxxx:`` line, and record ``tsc_ran=True,
  tsc_ok=False`` with the error captured -- **not** raise, and **not** report
  the app as green;
- the Supervisor must turn that red typecheck into a *repair* mission (rule 8),
  the fast path where vitest is skipped;
- whether or not the repair lands, the run must still write
  ``report.partial.json`` and exit ``0`` iff some mission had a usable turn,
  with a status that is a real, valid status -- ``partial``/``failed`` when the
  app never went green, and never a fabricated ``success``.

WHAT would fail if the handling were removed
--------------------------------------------
- If ``observe._run_tsc`` did not treat a non-zero ``tsc`` exit as a red-but-
  survivable typecheck (e.g. it raised, or reported ``tsc_ran=False``), the
  direct ``observe()`` check below would raise or see ``tsc_ok`` truthy, and
  the end-to-end runs would crash instead of exiting 0 with a report.
- If the Supervisor did not issue a repair on a red ``tsc``, the
  ``3-repairer`` session would never appear (case 1) and no repair loop would
  run (case 2).
- If ``loop._write_final_report`` only wrote on green, ``report.partial.json``
  would be absent after a run that stayed red (case 2).
- If ``final_status``/``green`` ever fabricated ``success`` for a red app,
  case 2's ``status != "success"`` assertion would fail.

Every subprocess is bounded (``run_harness``'s ``wait_s``); the fake gateway
binds an OS-assigned loopback port (never 3000); no real key, model or network
is ever touched. The broken source and the ``tsc`` stub are built at runtime in
a ``TemporaryDirectory`` -- nothing is written into the tree.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from harness.tests import support

REPO_ROOT = support.REPO_ROOT
CONFIG_REL = "src/app-config.ts"
TESTS_REL = "src/journeys.test.tsx"


# -- small helpers (kept local so this file does not depend on a sibling test
#    module another agent may be editing concurrently) -----------------------


def _executable(path: pathlib.Path, script: str) -> pathlib.Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _copy_app_template(dest_parent: pathlib.Path) -> pathlib.Path:
    """A private, writable copy of ``app-template`` -- never write into the real one."""
    dest = dest_parent / "app"
    shutil.copytree(
        REPO_ROOT / "app-template",
        dest,
        ignore=shutil.ignore_patterns("node_modules"),
        symlinks=True,
    )
    return dest


def _write_vitest_stub(path: pathlib.Path, tests_run) -> pathlib.Path:
    """A stand-in for vitest that writes a canned all-passed JSON report.

    Only reached once ``tsc`` is green (a red typecheck skips vitest), so in the
    "repair lands" case it is what lets the run finish green.
    """
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
                    "numTotalTests": len(tests_run),
                    "numPassedTests": len(tests_run),
                    "numFailedTests": 0,
                    "testResults": [{"assertionResults": assertion_results}],
                }
            )
        ),
    )


def _write_vite_stub(path: pathlib.Path) -> pathlib.Path:
    """A stand-in for ``vite build`` that always succeeds (only ``build`` is run)."""
    return _executable(
        path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('vite v5.0.0 building for production...\\nbuilt in 1ms\\n')\n"
        "sys.exit(0)\n",
    )


def _write_truncation_tsc_stub(path: pathlib.Path, *, marker=None) -> pathlib.Path:
    """A ``tsc`` stand-in that is RED exactly while ``src/app-config.ts`` is broken.

    "Broken" is what a ``max_tokens`` truncation leaves: unbalanced brackets or
    an unterminated string. The stub reads the config and, if it does not
    balance, prints one real ``src/app-config.ts(NN,C): error TSxxxx:`` line
    (the shape ``observe.parse_tsc_errors`` captures) and exits non-zero.

    Keying redness on the file's own content -- not on a blind first-invocation
    flip -- is what makes the truncated body *load-bearing*: hand this stub a
    valid config and it is green from the start, so the whole test genuinely
    depends on the injected fault.

    ``marker`` stands in for "the repair fixed it": with it set the stub is red
    on the first broken observation only and green afterwards (the marker file
    is how one ``tsc`` process tells the next, exactly as
    ``MissionsModeTest._write_tsc_stub`` does). With ``marker=None`` it stays
    red for as long as the file stays broken -- the repair-never-lands case.
    """
    header = "MARKER = {0!r}\nCONFIG = {1!r}\n".format(
        str(marker) if marker else None, CONFIG_REL
    )
    # Built by concatenation (never ``.format``ed) so the literal ``{`` / ``}``
    # inside the balance check need no escaping; ``chr(34)``/``chr(10)`` stand
    # in for the double-quote and newline to keep the source quote-clean.
    body = (
        "import os, sys\n"
        "try:\n"
        "    text = open(CONFIG, encoding='utf-8').read()\n"
        "except OSError:\n"
        "    text = ''\n"
        "opens = text.count('{') + text.count('(') + text.count('[')\n"
        "closes = text.count('}') + text.count(')') + text.count(']')\n"
        "broken = bool(text) and (opens != closes or text.count(chr(34)) % 2 != 0)\n"
        "red = broken\n"
        "if red and MARKER:\n"
        "    if os.path.exists(MARKER):\n"
        "        red = False\n"
        "    else:\n"
        "        open(MARKER, 'w').close()\n"
        "if red:\n"
        "    nlines = text.count(chr(10)) + 1\n"
        "    sys.stdout.write(CONFIG + '(' + str(nlines) + ',1): "
        "error TS1005: expression truncated (max_tokens)' + chr(10))\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )
    return _executable(path, "#!/usr/bin/env python3\n" + header + body)


def _broken_config_text() -> str:
    """A real ``app-config.ts`` body cut off mid-expression.

    Take the measured seed, keep everything up to ``fields:``, then append a
    field object that stops in the middle of a string literal -- so the file
    has unbalanced ``(``/``{``/``[`` and an unterminated ``"``: unmistakably
    what a truncated stream leaves behind, and different from the seed (so
    ``changed_from_seed`` is true and the Builder is not asked to rerun).
    """
    seed = (REPO_ROOT / "app-template" / "src" / "app-config.ts").read_text(encoding="utf-8")
    head = seed[: seed.index("fields:")]
    return (
        head
        + "fields: [\n"
        + '    { kind: "select", name: "category", label: "Category", required: true,\n'
        + '      options: ["Type A", "Type B", "Type C'
    )


def _plan_titles():
    """The normalised journey titles ``derive_plan`` produces for the scripted spec.

    The vitest stub must report the titles the Tester was asked for, and those
    are the normalised ones that end up in ``plan.json`` -- so the coverage
    check is complete and a green repaired run reaches ``success``.
    """
    from harness.analyst import normalize_spec
    from harness.plan import derive_plan

    raw = json.loads(
        (support.TESTS_DIR / "fixtures" / "spec-books.json").read_text(encoding="utf-8")
    )
    return [test["title"] for test in derive_plan(normalize_spec(raw))["tests"]]


class TruncationFaultTest(unittest.TestCase):
    """End-to-end: a truncated Builder write drives ``observe`` red, then repair.

    Modelled on ``test_main_wiring.MissionsModeTest`` -- the real
    ``python3 -m harness`` CLI in a subprocess, four doubles (fake Pi, fake
    gateway, stub tsc/vitest/vite), a private copy of ``app-template`` -- but
    the config the fake Builder "writes" is a truncated, unparseable file.
    ``per-mission`` session mode makes the repair its own, easy-to-assert
    ``N-repairer`` session directory.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = _copy_app_template(self.root)
        self.prompt_log = self.root / "prompts.jsonl"
        self.server = None

        # The fault: the Builder's write is a truncated app-config.ts. The
        # Tester's write is the real (valid) journeys template.
        self.broken_config = self.root / "broken-config.ts"
        self.broken_config.write_text(_broken_config_text(), encoding="utf-8")
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
        _write_vitest_stub(self.vitest, _plan_titles())
        _write_vite_stub(self.vite)

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def _gateway(self, spare: int = 20):
        """One scripted spec, then replies no Supervisor accepts (fall through)."""
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
            # When a prompt names src/app-config.ts, copy the BROKEN source onto
            # it; when it names the test file, copy the valid tests. Both the
            # Builder brief and every Repairer brief name the config, so the
            # config is (re)written truncated each time -- the repair can never
            # actually fix the content, which is precisely the stress the
            # graceful-degradation path must survive.
            "FAKE_PI_WRITE_ON_PROMPT": "{0}={1};{2}={3}".format(
                CONFIG_REL, self.broken_config, TESTS_REL, self.tests_source
            ),
            "HARNESS_TSC_BIN": str(self.tsc),
            "HARNESS_VITEST_BIN": str(self.vitest),
            "HARNESS_VITE_BIN": str(self.vite),
            # A separate session dir per mission so the repair is trivial to
            # assert (PHASE3_DESIGN §1).
            "HARNESS_SESSION_MODE": "per-mission",
        }
        env.update(extra)
        return env

    def _run(self, timeout_ms: int = 300_000, wait_s: float = 120.0, **extra):
        self._gateway()
        return support.run_harness(
            self.run_dir,
            timeout_ms=timeout_ms,
            cwd=self.app_dir,
            env_extra=self._env(**extra),
            wait_s=wait_s,
        )

    def _sessions(self):
        root = self.run_dir / "sessions"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def _report(self):
        return json.loads((self.app_dir / "report.partial.json").read_text(encoding="utf-8"))

    def _observation(self, index: int):
        return json.loads(
            (self.run_dir / "harness" / "observe-{0}.json".format(index)).read_text(encoding="utf-8")
        )

    def _supervisor(self):
        return json.loads(
            (self.run_dir / "harness" / "supervisor.json").read_text(encoding="utf-8")
        )

    # -- case 1: the harness sees the red app, repairs, and finishes -------

    def test_truncation_is_observed_repaired_and_reported(self):
        """Red-then-green ``tsc``: observe sees the truncation, a Repairer runs,
        the (simulated) fix lands, and a clean report is written -- exit 0."""
        # The marker flips tsc green after the first red observation: "the repair
        # fixed it". The config on disk stays truncated; the compiler is the
        # arbiter, exactly as MissionsModeTest's repaired-fault fixture.
        _write_truncation_tsc_stub(self.tsc, marker=self.root / "tsc-was-red")

        code, _, stderr = self._run()

        # Exit cleanly: a real assistant turn happened, so 0 -- not a crash.
        self.assertEqual(code, 0, stderr)

        # The truncated content actually landed on disk (the fault was induced).
        on_disk = (self.app_dir / "src" / "app-config.ts").read_text(encoding="utf-8")
        self.assertEqual(on_disk, _broken_config_text(), "the truncated write never landed")

        # observe saw tsc RED at least once (the first round), with the compiler
        # line captured -- not a crash, not a false-green. This is the assertion
        # that fails if observe stopped degrading a non-zero tsc gracefully.
        first = self._observation(1)
        self.assertTrue(first["tsc_ran"], first)
        self.assertFalse(first["tsc_ok"], first)
        self.assertTrue(
            any("app-config.ts" in e and "error TS" in e for e in first["tsc_errors"]),
            first["tsc_errors"],
        )

        # A repair session ran (fails if the Supervisor stopped repairing red tsc).
        sessions = self._sessions()
        self.assertIn("3-repairer", sessions, sessions)
        self.assertGreaterEqual(self._supervisor()["repairs"], 1, self._supervisor())

        # A report exists, is valid JSON, and carries a real status.
        report = self._report()
        self.assertIn("status", report, report)
        self.assertIn(report["status"], ("success", "partial", "failed"), report)
        # The simulated fix made the app green and built -> success.
        self.assertEqual(report["status"], "success", report)

    # -- case 2: the repair never lands; the run must still degrade cleanly -

    def test_truncation_that_never_repairs_degrades_without_crashing(self):
        """``tsc`` red every round (the config stays truncated): the harness must
        still exit cleanly, write a report, and report a status that is NOT
        ``success`` -- degrade to partial/failed rather than crash or fabricate."""
        # No marker: the stub is red for as long as the config is broken, and the
        # Repairer only ever rewrites the same truncated body, so it stays red.
        _write_truncation_tsc_stub(self.tsc, marker=None)

        code, _, stderr = self._run()

        # Never crashes: a usable assistant turn means a clean exit 0.
        self.assertEqual(code, 0, stderr)

        # The first observation was red and captured the error -- and it never
        # became green.
        first = self._observation(1)
        self.assertTrue(first["tsc_ran"], first)
        self.assertFalse(first["tsc_ok"], first)

        # The repair loop engaged and then stopped on its own caps rather than
        # spinning or throwing.
        supervisor = self._supervisor()
        self.assertGreaterEqual(supervisor["repairs"], 1, supervisor)
        self.assertEqual(supervisor["final_action"], "stop", supervisor)

        # A report was still written, and it does NOT claim success for a red app.
        report = self._report()
        self.assertIn("status", report, report)
        self.assertIn(report["status"], ("partial", "failed"), report)
        self.assertNotEqual(report["status"], "success", report)


class ObserveTruncationUnitTest(unittest.TestCase):
    """A direct ``observe.observe()`` check: a broken TS file is red, not fatal.

    The narrowest statement of the contract the end-to-end tests rely on -- and
    the one that fails loudest (it would *raise*, or return ``tsc_ok=True``) if
    ``observe`` stopped treating a non-zero ``tsc`` as a survivable red
    typecheck.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_broken_ts_yields_tsc_ran_true_ok_false_with_error_captured(self):
        from harness import observe as observe_mod

        app_dir = self.root / "app"
        (app_dir / "src").mkdir(parents=True)
        (app_dir / "src" / "app-config.ts").write_text(_broken_config_text(), encoding="utf-8")
        harness_dir = self.root / "harness"

        tsc = self.root / "tsc-stub.py"
        _write_truncation_tsc_stub(tsc, marker=None)

        # Patch HARNESS_TSC_BIN only for this call; restore in the finally so a
        # failing assertion never leaks the override into other tests.
        with mock.patch.dict(os.environ, {"HARNESS_TSC_BIN": str(tsc)}):
            observation = observe_mod.observe(
                app_dir,
                harness_dir,
                seed_dir=None,
                spec=None,
                run_build=False,
                timeout_s=30.0,
            )

        self.assertTrue(observation.tsc_ran, "tsc must have run")
        self.assertFalse(observation.tsc_ok, "a truncated config must be a RED typecheck")
        self.assertTrue(
            any("app-config.ts" in e and "error TS" in e for e in observation.tsc_errors),
            observation.tsc_errors,
        )
        # A red typecheck is never green, and it never fabricates success.
        self.assertFalse(observation.green, "a red typecheck must not be green")


if __name__ == "__main__":
    unittest.main()
