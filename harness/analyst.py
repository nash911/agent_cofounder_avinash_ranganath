"""The Analyst: one ``json_schema`` gateway call from the idea text to ``spec.json``.

The Phase-2 seed of the reasoning pipeline (C4). Deliberately small: extract the
application's identity, nothing else -- Architect/Supervisor/Reviewer own the
rest in Phase 3. Never blocks the build: any failure (network, parse, disk) is
logged to stderr and swallowed, and the Pi session proceeds without a spec.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Optional, Union

from .gateway import GatewayClient
from .log import log, warn

#: Terse by design -- P08 measured json_schema + terse decisions as 4/4 valid
#: at ~52 tokens; a chatty system prompt only burns output-token budget.
SYSTEM_PROMPT = "Extract the application identity from the idea. No commentary."

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "app_name": {"type": "string", "description": "The application's name, 2-4 words."},
        "tagline": {"type": "string", "description": "A tagline, at most 12 words."},
        "summary": {
            "type": "string",
            "description": "One paragraph describing what the app does for the user.",
        },
        "primary_entity": {
            "type": "string",
            "description": "The single domain noun the app is organised around.",
        },
    },
    "required": ["app_name", "tagline", "summary", "primary_entity"],
    "additionalProperties": False,
}

MAX_TOKENS = 400


def run_analyst(
    client: GatewayClient,
    idea_text: str,
    harness_dir: Union[str, pathlib.Path],
    deadline: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Writes ``<harness_dir>/spec.json`` and returns it, or ``None`` on any failure.

    ``deadline`` is an optional ``time.monotonic()`` instant bounding the
    whole call (including retries and backoff) -- see
    ``harness.__main__.run_analyst`` for how it is derived from the harness's
    own wall-clock budget. ``None`` means "no bound", matching
    :meth:`GatewayClient.chat`'s own default.
    """
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": idea_text},
        ]
        obj, result = client.json_schema(
            messages,
            name="app_spec",
            schema=SCHEMA,
            label="analyst",
            max_tokens=MAX_TOKENS,
            deadline=deadline,
        )
        if obj is None:
            warn(
                "analyst · no usable spec ({0} attempts, status {1}, error {2})".format(
                    result.attempts, result.status, result.error
                )
            )
            return None
        if not isinstance(obj, dict):
            warn("analyst · schema response was not a JSON object: {0!r}".format(type(obj)))
            return None

        harness_path = pathlib.Path(harness_dir)
        harness_path.mkdir(parents=True, exist_ok=True)
        spec_path = harness_path / "spec.json"
        spec_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log("analyst", "spec.json written ({0} attempt(s))".format(result.attempts))
        return obj
    except Exception as exc:  # noqa: BLE001 -- the Analyst must never block the build
        warn("analyst · unexpected failure: {0}: {1}".format(type(exc).__name__, exc))
        return None
