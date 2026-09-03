"""``--thinking`` coercion.

Pi validates ``--thinking`` against an exact, case-sensitive list; on a miss it
prints a warning, ignores the flag and uses its own configured default
(``medium``). An organizer typo would therefore turn thinking on for a judged
run -- the largest measured cost lever -- so the harness coerces anything it does
not recognise to ``off`` and warns, but never fails the run.
"""

from __future__ import annotations

import unittest

from harness.__main__ import VALID_THINKING_LEVELS, normalize_thinking


class NormalizeThinkingTest(unittest.TestCase):
    def test_every_valid_level_survives(self):
        for level in VALID_THINKING_LEVELS:
            self.assertEqual(normalize_thinking(level), level)

    def test_case_and_whitespace_are_normalised(self):
        self.assertEqual(normalize_thinking("OFF"), "off")
        self.assertEqual(normalize_thinking(" High "), "high")

    def test_empty_and_unrecognised_values_become_off(self):
        for raw in (None, "", "   ", "none", "disabled", "true", "1"):
            self.assertEqual(normalize_thinking(raw), "off")


if __name__ == "__main__":
    unittest.main()
