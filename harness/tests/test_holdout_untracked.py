"""Nothing under ``evals/`` may ever be tracked (BUILD_PLAN rev 5, Phase 4).

A blind evaluation is only blind while the holdout ideas stay out of the
repository. :mod:`harness.eval` refuses ``--cases`` paths inside the working
tree, which stops the runner from reading a committed holdout; this test closes
the other half of the hole -- a holdout, a snapshot or a report that someone
copies into ``evals/`` and commits by reflex.

Both checks are read-only: the ``.gitignore`` line is asserted, not written,
and ``git ls-files`` never mutates anything.
"""

from __future__ import annotations

import subprocess
import unittest

from harness.tests import support

HOLDOUT_IGNORE_LINE = "evals/"


class HoldoutUntrackedTest(unittest.TestCase):
    def test_gitignore_excludes_the_evals_directory(self):
        text = (support.REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines()]
        self.assertIn(
            HOLDOUT_IGNORE_LINE,
            lines,
            ".gitignore must contain a bare {0!r} line so holdout ideas, run "
            "snapshots and eval reports can never be staged".format(HOLDOUT_IGNORE_LINE),
        )

    def test_git_tracks_nothing_under_evals(self):
        try:
            listed = subprocess.run(
                ["git", "ls-files", "evals"],
                cwd=str(support.REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            self.skipTest("git is unavailable: {0}".format(exc))
        if listed.returncode != 0:  # pragma: no cover - not a checkout
            self.skipTest("not a git checkout: {0}".format(listed.stderr.strip()))
        self.assertEqual(
            listed.stdout.strip(),
            "",
            "these holdout files are tracked and must be removed from the index:\n"
            + listed.stdout,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
