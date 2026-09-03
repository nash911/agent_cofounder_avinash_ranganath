"""Gateway API key resolution (C3).

Precedence: ``BERGET_API_KEY`` > ``CHALLENGE_API_KEY`` > ``OPENAI_API_KEY``,
first non-empty wins. If ``BERGET_API_KEY`` itself was empty, it is set from
whichever candidate won, so every downstream reader of the environment (Pi
included, via ``.pi-agent/models.json``'s ``"apiKey": "$BERGET_API_KEY"``) sees
the same key without a second resolution pass.

The key value is returned to the caller and never logged by this module --
callers must log only the ``name`` half of the returned pair.
"""

from __future__ import annotations

import os
from typing import Tuple

#: Checked in this order; the first non-empty value wins.
_CANDIDATES = ("BERGET_API_KEY", "CHALLENGE_API_KEY", "OPENAI_API_KEY")


def resolve_api_key() -> Tuple[str, str]:
    """Returns ``(key, name_used)``; ``("", "")`` when nothing is set."""
    for name in _CANDIDATES:
        value = os.environ.get(name)
        if value:
            if not os.environ.get("BERGET_API_KEY"):
                os.environ["BERGET_API_KEY"] = value
            return value, name
    return "", ""
