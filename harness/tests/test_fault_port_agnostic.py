"""Fault scenario: **port 3000 already occupied**. Prove the *harness* is
port-agnostic, so an externally occupied port 3000 cannot wedge it.

Where port 3000 actually lives
------------------------------
Port 3000 is the RUNNER's concern, never the harness's. The generated app's
dev server binds 3000; the runner audits and reclaims that port around every
Pi invocation:

- ``src/verify-app.ts`` probes port 3000 (is the app up?), and
- ``src/port-owner.ts`` / port reclamation frees a wedged 3000 between runs.

Those live in the TypeScript runner and are already covered by
``test/verify-app.test.ts`` and ``test/result.test.ts``. ``harness/eval.py``'s
docstrings even *say* "the generated app binds port 3000 and the runner audits
that port" -- the harness knows about 3000 only to keep out of its way.

Why the harness is immune by construction
-----------------------------------------
``harness.observe`` runs ``tsc`` / ``vitest`` / ``vite build`` -- a type check,
a headless test runner, a production bundle. None of them starts a server, and
nothing else in ``harness/*.py`` opens a listening socket at all. So a port
3000 that is already taken (by the runner, by a previous app, by the Qwen
holdout sweep running right now) is simply invisible to ``python3 -m harness``:
there is no bind to fail with ``EADDRINUSE``.

This module PINS that invariant two ways, WITHOUT ever binding, connecting to,
or freeing port 3000 (the Qwen sweep and the runner own it):

1. A SOURCE AUDIT (``HarnessOwnsNoPort3000ServerTest``). It reads every
   ``harness/*.py`` module and every ``harness/tests/fake_*.py`` fixture,
   strips comments and string/docstring literals (so a *comment* that merely
   explains "the runner owns 3000" can never trip it), and asserts that no
   module contains BOTH a server-bind construct (``.bind(`` / ``.listen(`` /
   ``serve_forever`` / ``HTTPServer`` / ``socketserver`` / ``TCPServer``) AND
   the port literal ``3000`` in executable code. The conjunction is what makes
   it precise: ``harness/missions.py`` names ``3000`` (a resume token budget)
   but opens no socket, and ``fake_gateway.py`` opens a socket but on an
   OS-assigned ephemeral port, never 3000 -- both are legitimate and both pass.
   HOW THE AUDIT WOULD FAIL if someone regressed the harness: add, say,
   ``HTTPServer(("0.0.0.0", 3000), Handler)`` to ``harness/observe.py`` and
   that module now carries a bind construct *and* the literal 3000 in real
   code, so ``test_no_harness_module_opens_a_listening_socket_on_port_3000``
   flags it by filename and fails. ``test_detector_flags_a_synthetic_port_3000_
   listener`` runs the very same detector over an in-memory snippet of exactly
   that shape to prove, in-process, that the guard has teeth -- if the detector
   were a no-op the audit would be worthless, and that self-check catches it.
   ``test_the_fake_gateway_binds_an_ephemeral_port_not_3000`` proves the
   detector is not blind the other way: it DOES see the fixture's real server,
   and that server still avoids 3000.

2. A BEHAVIOURAL run (``HarnessReachesSuccessWithoutAServerTest``). A full
   MissionsModeTest-style ``python3 -m harness`` run -- fake Pi, a fake gateway
   on a loopback *ephemeral* port (never 3000), and green ``tsc``/``vitest``/
   ``vite`` stubs -- drives Analyst -> plan -> Builder + Tester to completion,
   exits 0, and writes ``report.partial.json``. It reaches success with NO
   server of its own, which is the whole point: a pre-occupied port 3000 is
   irrelevant to a process that never binds one. (Were a 3000 listener ever
   added to ``observe()``, this run would raise ``EADDRINUSE`` against the
   occupied port and fail to write its report -- but the deterministic guard
   with teeth is the source audit above, which needs no port at all.)

Hard rules honoured: no real network/model call (the gateway is a scripted
loopback double, the Pi is the fake), no credential is read or set to a real
value, and this test never binds, connects to, or frees TCP port 3000.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import stat
import tempfile
import tokenize
import unittest

from harness.tests import support

REPO_ROOT = support.REPO_ROOT
HARNESS_DIR = REPO_ROOT / "harness"
TESTS_DIR = support.TESTS_DIR

#: Constructs that open (or subclass a thing that opens) a listening socket.
#: After comment/string stripping, ``.bind(`` reads as ``. bind (`` -- hence the
#: ``\s*`` -- and ``HTTPServer`` matches inside ``ThreadingHTTPServer`` too,
#: which is correct: that subclass still opens a server.
_SERVER_BIND_RE = re.compile(
    r"\.\s*bind\s*\(|\.\s*listen\s*\(|serve_forever|HTTPServer|socketserver|TCPServer"
)

#: The port literal 3000 as a standalone integer token -- not ``30000``, not
#: ``x3000``, not ``3000.5``. (After stripping, a NUMBER token stands alone.)
_PORT_3000_RE = re.compile(r"(?<![\w.])3000(?![\w.])")

#: Token types whose text is dropped so only *executable* code is scanned: a
#: docstring paragraph ("the app binds port 3000") or a ``# comment`` must never
#: be read as a real listener.
_DROPPED_TOKEN_TYPES = {tokenize.COMMENT, tokenize.STRING}
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):  # py3.12+; absent on 3.10
    _fstring_type = getattr(tokenize, _name, None)
    if _fstring_type is not None:
        _DROPPED_TOKEN_TYPES.add(_fstring_type)


def _executable_code(source: str) -> str:
    """``source`` with every comment and string/docstring literal removed.

    Returns a space-joined token stream: exact spacing is irrelevant because the
    detectors only look for the bind constructs and the ``3000`` NUMBER token,
    none of which can legitimately hide inside a string in real listener code.
    A tokenize failure would silently blind the audit, so it re-raises loudly
    rather than returning a partial strip.
    """
    pieces = []
    reader = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(reader):
        if tok.type in _DROPPED_TOKEN_TYPES:
            continue
        if tok.type in (
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
            tokenize.ENDMARKER,
        ):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


def _scanned_sources():
    """Every ``harness/*.py`` module and ``harness/tests/fake_*.py`` fixture.

    This test file is not under either glob, so it excludes itself naturally;
    the explicit guard keeps that true if the file were ever renamed.
    """
    paths = sorted(HARNESS_DIR.glob("*.py"))
    paths += sorted((HARNESS_DIR / "tests").glob("fake_*.py"))
    here = pathlib.Path(__file__).resolve()
    return [p for p in paths if p.resolve() != here]


class HarnessOwnsNoPort3000ServerTest(unittest.TestCase):
    """Source audit: no harness module opens a listening socket on port 3000."""

    def test_detector_flags_a_synthetic_port_3000_listener(self):
        # Teeth check: run the exact detector over an in-memory module that DOES
        # stand up a server on 3000. If this snippet were added to any harness
        # module, the invariant test below would flag that file. If this fails,
        # the detector is broken and the whole audit is worthless.
        offending = (
            "import http.server\n"
            "def serve():\n"
            "    srv = http.server.HTTPServer(('0.0.0.0', 3000), Handler)\n"
            "    srv.serve_forever()\n"
        )
        code = _executable_code(offending)
        self.assertTrue(_SERVER_BIND_RE.search(code), "detector missed a real HTTPServer bind")
        self.assertTrue(_PORT_3000_RE.search(code), "detector missed the literal port 3000")

        # And a *comment/docstring* mention of 3000 must NOT look like a listener:
        # this is the "the runner owns 3000" false positive the audit must dodge.
        benign = (
            '"""The runner owns port 3000; observe() never binds it."""\n'
            "PORT_NOTE = 3000  # runner-owned; not a socket\n"
        )
        benign_code = _executable_code(benign)
        # The docstring's 3000 is gone; the assignment's 3000 survives but there
        # is no bind construct, so the conjunction (bind AND 3000) is false.
        self.assertFalse(
            _SERVER_BIND_RE.search(benign_code) and _PORT_3000_RE.search(benign_code),
            "a benign 3000 mention was misread as a port-3000 listener",
        )

    def test_the_fake_gateway_binds_an_ephemeral_port_not_3000(self):
        # Proves the detector is not vacuously passing: the one real server in
        # the scanned set (the fake gateway) IS seen as a bind construct, and
        # even it avoids 3000 -- it binds 127.0.0.1 on an OS-assigned port.
        gateway = (TESTS_DIR / "fake_gateway.py")
        self.assertIn(gateway, _scanned_sources(), "fake_gateway.py must be in the audited set")
        code = _executable_code(gateway.read_text(encoding="utf-8"))
        self.assertTrue(
            _SERVER_BIND_RE.search(code),
            "detector failed to see the fake gateway's server -- it is now blind",
        )
        self.assertFalse(
            _PORT_3000_RE.search(code),
            "the fake gateway must bind an ephemeral port, never 3000",
        )

    def test_no_harness_module_opens_a_listening_socket_on_port_3000(self):
        sources = _scanned_sources()
        # An empty glob would let the invariant pass vacuously; the real tree has
        # ~20 modules plus the two fake_*.py fixtures, so require a real corpus.
        self.assertGreaterEqual(len(sources), 15, "the audit scanned too few files to be meaningful")

        offenders = []
        for path in sources:
            try:
                code = _executable_code(path.read_text(encoding="utf-8"))
            except (OSError, tokenize.TokenError, SyntaxError) as exc:  # pragma: no cover
                self.fail("could not audit {0}: {1}".format(path, exc))
            has_server = bool(_SERVER_BIND_RE.search(code))
            has_3000 = bool(_PORT_3000_RE.search(code))
            if has_server and has_3000:
                offenders.append(path.relative_to(REPO_ROOT).as_posix())

        self.assertEqual(
            offenders,
            [],
            "these harness modules open a listening socket AND name port 3000 in "
            "executable code -- port 3000 belongs to the runner (verify-app / "
            "port reclamation), the harness must never bind it: {0}".format(offenders),
        )


# --------------------------------------------------------------------------- #
# Behavioural half: a full harness run reaches success with no server at all.  #
# Mirrors test_main_wiring.MissionsModeTest's fixtures, kept self-contained.   #
# --------------------------------------------------------------------------- #


def _executable_stub(path: pathlib.Path, script: str) -> pathlib.Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_green_tsc(path: pathlib.Path) -> None:
    _executable_stub(path, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")


def _write_green_vite(path: pathlib.Path) -> None:
    _executable_stub(
        path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('vite v5.0.0 building for production...\\nbuilt\\n')\n"
        "sys.exit(0)\n",
    )


def _write_green_vitest(path: pathlib.Path, titles) -> None:
    data = {
        "numTotalTests": len(titles),
        "numPassedTests": len(titles),
        "numFailedTests": 0,
        "testResults": [
            {"assertionResults": [{"fullName": t, "status": "passed"} for t in titles]}
        ],
    }
    _executable_stub(
        path,
        (
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "out = None\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('--outputFile='):\n"
            "        out = a.split('=', 1)[1]\n"
            "data = {0}\n"
            "if out:\n"
            "    open(out, 'w', encoding='utf-8').write(json.dumps(data))\n"
            "sys.exit(0)\n"
        ).format(json.dumps(data)),
    )


def _plan_titles():
    """The normalised journey titles ``derive_plan`` yields for the scripted spec
    -- exactly what the vitest stub must report so the plan is graded green."""
    from harness.analyst import normalize_spec
    from harness.plan import derive_plan

    raw = json.loads((TESTS_DIR / "fixtures" / "spec-books.json").read_text(encoding="utf-8"))
    return [test["title"] for test in derive_plan(normalize_spec(raw))["tests"]]


class HarnessReachesSuccessWithoutAServerTest(unittest.TestCase):
    """A full missions-mode run completes and writes a report while binding no
    port of its own -- so an externally occupied port 3000 is irrelevant."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.root = pathlib.Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.server = None

        # A private, writable copy of app-template (never write into the real one).
        self.app_dir = self.root / "app"
        shutil.copytree(
            REPO_ROOT / "app-template",
            self.app_dir,
            ignore=shutil.ignore_patterns("node_modules"),
            symlinks=True,
        )

        # What the fake Builder/Tester "write": the seed config plus one line
        # (byte-identical would read as no change), and the journeys template.
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
        _write_green_tsc(self.tsc)
        _write_green_vitest(self.vitest, _plan_titles())
        _write_green_vite(self.vite)

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    def _start_gateway(self):
        """A scripted provider on a loopback EPHEMERAL port (port=0), never 3000:
        one spec, then spare schema-failing replies so the run always terminates."""
        from harness.tests.fake_gateway import FakeGatewayServer, ScriptedResponse, ok_response

        spec_text = (TESTS_DIR / "fixtures" / "spec-books.json").read_text(encoding="utf-8")
        server = FakeGatewayServer()  # port=0 -> OS-assigned; we never request 3000
        server.script(
            [ScriptedResponse(status=200, body=ok_response(content=spec_text, completion_tokens=800))]
            + [ScriptedResponse(status=200, body=ok_response(content="{}")) for _ in range(20)]
        )
        server.start()
        self.server = server
        return server

    def test_full_missions_run_completes_and_writes_a_report_without_binding_a_port(self):
        server = self._start_gateway()

        # Guard the hard rule directly: the only socket in this whole test is the
        # fake gateway, and it is on loopback at an ephemeral port -- not 3000.
        self.assertNotEqual(server.server_address[1], 3000, "the fake gateway must not sit on 3000")
        self.assertNotIn(":3000", server.base_url)

        env = {
            "BERGET_API_KEY": "fake-test-key",  # a dummy for the fake gateway (allowed)
            "HARNESS_GATEWAY_URL": server.base_url,
            "HARNESS_SESSION_MODE": "per-mission",
            "FAKE_PI_WRITE_ON_PROMPT": "src/app-config.ts={0};src/journeys.test.tsx={1}".format(
                self.config_source, self.tests_source
            ),
            "HARNESS_TSC_BIN": str(self.tsc),
            "HARNESS_VITEST_BIN": str(self.vitest),
            "HARNESS_VITE_BIN": str(self.vite),
        }

        # Bounded: run_harness raises if the run does not exit within wait_s.
        code, _stdout, stderr = support.run_harness(
            self.run_dir,
            timeout_ms=300_000,
            cwd=self.app_dir,
            env_extra=env,
            wait_s=180.0,
        )

        # The harness ran observe() (tsc/vitest/vite -- no server) to a verdict
        # and finished cleanly. No bind, so an occupied 3000 could not wedge it.
        self.assertEqual(code, 0, stderr)

        report_path = self.app_dir / "report.partial.json"
        self.assertTrue(
            support.wait_for(report_path.is_file, timeout=10.0),
            "report.partial.json was never written: " + stderr,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "success", report)
        self.assertTrue(all(e["result"] == "passed" for e in report["tests_run"]), report)

        # The gateway (the sole server anywhere in this run) never moved to 3000.
        self.assertNotEqual(self.server.server_address[1], 3000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
