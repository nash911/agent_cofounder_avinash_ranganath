"""Unit tests for :mod:`harness.prefix` (C5, harness side): grouping by tool
count and detecting a drifted (non-unique) system-prompt hash per group."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from harness import prefix


def _write_jsonl(path: pathlib.Path, records) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class CheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "payload.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_none(self):
        self.assertIsNone(prefix.check(self.path))

    def test_a_directory_at_the_path_is_not_a_file_and_returns_none(self):
        directory = pathlib.Path(self._tmp.name) / "adir"
        directory.mkdir()
        self.assertIsNone(prefix.check(directory))


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "payload.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_group_single_hash_is_not_warned(self):
        _write_jsonl(
            self.path,
            [
                {"tools": 3, "system_sha256": "aaa"},
                {"tools": 3, "system_sha256": "aaa"},
                {"tools": 3, "system_sha256": "aaa"},
            ],
        )
        summary = prefix.summarize(self.path)
        self.assertEqual(summary["records"], 3)
        self.assertFalse(summary["warned"])
        self.assertEqual(summary["groups"]["3"]["count"], 3)
        self.assertEqual(summary["groups"]["3"]["distinct_hashes"], 1)

    def test_two_distinct_hashes_in_one_group_is_warned(self):
        _write_jsonl(
            self.path,
            [
                {"tools": 2, "system_sha256": "aaa"},
                {"tools": 2, "system_sha256": "bbb"},
            ],
        )
        summary = prefix.summarize(self.path)
        self.assertTrue(summary["warned"])
        self.assertEqual(summary["groups"]["2"]["distinct_hashes"], 2)
        self.assertCountEqual(summary["groups"]["2"]["hashes"], ["aaa", "bbb"])

    def test_groups_are_independent_by_tool_count(self):
        _write_jsonl(
            self.path,
            [
                {"tools": 0, "system_sha256": "sys-a"},
                {"tools": 0, "system_sha256": "sys-a"},
                {"tools": 5, "system_sha256": "sys-b"},
                {"tools": 5, "system_sha256": "sys-c"},  # drift only in the tools=5 group
            ],
        )
        summary = prefix.summarize(self.path)
        self.assertFalse(summary["groups"]["0"]["distinct_hashes"] > 1)
        self.assertEqual(summary["groups"]["5"]["distinct_hashes"], 2)
        self.assertTrue(summary["warned"])

    def test_missing_tools_field_defaults_to_group_zero(self):
        _write_jsonl(self.path, [{"system_sha256": "aaa"}])
        summary = prefix.summarize(self.path)
        self.assertIn("0", summary["groups"])
        self.assertEqual(summary["groups"]["0"]["count"], 1)

    def test_missing_system_sha256_is_counted_but_contributes_no_hash(self):
        _write_jsonl(self.path, [{"tools": 1}, {"tools": 1}])
        summary = prefix.summarize(self.path)
        self.assertEqual(summary["groups"]["1"]["count"], 2)
        self.assertEqual(summary["groups"]["1"]["distinct_hashes"], 0)
        self.assertFalse(summary["warned"])

    def test_malformed_lines_and_blank_lines_are_skipped(self):
        self.path.write_text(
            '{"tools": 1, "system_sha256": "aaa"}\n'
            "\n"
            "not json at all {{{\n"
            '["also", "not", "an", "object"]\n'
            '{"tools": 1, "system_sha256": "aaa"}\n',
            encoding="utf-8",
        )
        summary = prefix.summarize(self.path)
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["groups"]["1"]["count"], 2)

    def test_non_integer_tools_value_falls_back_to_zero(self):
        _write_jsonl(self.path, [{"tools": "not-a-number", "system_sha256": "aaa"}])
        summary = prefix.summarize(self.path)
        self.assertIn("0", summary["groups"])

    def test_empty_file_has_no_groups_and_is_not_warned(self):
        self.path.write_text("", encoding="utf-8")
        summary = prefix.summarize(self.path)
        self.assertEqual(summary["records"], 0)
        self.assertEqual(summary["groups"], {})
        self.assertFalse(summary["warned"])


if __name__ == "__main__":
    unittest.main()
