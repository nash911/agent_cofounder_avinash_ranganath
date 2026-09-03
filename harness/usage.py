"""Usage accumulation and the harness run-success predicate.

Pi's own numbers are the only numbers the harness ever reports. Usage arrives
on assistant ``message_end`` events; an aborted or errored turn still carries a
structurally valid **all-zero** usage object, which is why
:func:`is_success_message` checks ``stopReason`` *and* ``output`` together.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

#: Stop reasons that mean the assistant turn produced nothing usable.
FAILED_STOP_REASONS = frozenset({"error", "aborted"})


@dataclass
class Usage:
    """Running totals over one or more assistant ``message_end`` payloads."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0
    reasoning: int = 0
    cost: float = 0.0
    calls: int = 0

    def add(self, usage: Dict[str, Any]) -> None:
        """Fold one Pi ``usage`` object into the totals."""
        self.input += _as_int(usage.get("input"))
        self.output += _as_int(usage.get("output"))
        self.cache_read += _as_int(usage.get("cacheRead"))
        self.cache_write += _as_int(usage.get("cacheWrite"))
        self.total += _as_int(usage.get("totalTokens"))
        self.reasoning += _as_int(usage.get("reasoning"))
        cost = usage.get("cost")
        if isinstance(cost, dict):
            self.cost += _as_float(cost.get("total"))
        else:
            self.cost += _as_float(cost)
        self.calls += 1

    @property
    def efficiency(self) -> float:
        """The organizers' efficiency score: ``input + output*3 + cache_read*0.1``."""
        return self.input + self.output * 3 + self.cache_read * 0.1

    @property
    def prefix(self) -> int:
        """What the model saw as prompt: fresh input plus cache reads and writes."""
        return self.input + self.cache_read + self.cache_write

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model_calls": self.calls,
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "total": self.total,
            "reasoning": self.reasoning,
            "cost": round(self.cost, 6),
            "efficiency_points": round(self.efficiency, 1),
        }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_events(text: str) -> List[Dict[str, Any]]:
    """Parse a JSONL event stream, skipping blank and malformed records."""
    events: List[Dict[str, Any]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def assistant_message_ends(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The assistant ``message`` objects carried by ``message_end`` events."""
    out: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "message_end":
            continue
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            out.append(message)
    return out


def message_text(message: Dict[str, Any]) -> str:
    """Concatenated text blocks of an assistant message."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_success_message(message: Dict[str, Any]) -> bool:
    """True for an assistant message that actually produced output tokens.

    ``usage.output > 0`` alone is not enough: an aborted or errored turn emits a
    structurally valid all-zero usage object, and ``model_calls > 0`` would then
    accept a run in which every call failed.
    """
    if not isinstance(message, dict):
        return False
    if message.get("stopReason") in FAILED_STOP_REASONS:
        return False
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return False
    return _as_int(usage.get("output")) > 0


def run_succeeded(events: Iterable[Dict[str, Any]]) -> bool:
    """The harness exit predicate.

    Exit 0 iff at least one assistant ``message_end`` had a ``stopReason`` that
    is neither ``error`` nor ``aborted`` **and** reported ``usage.output > 0``.
    """
    return any(is_success_message(message) for message in assistant_message_ends(events))


def last_stop_reason(events: Iterable[Dict[str, Any]]) -> Optional[str]:
    """The ``stopReason`` of the final assistant message, if any."""
    messages = assistant_message_ends(events)
    if not messages:
        return None
    reason = messages[-1].get("stopReason")
    return None if reason is None else str(reason)


def summarize(events: Iterable[Dict[str, Any]]) -> Usage:
    """Total usage across every assistant ``message_end`` in ``events``."""
    total = Usage()
    for message in assistant_message_ends(events):
        usage = message.get("usage")
        total.add(usage if isinstance(usage, dict) else {})
    return total
