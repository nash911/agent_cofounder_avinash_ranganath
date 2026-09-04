"""Fault injection (BUILD_PLAN.md §Phase 5): the harness-authored
``report.partial.json`` destination is unwritable.

Why this exists
---------------
``harness/report.py`` writes the fallback report atomically -- it stages the
JSON in ``report.partial.json.harness-tmp`` and then ``os.replace``s it onto
``report.partial.json`` (``write_report``). BUILD_PLAN.md's Phase-5 fault list
includes "one ``result.json`` destination unwritable": if that write cannot
land (a read-only app dir, or a destination that refuses the write), the
harness must *log the failure and keep going to a clean exit* -- never crash.
``write_report`` already wraps the two-step write in ``except OSError`` and
returns ``False`` after ``warn("could not write report.partial.json: ...")``;
this module proves that guard actually holds, at the two layers Phase 5 asks
for:

1. a UNIT test of :func:`harness.report.write_report` (and of the
   :class:`~harness.report.ReportWatcher` path that drives it) against an app
   dir whose ``report.partial.json`` cannot be created; and
2. an END-TO-END missions-mode run (``python3 -m harness`` in a subprocess,
   ``MissionsModeTest``-style) in which *only* the report write is forced to
   fail, asserting the harness still exits 0, still writes supervisor.json /
   missions.json / budget.json, and logs the write failure on stderr.

How the fault is induced (surgical + uid-independent)
-----------------------------------------------------
The write's very first step is ``open(<app_dir>/report.partial.json.harness-tmp,
"wb")``. Pre-creating that exact staging path as a **directory** makes that
``open`` raise ``IsADirectoryError`` (a subclass of ``OSError``) on every
attempt, for any uid -- so the report can never land while *nothing else* in
the run is disturbed: the fake Builder/Tester still write ``src/*`` into the
app dir, ``observe()`` still writes ``harness/vitest.json``, and the harness
dir still works. That surgical scoping is exactly what the task requires ("only
the report path must be unwritable"). The unit layer additionally exercises the
more literal "app dir is read-only" shape via ``chmod 0o555`` (skipped only if
the process is root, where mode bits do not restrict writes); every permission
change is reverted in a ``finally``.

If report.py's ``except OSError`` were deleted
----------------------------------------------
``write_report`` would let the ``IsADirectoryError`` / ``PermissionError``
propagate instead of logging and returning ``False``:

- UNIT: the ``write_report(...)`` call itself would raise, so ``assertFalse(ok)``
  is never reached and the test errors out (and no warning is logged).
- E2E: the exception escapes ``_supervise``'s green-round write (loop.py:337)
  and then ``run_missions`` uncaught, so the harness exits non-zero with a
  ``Traceback`` on stderr and never gets to write supervisor.json /
  missions.json -- flipping every assertion below.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from harness import report
from harness.tests import support

# The end-to-end body reuses MissionsModeTest's stub/fixture helpers verbatim
# (importing is read-only; this file edits nothing there).
from harness.tests import test_main_wiring as tmw

REPO_ROOT = support.REPO_ROOT

#: The exact staging file ``write_report`` opens before the atomic replace.
TMP_SUFFIX = "report.partial.json.harness-tmp"
#: The substring ``write_report`` logs from its ``except OSError`` branch.
WRITE_FAILURE_LOG = "could not write report.partial.json"


class WriteReportUnwritableUnitTest(unittest.TestCase):
    """Layer 1: :func:`harness.report.write_report` degrades, never raises."""

    GREEN = {"green": True, "total": 1, "passed": 1, "failed": 0, "names": ["a journey"]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.app_dir = self.root / "app"
        self.app_dir.mkdir()

    def tearDown(self):
        # Restore before cleanup so a read-only dir left by a failing assertion
        # never blocks TemporaryDirectory from removing the tree.
        try:
            os.chmod(self.app_dir, 0o755)
        except OSError:
            pass
        self._tmp.cleanup()

    def test_readonly_app_dir_makes_write_report_return_false_and_warn(self):
        # The literal "make the app dir read-only" shape. The report does not yet
        # exist, so the authorship/mtime guards pass and control reaches the
        # write block, where creating the ``.harness-tmp`` staging file in a
        # 0o555 dir raises PermissionError (an OSError) -> warn + return False.
        report_path = self.app_dir / "report.partial.json"
        tmp_path = self.app_dir / TMP_SUFFIX
        os.chmod(self.app_dir, 0o555)
        # As root, mode bits do not restrict writes: the fault would not fire and
        # the assertion below would be a false pass, so skip rather than lie.
        probe = self.app_dir / ".probe"
        try:
            probe.write_text("x", encoding="utf-8")
        except OSError:
            pass  # good: the directory really is unwritable
        else:
            probe.unlink()
            self.skipTest("process can write a 0o555 dir (root); see the tmp-dir test")

        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                ok = report.write_report(self.app_dir, None, self.GREEN, "An idea.")
        finally:
            os.chmod(self.app_dir, 0o755)

        self.assertFalse(ok)
        self.assertIn(WRITE_FAILURE_LOG, stderr.getvalue())
        # No partial/corrupt artifact may be left behind.
        self.assertFalse(report_path.exists())
        self.assertFalse(tmp_path.exists())

    def test_blocked_staging_path_makes_write_report_return_false_and_warn(self):
        # uid-independent shape: the staging file's own path is a directory, so
        # ``open(tmp, "wb")`` raises IsADirectoryError (an OSError) for any user.
        report_path = self.app_dir / "report.partial.json"
        tmp_path = self.app_dir / TMP_SUFFIX
        tmp_path.mkdir()

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ok = report.write_report(self.app_dir, None, self.GREEN, "idea")

        self.assertFalse(ok)
        self.assertIn(WRITE_FAILURE_LOG, stderr.getvalue())
        # The real report was never created, and the blocked staging dir was left
        # exactly as it was -- no bytes leaked into it.
        self.assertFalse(report_path.exists())
        self.assertTrue(tmp_path.is_dir())
        self.assertEqual(list(tmp_path.iterdir()), [])

    def test_report_watcher_final_observe_survives_a_blocked_write(self):
        # The ReportWatcher path: a green final observation drives write_report,
        # which is blocked. final_observe() must still return the observation
        # (its whole contract) rather than propagate the write failure -- a
        # background/shutdown observer must never take the run down.
        harness_dir = self.root / "harness"
        harness_dir.mkdir()
        (self.app_dir / TMP_SUFFIX).mkdir()  # block the write

        watcher = report.ReportWatcher(self.app_dir, harness_dir, "idea", spec=None)
        stderr = io.StringIO()
        with mock.patch.object(report, "observe", return_value=dict(self.GREEN)):
            with contextlib.redirect_stderr(stderr):
                observation = watcher.final_observe()

        self.assertEqual(observation, self.GREEN)  # returned, did not raise
        self.assertIn(WRITE_FAILURE_LOG, stderr.getvalue())
        self.assertFalse((self.app_dir / "report.partial.json").exists())
        # The observation was still recorded even though the write failed.
        self.assertEqual(len(watcher.observations), 1)


class MissionsReportUnwritableE2ETest(unittest.TestCase):
    """Layer 2: a full ``python3 -m harness`` missions run whose report write is
    the only thing forced to fail (MissionsModeTest fixtures, subprocess CLI).

    The staging path is pre-created as a directory *before* the run, so both the
    green-round write (loop.py:337) and the final write (loop.py:244) hit
    ``write_report``'s ``except OSError`` branch. Everything else -- the fake
    Builder/Tester writes into ``src/``, ``observe()``'s ``harness/vitest.json``,
    the harness JSON artifacts -- is untouched, which is what proves the harness
    degrades on *this* fault rather than simply failing to run.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = tmw._copy_app_template(self.root)
        self.prompt_log = self.root / "prompts.jsonl"
        self.server = None

        # What the fake Builder/Tester "write" (mirrors MissionsModeTest.setUp):
        # the config must differ from the seed or the Builder earns a rerun.
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
        tmw._write_tsc_stub(self.tsc)
        tmw._write_vitest_stub(self.vitest, tmw._plan_titles())
        tmw._write_vite_stub(self.vite)

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    def _gateway(self, spare: int = 20):
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
            "HARNESS_SESSION_MODE": "per-mission",
        }
        env.update(extra)
        return env

    def test_missions_run_survives_an_unwritable_report_and_logs_the_failure(self):
        tmp_path = self.app_dir / TMP_SUFFIX
        tmp_path.mkdir()  # <- the only fault: the report's staging path is a dir
        self._gateway()

        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=300_000,
            cwd=self.app_dir,
            env_extra=self._env(),
            wait_s=120.0,
        )

        # Graceful degradation: a clean exit, no crash, and the write failure
        # logged (not the authorship/mtime "leaving it untouched" branch).
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertIn(WRITE_FAILURE_LOG, stderr)

        # The report could not land, and no partial/corrupt file was left; the
        # blocked staging path is untouched.
        self.assertFalse((self.app_dir / "report.partial.json").is_file(), stderr)
        self.assertTrue(tmp_path.is_dir())
        self.assertEqual(list(tmp_path.iterdir()), [])

        # Proof the failure was scoped to the report only: the run still reached
        # and completed the post-report steps. If report.py's OSError guard were
        # removed, the green-round write would raise out of _supervise and these
        # files would never be written (and the assertions above would already
        # have failed on a non-zero exit + Traceback).
        harness_dir = self.run_dir / "harness"
        for name in ("supervisor.json", "missions.json", "budget.json"):
            self.assertTrue((harness_dir / name).is_file(), name + " missing: " + stderr)

        missions = json.loads((harness_dir / "missions.json").read_text(encoding="utf-8"))
        self.assertTrue(missions["missions"], missions)
        self.assertTrue(all(m["success"] for m in missions["missions"]), missions)


if __name__ == "__main__":
    unittest.main()
