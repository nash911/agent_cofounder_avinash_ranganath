"""Unit tests for the RPC client: spawn shape, flag set, environment, framing."""

from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from harness import pirpc
from harness.pirpc import PiRpc, base_args, pi_env


class _FakeProcess:
    """Just enough of ``Popen`` for :class:`PiRpc` to start and stop cleanly."""

    def __init__(self):
        self.stdout = io.BytesIO(b"")
        self.stdin = io.BytesIO()
        self.pid = 4242
        self._code = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class SpawnShapeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _construct(self):
        with mock.patch.object(pirpc.subprocess, "Popen") as popen:
            popen.return_value = _FakeProcess()
            client = PiRpc(
                pi_bin="/bin/false",
                args=["--offline"],
                cwd=self.root,
                env={"PI_OFFLINE": "1"},
                session_dir=self.root / "sessions" / "1-builder",
                label="1-builder",
                stderr_path=self.root / "harness" / "1-builder.stderr.log",
            )
        return client, popen.call_args

    def test_popen_never_detaches_the_process_group(self):
        client, call = self._construct()
        try:
            self.assertNotIn("start_new_session", call.kwargs)
            self.assertNotIn("preexec_fn", call.kwargs)
            self.assertNotIn("creationflags", call.kwargs)
        finally:
            client.close(stdin_grace=0.1, term_grace=0.1, kill_grace=0.1)

    def test_popen_pipes_stdin_and_stdout_and_files_stderr(self):
        client, call = self._construct()
        try:
            self.assertEqual(call.kwargs["stdin"], pirpc.subprocess.PIPE)
            self.assertEqual(call.kwargs["stdout"], pirpc.subprocess.PIPE)
            self.assertNotIn(call.kwargs["stderr"], (pirpc.subprocess.PIPE, None))
            self.assertNotIn("text", call.kwargs)
            self.assertNotIn("universal_newlines", call.kwargs)
        finally:
            client.close(stdin_grace=0.1, term_grace=0.1, kill_grace=0.1)

    def test_mode_rpc_is_prepended_and_session_dir_created(self):
        client, call = self._construct()
        try:
            argv = call.args[0]
            self.assertEqual(argv[:3], ["/bin/false", "--mode", "rpc"])
            self.assertTrue((self.root / "sessions" / "1-builder").is_dir())
            self.assertTrue((self.root / "harness").is_dir())
        finally:
            client.close(stdin_grace=0.1, term_grace=0.1, kill_grace=0.1)


