"""The AgentCofounder harness: a standard-library-only Pi orchestrator.

The harness is spawned by ``src/run-challenge.ts`` as ``<python> -m harness``.
Its stdout carries **only** Pi event lines, forwarded verbatim, so the runner can
write them byte-for-byte into ``artifacts/runs/<id>/events.jsonl`` and hand them
to the organizer's own ``collectUsageFromJsonLines``. Every harness diagnostic
goes to stderr (see :mod:`harness.log`).

Modules:

- :mod:`harness.usage`  -- usage accumulation and the run-success predicate.
- :mod:`harness.log`    -- stderr logging, optionally coloured and tee'd to a file.
- :mod:`harness.pirpc`  -- the ``pi --mode rpc`` client.
- :mod:`harness.__main__` -- the CLI entry point.

Python 3.10 is the floor: every module uses ``from __future__ import annotations``
and the standard library only.
"""

from __future__ import annotations

__all__ = ["log", "pirpc", "usage"]
