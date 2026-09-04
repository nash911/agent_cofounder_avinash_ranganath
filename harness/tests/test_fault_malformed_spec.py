"""Fault injection: a malformed / unusable Analyst spec must fall back to single mode.

WHY. ``harness.__main__.run`` attempts the Analyst (C4) before it chooses a
body. ``run_analyst`` must return ``None`` on **any** failure, and two failures
in this family are easy to get wrong:

- The direct gateway returns a 200 whose message ``content`` is not JSON. This
  is exactly what ``gateway.json_schema``'s one parse-retry (C3) exists for: it
  strips ``json`` fences, and on a parse failure re-asks the model once,
  quoting the parse error. When *both* the first attempt and that retry come
  back non-JSON, ``json_schema`` returns ``(None, ...)`` and ``run_analyst``
  must surface that as ``None`` -- never a crash, never a half-parsed dict.

- The gateway returns a 200 that *is* valid JSON but normalises to something no
  build can use (an empty object, or one with no fields). ``normalize_spec`` is
  total, but ``unusable_reasons`` then reports "no usable fields / no valid
  title_field / no journeys / no app_name", and ``run_analyst`` must again
  return ``None`` -- "a half-empty spec is worse than none, because missions
  mode would build the whole application from it" (analyst.py docstring).

On ``None``, ``resolve_mode(None)`` picks the Phase-2 single-session path
("no usable spec"). Two invariants then hold and are asserted here:
``harness/spec.json`` is never written (analyst.py writes it only *after* the
usability gate passes) and ``harness/plan.json`` -- a missions-only artifact
(loop.run_missions) -- never appears. The run must still reach a clean exit and
a harness-authored ``report.partial.json``.

HOW THIS FAILS IF THE FALLBACK WERE BROKEN. If ``run_analyst`` let the
``JSONDecodeError`` propagate instead of swallowing it, the harness would crash:
the exit code would not be 0 and a ``Traceback`` would appear in stderr -- both
asserted against. If it returned the raw garbage/empty dict instead of ``None``,
``resolve_mode`` would see a truthy spec and log ``mode · missions``, write
``plan.json`` and drive the Builder/Tester on a null contract -- so the
``mode · single (no usable spec)``, ``spec.json absent`` and ``plan.json
absent`` assertions would each fail. A test that only checked "it did not
raise" would pass even with the fallback deleted; these do not.

The gateway/env wiring is *replicated* from ``MissionsModeTest._gateway/_env``
and the green single-session recipe from
``test_green_tests_produce_a_report_and_a_budget_snapshot`` rather than
imported -- sibling fault-injection files are being written concurrently, and
importing another test module perturbs discovery order. Only the public knobs
of ``fake_gateway`` / ``fake_pi`` / ``support`` are used; nothing is edited.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import stat
import tempfile
import unittest

from harness.tests import support
from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

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
    return dest


def _write_green_vitest_stub(path: pathlib.Path, tests_run) -> None:
    """A stand-in for ``vitest`` that writes a canned all-passed JSON report.

    Replicated from ``test_main_wiring._write_vitest_stub``: this is what lets
    the single-session ``ReportWatcher.final_observe`` see a green run and author
    ``report.partial.json`` with no model and no real test runner.
    """
    assertion_results = [{"fullName": name, "status": "passed"} for name in tests_run]
    data = {
        "numTotalTests": len(tests_run),
        "numPassedTests": len(tests_run),
        "numFailedTests": 0,
        "testResults": [{"assertionResults": assertion_results}],
    }
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
    ).format(json.dumps(data))
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class MalformedSpecFallbackTest(unittest.TestCase):
    """Both spec-failure families collapse to the single-session path, cleanly."""

    #: The journeys the green vitest stub reports as passed. In single mode the
    #: report has no plan to reconcile against, so any names are fine.
    GREEN_JOURNEYS = ("creates a record", "lists records", "filters records")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = _copy_app_template(self.root)
        self.vitest = self.root / "vitest-stub.py"
        _write_green_vitest_stub(self.vitest, self.GREEN_JOURNEYS)
        self.server = None

    def tearDown(self):
        # Stop the loopback gateway first so its thread never outlives the tmp
        # dir cleanup; both are best-effort so a failing assertion still tears
        # the fixture down completely.
        if self.server is not None:
            try:
                self.server.stop()
            finally:
                self.server = None
        self._tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def _start_gateway(self, responses):
        """Start a scripted loopback gateway (OS-assigned port; never :3000)."""
        server = FakeGatewayServer()
        server.script(responses)
        server.start()
        self.server = server
        return server

    def _env(self):
        # BERGET_API_KEY is a *dummy* -- the gateway is a fake loopback socket,
        # so the analyst is genuinely attempted but no real call is ever made.
        # A large --timeout-ms is required (not for wall clock -- the fake Pi
        # settles at once) but so the analyst's bounded deadline slice lands in
        # the future and the POST actually goes out; a short timeout would make
        # __main__.run skip the call before it starts (see
        # test_analyst_deadline_is_bounded_not_the_full_run_budget) and never
        # exercise the parse/retry path at all.
        return {
            "BERGET_API_KEY": "test-key",
            "HARNESS_GATEWAY_URL": self.server.base_url,
            "FAKE_PI_GREEN_TESTS": "1",
            "HARNESS_VITEST_BIN": str(self.vitest),
        }

    def _assert_single_mode_degradation(self, code, stderr):
        """The invariants shared by every unusable-spec run."""
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("Traceback", stderr)

        # The dispatch log proves the Phase-2 body ran, not missions.
        self.assertIn("mode · single (no usable spec)", stderr)
        # __main__.run's own line after run_analyst returned None.
        self.assertIn("no spec (continuing without one)", stderr)
        # It must never have logged the missions dispatch.
        self.assertNotIn("mode · missions", stderr)

        harness_dir = self.run_dir / "harness"
        # spec.json is written only *after* the usability gate passes; a failed
        # or unusable spec must leave no spec file behind.
        self.assertFalse((harness_dir / "spec.json").is_file(), "spec.json was written")
        # plan.json is a missions-only artifact (loop.run_missions); single mode
        # never derives it to disk.
        self.assertFalse((harness_dir / "plan.json").is_file(), "plan.json was written")
        for missions_only in ("supervisor.json", "missions.json"):
            self.assertFalse(
                (harness_dir / missions_only).is_file(), missions_only + " was written"
            )

        # The single-session path still produced a harness-authored report.
        report_path = self.app_dir / "report.partial.json"
        self.assertTrue(
            support.wait_for(lambda: report_path.is_file(), timeout=10.0),
            "report.partial.json was never written; single-session path did not finish",
        )
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "partial", payload)

    # -- case 1: non-JSON on both the analyst call and its parse-retry -----

    def test_non_json_on_both_attempts_falls_back_to_single(self):
        # The first POST is the analyst call; because json_schema strips fences
        # and retries once on a parse failure, the SECOND POST is that retry.
        # Both 200s carry prose, not JSON, so json_schema returns obj=None.
        non_json = ok_response(content="Sorry, here is the app: it lists your books ...")
        self._start_gateway(
            [
                ScriptedResponse(status=200, body=non_json),
                ScriptedResponse(status=200, body=non_json),
            ]
            # Spares: single mode makes no further gateway call, but a stray one
            # must resolve deterministically rather than hang the fake server.
            + [ScriptedResponse(status=200, body=ok_response(content="{}")) for _ in range(6)]
        )

        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=300_000,
            cwd=self.app_dir,
            env_extra=self._env(),
            wait_s=120.0,
        )

        self._assert_single_mode_degradation(code, stderr)
        # The analyst-level warn fires only after BOTH the call and its retry
        # failed to parse -- proof the retry path was walked, not short-circuited.
        self.assertIn("analyst · no usable spec", stderr)

        # Exactly two POSTs reached the gateway: the call and its one retry.
        self.assertEqual(len(self.server.requests), 2, self.server.requests)
        # The second request is the retry: it quotes the parse error back and
        # echoes the first, non-JSON reply as the prior assistant turn.
        retry_messages = self.server.requests[1]["body"]["messages"]
        self.assertIn(
            "not valid JSON matching the schema", retry_messages[-1]["content"], retry_messages
        )
        self.assertEqual(retry_messages[-2]["role"], "assistant", retry_messages)

    # -- case 2: valid JSON that normalises to unusable --------------------

    def test_valid_but_unusable_json_falls_back_to_single(self):
        # A 200 whose content is valid JSON but an empty object: it parses on
        # the first attempt (so there is NO retry), then unusable_reasons finds
        # no fields / no title_field / no journeys / no app_name, and
        # run_analyst returns None all the same.
        self._start_gateway(
            [ScriptedResponse(status=200, body=ok_response(content="{}"))]
            + [ScriptedResponse(status=200, body=ok_response(content="{}")) for _ in range(6)]
        )

        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=300_000,
            cwd=self.app_dir,
            env_extra=self._env(),
            wait_s=120.0,
        )

        self._assert_single_mode_degradation(code, stderr)
        # This family logs the *usability* rejection, not a parse failure.
        self.assertIn("analyst · spec unusable", stderr)

        # Valid JSON parses on the first try, so json_schema never retries:
        # exactly one POST, in contrast to case 1's two.
        self.assertEqual(len(self.server.requests), 1, self.server.requests)


if __name__ == "__main__":
    unittest.main()
