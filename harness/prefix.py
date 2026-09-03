"""Prompt-prefix byte-identity check (BUILD_PLAN.md rev 6, §1, C5 -- harness side).

``solution/extensions/thinking-guard.ts`` appends one record per
``before_provider_request`` to ``HARNESS_PAYLOAD_LOG`` (when set), grouped by
tool count (``payload.tools ? payload.tools.length : 0``) and carrying a
``system_sha256`` of the first system message. If a single tool-count group
ever shows more than one distinct hash, the prompt prefix drifted across
sessions or turns -- exactly the volatility BUILD_PLAN.md §5 forbids -- so
this module reads that log at the end of a run, summarizes it by group, and
warns on stderr.

Read-only and tolerant: a record missing ``tools`` or ``system_sha256``
(an older log shape, a partial write) is still counted rather than dropped.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional, Union

from .log import log, warn

PathLike = Union[str, "os.PathLike[str]"]  # noqa: F821 - annotation only


def _read_records(path: pathlib.Path) -> List[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _tools_key(record: Dict[str, Any]) -> int:
    tools = record.get("tools")
    try:
        return int(tools) if tools is not None else 0
    except (TypeError, ValueError):
        return 0


def summarize(path: PathLike) -> Dict[str, Any]:
    """Group payload-log records by tool count and count distinct system hashes.

    Returns ``{"records": n, "groups": {"<tools>": {"count", "distinct_hashes",
    "hashes"}}, "warned": bool}``. Logs one line per group (``warn`` when that
    group carries more than one distinct hash, ``log`` otherwise) so the
    summary is visible on stderr without the caller needing to reformat it.
    """
    path = pathlib.Path(path)
    records = _read_records(path)

    groups: "Dict[int, Dict[str, Any]]" = {}
    for record in records:
        tools = _tools_key(record)
        bucket = groups.setdefault(tools, {"count": 0, "hashes": set()})
        bucket["count"] += 1
        sha = record.get("system_sha256")
        if sha:
            bucket["hashes"].add(str(sha))

    summary: Dict[str, Any] = {}
    warned = False
    for tools in sorted(groups):
        bucket = groups[tools]
        hashes = sorted(bucket["hashes"])
        distinct = len(hashes)
        summary[str(tools)] = {
            "count": bucket["count"],
            "distinct_hashes": distinct,
            "hashes": hashes,
        }
        message = "tools={0}: {1} distinct system prompt hash(es)".format(tools, distinct)
        if distinct > 1:
            warn("prefix · {0}".format(message))
            warned = True
        else:
            log("prefix", message)

    return {"records": len(records), "groups": summary, "warned": warned}


def check(path: PathLike) -> Optional[Dict[str, Any]]:
    """``summarize`` a payload log if it exists; ``None`` (silently) if not.

    A run with the thinking guard disabled, or one that never made a provider
    request, has no log at all -- that is not itself a finding.
    """
    resolved = pathlib.Path(path)
    if not resolved.is_file():
        return None
    return summarize(resolved)
