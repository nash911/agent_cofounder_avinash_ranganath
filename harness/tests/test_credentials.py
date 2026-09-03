"""Unit tests for :mod:`harness.credentials`: precedence, aliasing, no leaking."""

from __future__ import annotations

import io
import os
import sys
import unittest
from unittest import mock

from harness import credentials
from harness.log import log

_ENV_VARS = ("BERGET_API_KEY", "CHALLENGE_API_KEY", "OPENAI_API_KEY")


class CredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in _ENV_VARS}
        for name in _ENV_VARS:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_nothing_set_returns_empty_pair(self):
        self.assertEqual(credentials.resolve_api_key(), ("", ""))

    def test_berget_wins_over_challenge_and_openai(self):
        os.environ["BERGET_API_KEY"] = "berget-key"
        os.environ["CHALLENGE_API_KEY"] = "challenge-key"
        os.environ["OPENAI_API_KEY"] = "openai-key"
        key, name = credentials.resolve_api_key()
        self.assertEqual((key, name), ("berget-key", "BERGET_API_KEY"))

    def test_challenge_wins_over_openai_when_berget_absent(self):
        os.environ["CHALLENGE_API_KEY"] = "challenge-key"
        os.environ["OPENAI_API_KEY"] = "openai-key"
        key, name = credentials.resolve_api_key()
        self.assertEqual((key, name), ("challenge-key", "CHALLENGE_API_KEY"))
        self.assertEqual(os.environ.get("BERGET_API_KEY"), "challenge-key", "aliased for downstream readers")

    def test_openai_used_as_last_resort(self):
        os.environ["OPENAI_API_KEY"] = "openai-key"
        key, name = credentials.resolve_api_key()
        self.assertEqual((key, name), ("openai-key", "OPENAI_API_KEY"))
        self.assertEqual(os.environ.get("BERGET_API_KEY"), "openai-key")

    def test_existing_berget_key_is_never_overwritten(self):
        os.environ["BERGET_API_KEY"] = "already-set"
        os.environ["OPENAI_API_KEY"] = "openai-key"
        key, name = credentials.resolve_api_key()
        # BERGET_API_KEY itself was non-empty, so it wins on precedence too.
        self.assertEqual((key, name), ("already-set", "BERGET_API_KEY"))
        self.assertEqual(os.environ["BERGET_API_KEY"], "already-set")

    def test_empty_string_values_are_treated_as_unset(self):
        os.environ["BERGET_API_KEY"] = ""
        os.environ["CHALLENGE_API_KEY"] = "challenge-key"
        key, name = credentials.resolve_api_key()
        self.assertEqual((key, name), ("challenge-key", "CHALLENGE_API_KEY"))

    def test_name_is_loggable_but_value_never_appears_in_the_log(self):
        os.environ["CHALLENGE_API_KEY"] = "super-secret-value-12345"
        key, name = credentials.resolve_api_key()

        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            # Mirrors the caller contract: log only the name, never the value.
            log("harness", "resolved gateway API key via {0}".format(name))

        output = captured.getvalue()
        self.assertIn("CHALLENGE_API_KEY", output)
        self.assertNotIn(key, output)
        self.assertNotIn("super-secret-value-12345", output)


if __name__ == "__main__":
    unittest.main()
