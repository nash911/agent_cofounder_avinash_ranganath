"""Tests for the harness package.

Run from the repository root:

    python3 -m unittest discover -s harness/tests -t .

Every test here uses ``harness/tests/fake_pi.py`` as the Pi stand-in
(``HARNESS_PI_BIN``). Nothing in this directory ever reaches a real model.
"""

from __future__ import annotations
