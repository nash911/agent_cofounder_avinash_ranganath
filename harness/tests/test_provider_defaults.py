"""The provider and the model are always explicit (BUILD_PLAN §2).

With ``CHALLENGE_PROVIDER``/``CHALLENGE_MODEL`` unset and no ``--provider``/
``--model`` on the command line, a Pi session used to be started with neither
flag: Pi then picked the first model of the only configured provider, which was
the judged model by list order alone. These tests pin the resolution
(:func:`harness.__main__.resolve_provider_model`) and, more importantly, pin
what actually reaches the command line -- through the real code paths:

* the in-process :class:`~harness.missions.MissionRunner`, which spawns a real
  fake-Pi session and logs its own ``argv``;
* a full ``python3 -m harness`` subprocess in missions mode, and another in the
  single-session fallback, both against the fake Pi with ``FAKE_PI_PROMPT_LOG``;
* the direct gateway client, which must resolve to the *same* provider/model as
  the missions -- the fake gateway records the model it was asked for.

``support.harness_environment`` pops both ``CHALLENGE_*`` variables, so every
subprocess here already runs in the empty environment this is about.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import tempfile
import unittest
from typing import Dict, List
from unittest import mock

from harness import __main__ as main
from harness.pirpc import base_args
from harness.tests import support
from harness.tests.test_main_wiring import (
    _copy_app_template,
    _plan_titles,
    _prompts,
    _write_tsc_stub,
    _write_vite_stub,
    _write_vitest_stub,
)
from harness.tests.test_missions import MissionRunnerTestCase

#: What the contract defaults to when nothing else says otherwise. Duplicated
#: from ``__main__`` on purpose: a test that imported the constants could not
#: notice them changing, and this pair is what ``.pi-agent/settings.json`` and
#: ``.env.example`` also spell out.
PROVIDER = "berget"
MODEL = "zai-org/GLM-5.2"

#: Stand-ins for "the organizers set something else".
OTHER_PROVIDER = "organizer-provider"
OTHER_MODEL = "organizer/model-x"


def _args(provider=None, model=None) -> argparse.Namespace:
    return argparse.Namespace(provider=provider, model=model)


def _empty_env():
    """``os.environ`` with both ``CHALLENGE_*`` variables removed."""
    return mock.patch.dict(os.environ, {}, clear=True)


def _flag(argv: List[str], name: str) -> str:
    """The value of ``name`` in ``argv``; fails the test when the flag is absent."""
    if name not in argv:
        raise AssertionError("{0} missing from {1!r}".format(name, argv))
    return argv[argv.index(name) + 1]


class ResolutionTest(unittest.TestCase):
    """``resolve_provider_model``: CLI flag, then ``CHALLENGE_*``, then default."""

    def test_an_empty_environment_resolves_the_contract_defaults(self):
        with _empty_env():
            self.assertEqual(main.resolve_provider_model(_args()), (PROVIDER, MODEL))

    def test_the_challenge_variables_win_over_the_defaults(self):
        with _empty_env():
            os.environ["CHALLENGE_PROVIDER"] = OTHER_PROVIDER
            os.environ["CHALLENGE_MODEL"] = OTHER_MODEL
            self.assertEqual(
                main.resolve_provider_model(_args()), (OTHER_PROVIDER, OTHER_MODEL)
            )

    def test_the_command_line_wins_over_the_challenge_variables(self):
        with _empty_env():
            os.environ["CHALLENGE_PROVIDER"] = OTHER_PROVIDER
            os.environ["CHALLENGE_MODEL"] = OTHER_MODEL
            resolved = main.resolve_provider_model(_args(provider="cli", model="cli/model"))
        self.assertEqual(resolved, ("cli", "cli/model"))

    def test_an_empty_variable_falls_through_to_the_default(self):
        with _empty_env():
            os.environ["CHALLENGE_PROVIDER"] = ""
            os.environ["CHALLENGE_MODEL"] = ""
            self.assertEqual(main.resolve_provider_model(_args()), (PROVIDER, MODEL))

    def test_the_resolved_pair_reaches_base_args_as_two_flags(self):
        with _empty_env():
            provider, model = main.resolve_provider_model(_args())
        argv = base_args(provider=provider, model=model)
        self.assertEqual(_flag(argv, "--provider"), PROVIDER)
        self.assertEqual(_flag(argv, "--model"), MODEL)


class DirectClientAgreesTest(unittest.TestCase):
    """The direct client is built from the same resolution the sessions get."""

    def test_build_direct_client_uses_the_resolved_provider_and_model(self):
        gateway = mock.Mock()
        with tempfile.TemporaryDirectory(dir=str(support.scratch_root())) as tmp:
            with mock.patch.object(main, "_gateway", gateway), _empty_env():
                main.build_direct_client(pathlib.Path(tmp), "test-key", _args())
        kwargs = gateway.GatewayClient.call_args[1]
        self.assertEqual(kwargs["provider"], PROVIDER)
        self.assertEqual(kwargs["model"], MODEL)


class MissionArgvTest(MissionRunnerTestCase):
    """A real fake-Pi mission session, spawned in process, logs its own argv."""

    def _run_one_mission(self, provider: str, model: str) -> List[str]:
        self.log_prompts()
        runner = self.make_runner(provider=provider, model=model)
        result = runner.run(self.mission_builder())
        self.assertTrue(result.success, result.error)
        entries = self.prompt_entries()
        self.assertTrue(entries, "the fake Pi logged no prompt")
        return list(entries[0]["argv"])

    def test_an_empty_environment_still_names_the_defaults(self):
        with _empty_env():
            provider, model = main.resolve_provider_model(_args())
        argv = self._run_one_mission(provider, model)
        self.assertEqual(_flag(argv, "--provider"), PROVIDER)
        self.assertEqual(_flag(argv, "--model"), MODEL)

    def test_the_challenge_variables_reach_the_command_line(self):
        with _empty_env():
            os.environ["CHALLENGE_PROVIDER"] = OTHER_PROVIDER
            os.environ["CHALLENGE_MODEL"] = OTHER_MODEL
            provider, model = main.resolve_provider_model(_args())
        argv = self._run_one_mission(provider, model)
        self.assertEqual(_flag(argv, "--provider"), OTHER_PROVIDER)
        self.assertEqual(_flag(argv, "--model"), OTHER_MODEL)


class EndToEndArgvTest(unittest.TestCase):
    """``python3 -m harness`` as a subprocess: both bodies, real command lines."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.app_dir = _copy_app_template(self.root)
        self.prompt_log = self.root / "prompts.jsonl"
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.stop()

    # -- doubles -----------------------------------------------------------

    def _gateway(self, spare: int = 20):
        """The scripted provider: the Analyst's spec, then unusable replies."""
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

    def _missions_env(self, **extra) -> Dict[str, str]:
        seed_config = (self.app_dir / "src" / "app-config.ts").read_text(encoding="utf-8")
        config_source = self.root / "written-config.ts"
        config_source.write_text(seed_config + "\n// written by the fake Builder\n", encoding="utf-8")
        tests_source = self.root / "written-tests.tsx"
        tests_source.write_text(
            (self.app_dir / "src" / "test" / "journeys.template.tsx").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        tsc, vitest, vite = self.root / "tsc.py", self.root / "vitest.py", self.root / "vite.py"
        _write_tsc_stub(tsc)
        _write_vitest_stub(vitest, _plan_titles())
        _write_vite_stub(vite)
        env = {
            "BERGET_API_KEY": "fake-test-key",
            "HARNESS_GATEWAY_URL": self.server.base_url,
            "HARNESS_SESSION_MODE": "per-mission",
            "FAKE_PI_PROMPT_LOG": str(self.prompt_log),
            "FAKE_PI_WRITE_ON_PROMPT": "src/app-config.ts={0};src/journeys.test.tsx={1}".format(
                config_source, tests_source
            ),
            "HARNESS_TSC_BIN": str(tsc),
            "HARNESS_VITEST_BIN": str(vitest),
            "HARNESS_VITE_BIN": str(vite),
        }
        env.update(extra)
        return env

    def _argvs(self) -> List[List[str]]:
        """Every distinct Pi command line the run produced, in prompt order."""
        seen: List[List[str]] = []
        for entry in _prompts(self.prompt_log):
            argv = list(entry["argv"])
            if argv not in seen:
                seen.append(argv)
        self.assertTrue(seen, "the fake Pi logged no prompt")
        return seen

    # -- missions mode ------------------------------------------------------

    def test_missions_and_the_direct_client_agree_on_the_defaults(self):
        self._gateway()
        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=300_000,
            cwd=self.app_dir,
            env_extra=self._missions_env(),
            wait_s=120.0,
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("mode · missions", stderr)
        self.assertIn("model · {0} / {1}".format(PROVIDER, MODEL), stderr)

        argvs = self._argvs()
        self.assertEqual(len(argvs), 2, argvs)  # one Builder, one Tester
        for argv in argvs:
            self.assertEqual(_flag(argv, "--provider"), PROVIDER, argv)
            self.assertEqual(_flag(argv, "--model"), MODEL, argv)

        # The Analyst's own call went to the same model over the direct client.
        models = {
            request["body"].get("model")
            for request in self.server.requests
            if isinstance(request.get("body"), dict)
        }
        self.assertEqual(models, {MODEL}, self.server.requests)

    # -- the single-session fallback ---------------------------------------

    def _run_single(self, **extra) -> str:
        """No key and no gateway: no spec, so the run falls back to one session."""
        code, _, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=60_000,
            cwd=self.app_dir,
            env_extra=dict({"FAKE_PI_PROMPT_LOG": str(self.prompt_log)}, **extra),
            wait_s=120.0,
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("mode · single (no usable spec)", stderr)
        return stderr

    def test_the_fallback_session_names_the_defaults_too(self):
        stderr = self._run_single()
        self.assertIn("model · {0} / {1}".format(PROVIDER, MODEL), stderr)
        argvs = self._argvs()
        self.assertEqual(len(argvs), 1, argvs)
        self.assertEqual(_flag(argvs[0], "--provider"), PROVIDER, argvs[0])
        self.assertEqual(_flag(argvs[0], "--model"), MODEL, argvs[0])

    def test_the_challenge_variables_win_end_to_end(self):
        self._run_single(CHALLENGE_PROVIDER=OTHER_PROVIDER, CHALLENGE_MODEL=OTHER_MODEL)
        argv = self._argvs()[0]
        self.assertEqual(_flag(argv, "--provider"), OTHER_PROVIDER, argv)
        self.assertEqual(_flag(argv, "--model"), OTHER_MODEL, argv)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