class BaseArgumentsTest(unittest.TestCase):
    def test_flag_set_matches_the_starter(self):
        args = base_args(
            append_system="PROMPT",
            session_dir="/runs/1/sessions/1-builder",
            extensions=["/repo/solution/extensions/protected-paths.ts"],
            skill="/repo/solution/skills/mvp-builder",
            thinking="off",
        )
        for flag in ("--offline", "--no-extensions", "--no-skills", "--no-prompt-templates",
                     "--no-themes", "--no-context-files"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--append-system-prompt") + 1], "PROMPT")
        self.assertEqual(args[args.index("--session-dir") + 1], "/runs/1/sessions/1-builder")
        self.assertEqual(args[args.index("--skill") + 1], "/repo/solution/skills/mvp-builder")
        self.assertEqual(args[-2:], ["--thinking", "off"])
        self.assertNotIn("--provider", args)
        self.assertNotIn("--model", args)

    def test_provider_and_model_only_when_given(self):
        args = base_args(provider="berget", model="zai-org/GLM-5.2", thinking="low")
        self.assertEqual(args[args.index("--provider") + 1], "berget")
        self.assertEqual(args[args.index("--model") + 1], "zai-org/GLM-5.2")
        self.assertEqual(args[args.index("--thinking") + 1], "low")

    def test_several_extensions_each_get_their_own_flag(self):
        args = base_args(extensions=["/a/protected-paths.ts", "/a/thinking-guard.ts"])
        self.assertEqual(args.count("--extension"), 2)


class EnvironmentTest(unittest.TestCase):
    def test_pi_offline_is_set_and_agent_dir_is_never_invented(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["CHALLENGE_PROVIDER"] = "berget"
            env = pi_env()
        self.assertEqual(env["PI_OFFLINE"], "1")
        self.assertEqual(env["CHALLENGE_PROVIDER"], "berget")
        self.assertNotIn("PI_CODING_AGENT_DIR", env)

    def test_an_inherited_agent_dir_passes_through_untouched(self):
        with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": "/organizer/.pi"}, clear=True):
            env = pi_env()
        self.assertEqual(env["PI_CODING_AGENT_DIR"], "/organizer/.pi")

    def test_extra_values_are_merged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = pi_env({"HARNESS_PAYLOAD_LOG": "/runs/1/harness/payload.jsonl"})
        self.assertEqual(env["HARNESS_PAYLOAD_LOG"], "/runs/1/harness/payload.jsonl")


class FramingTest(unittest.TestCase):
    """The reader splits on LF only, strips one trailing CR, forwards everything."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _drive(self, payload: bytes):
        forwarded = []
        process = _FakeProcess()
        process.stdout = io.BytesIO(payload)
        with mock.patch.object(pirpc.subprocess, "Popen") as popen, \
                mock.patch.object(pirpc, "forward_record", side_effect=forwarded.append):
            popen.return_value = process
            client = PiRpc(
                pi_bin="/bin/false",
                args=[],
                cwd=self.root,
                env={},
                session_dir=self.root / "s",
                label="t",
                stderr_path=self.root / "h" / "t.stderr.log",
            )
            client._reader.join(timeout=5.0)
            events = client.drain()
            client.close(stdin_grace=0.1, term_grace=0.1, kill_grace=0.1)
        return forwarded, events

    def test_crlf_is_normalised_and_lf_is_the_only_delimiter(self):
        payload = b'{"type":"a"}\r\n{"type":"b\\u2028still one record"}\n'
        forwarded, events = self._drive(payload)
        self.assertEqual(forwarded, [b'{"type":"a"}', b'{"type":"b\\u2028still one record"}'])
        self.assertEqual(len(events), 2)

    def test_malformed_records_are_forwarded_but_not_queued(self):
        payload = b'{"type":"a"}\nnot json\n{"type":"b"}\n'
        forwarded, events = self._drive(payload)
        self.assertEqual(len(forwarded), 3)
        self.assertIn(b"not json", forwarded)
        self.assertEqual([e["type"] for e in events], ["a", "b"])

    def test_a_record_split_across_reads_is_reassembled(self):
        big = json.dumps({"type": "message_update", "text": "y" * 200_000}).encode("utf-8")
        forwarded, events = self._drive(big + b"\n")
        self.assertEqual(forwarded, [big])
        self.assertEqual(len(events), 1)

    def test_trailing_record_without_a_newline_is_still_forwarded(self):
        forwarded, events = self._drive(b'{"type":"a"}\n{"type":"b"}')
        self.assertEqual(len(forwarded), 2)
        self.assertEqual(len(events), 2)

    def test_blank_records_are_dropped(self):
        forwarded, events = self._drive(b'\n\n{"type":"a"}\n')
        self.assertEqual(forwarded, [b'{"type":"a"}'])
        self.assertEqual(len(events), 1)

    def test_json_arrays_are_not_treated_as_events(self):
        forwarded, events = self._drive(b'[1,2,3]\n{"type":"a"}\n')
        self.assertEqual(len(forwarded), 2)
        self.assertEqual([e["type"] for e in events], ["a"])


class _FakeBuffer:
    """A stdout buffer that accepts at most ``chunk`` bytes per ``write``.

    ``PYTHONUNBUFFERED=1`` (which the runner forces) makes the real
    ``sys.stdout.buffer`` a raw ``_io.FileIO``, whose ``write`` is one
    ``write(2)`` and may return a short count.
    """

    def __init__(self, chunk=None):
        self.chunk = chunk
        self.written = bytearray()
        self.writes = 0
        self.flushes = 0

    def write(self, data):
        self.writes += 1
        payload = bytes(data)
        if self.chunk is not None:
            payload = payload[: self.chunk]
        if self.chunk == 0:
            return 0
        self.written += payload
        return len(payload)

    def flush(self):
        self.flushes += 1


class ForwardRecordTest(unittest.TestCase):
    def _forward(self, record: bytes, buffer: "_FakeBuffer") -> None:
        with mock.patch.object(pirpc.sys, "stdout", mock.Mock(buffer=buffer)):
            pirpc.forward_record(record)

    def test_one_write_and_one_flush_per_record(self):
        buffer = _FakeBuffer()
        self._forward(b'{"type":"a"}', buffer)
        self.assertEqual(bytes(buffer.written), b'{"type":"a"}\n')
        self.assertEqual(buffer.writes, 1)
        self.assertEqual(buffer.flushes, 1)

    def test_a_short_write_is_completed_not_truncated(self):
        record = b'{"type":"message_update","text":"' + b"y" * 4096 + b'"}'
        buffer = _FakeBuffer(chunk=7)
        self._forward(record, buffer)
        self.assertEqual(bytes(buffer.written), record + b"\n")
        self.assertGreater(buffer.writes, 1)
        self.assertEqual(buffer.flushes, 1)

    def test_a_stalled_stdout_terminates_instead_of_spinning(self):
        buffer = _FakeBuffer(chunk=0)
        self._forward(b'{"type":"a"}', buffer)
        self.assertEqual(bytes(buffer.written), b"")
        self.assertEqual(buffer.writes, 1)
        self.assertEqual(buffer.flushes, 1)

    def test_a_none_returning_writer_does_not_raise(self):
        buffer = mock.Mock()
        buffer.write.return_value = None
        with mock.patch.object(pirpc.sys, "stdout", mock.Mock(buffer=buffer)):
            pirpc.forward_record(b'{"type":"a"}')
        self.assertEqual(buffer.write.call_count, 1)
        self.assertEqual(buffer.flush.call_count, 1)

    def test_the_lock_is_module_level(self):
        self.assertIsInstance(pirpc._STDOUT_LOCK, type(threading.Lock()))


if __name__ == "__main__":
    unittest.main()
