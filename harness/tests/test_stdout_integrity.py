"""A record must reach the runner whole, even when a signal cuts the write short.

The runner forces ``PYTHONUNBUFFERED=1``, so the harness's ``sys.stdout.buffer``
is a raw ``_io.FileIO``: one ``write()`` is one ``write(2)``. With a Python-level
signal handler installed (the harness installs SIGTERM/SIGINT) and a full pipe, a
signal landing mid-write returns a short count. Ignoring it truncates the record
and merges it with the next one, which makes ``events.jsonl`` unparseable exactly
when the run is being wound down.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from harness.tests import support

RECORD_BYTES = 4_000_000

DRIVER = '''\
import os
import signal
import sys
import threading
import time

sys.path.insert(0, {repo!r})

from harness import pirpc

# SA_RESTART only restarts a syscall that moved zero bytes; once bytes have been
# written, the short count is returned to Python.
signal.signal(signal.SIGTERM, lambda *_: None)


def nag() -> None:
    for _ in range(60):
        time.sleep(0.05)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            return


threading.Thread(target=nag, daemon=True).start()
record = b'{{"type":"message_update","text":"' + b"y" * {size} + b'"}}'
pirpc.forward_record(record)
sys.stderr.write("driver-done\\n")
'''


@unittest.skipUnless(os.name == "posix", "signal delivery is POSIX only")
class StdoutIntegrityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(support.scratch_root()))
        self.driver = pathlib.Path(self._tmp.name) / "forward_driver.py"
        self.driver.write_text(
            DRIVER.format(repo=str(support.REPO_ROOT), size=RECORD_BYTES), encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_large_record_survives_a_signal_mid_write(self):
        env = support.harness_environment()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(self.driver)],
            cwd=str(support.REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Let the pipe fill so the child blocks inside write(2) while the
            # signals arrive.
            time.sleep(0.5)
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
            raise AssertionError("the forwarder never finished")

        expected = b'{"type":"message_update","text":"' + b"y" * RECORD_BYTES + b'"}\n'
        self.assertIn(b"driver-done", stderr)
        self.assertEqual(len(stdout), len(expected), "the record was truncated on stdout")
        self.assertEqual(stdout, expected)
        self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
