"""The budget controller: owns the deadline (BUILD_PLAN.md rev 6, §1).

Tracks elapsed time, cumulative output tokens, and a live tokens/s estimate
derived from recent assistant turns; predicts a mission's wall-clock cost from
its expected output-token size; and refuses to start a mission whose predicted
finish would blow through the deadline (minus a safety margin), or whose
predicted output would push the run's cumulative total past the hard ceiling
-- unless the caller explicitly accepts a partial result.

Pure and side-effect free apart from :func:`time.monotonic`: no I/O, no
threads, no blocking waits. Everything here is in-process bookkeeping that
``harness/__main__.py`` drives.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

#: How many of the most recent calls feed the live tokens/s estimate. A small
#: window lets the estimate track a real change in generation speed (a model
#: swap, throttling) without one slow or fast outlier dominating forever.
RECENT_WINDOW = 5


class BudgetController:
    """Owns the deadline for one harness run.

    ``deadline_monotonic`` is a :func:`time.monotonic` timestamp -- the same
    clock the harness's own shutdown logic already uses, so the two never
    drift apart.
    """

    def __init__(
        self,
        deadline_monotonic: float,
        tokens_per_s: float = 30.0,
        output_ceiling: int = 18000,
        finish_margin_s: float = 50.0,
    ) -> None:
        self.deadline_monotonic = float(deadline_monotonic)
        self.tokens_per_s = float(tokens_per_s) if tokens_per_s > 0 else 30.0
        self.output_ceiling = int(output_ceiling)
        self.finish_margin_s = float(finish_margin_s)

        self._started = time.monotonic()
        self.cumulative_output = 0
        self.peak_output = 0
        self._recent: List[Tuple[float, float]] = []  # (output_tokens, elapsed_s)
        self.predictions: List[Dict[str, Any]] = []

    # -- live accounting -----------------------------------------------------

    def observe_usage(self, output_tokens: int, elapsed_call_s: float) -> None:
        """Fold one completed call's output and wall time into the totals.

        ``elapsed_call_s`` is the wall time attributable to that call alone
        (not cumulative). Calls with no measurable output or elapsed time
        still count toward the cumulative total but never perturb the tok/s
        estimate -- a zero-output error turn says nothing about generation
        speed.
        """
        output_tokens = max(0, int(output_tokens or 0))
        elapsed_call_s = max(0.0, float(elapsed_call_s or 0.0))

        self.cumulative_output += output_tokens
        self.peak_output = max(self.peak_output, self.cumulative_output)

        if output_tokens > 0 and elapsed_call_s > 0:
            self._recent.append((output_tokens, elapsed_call_s))
            if len(self._recent) > RECENT_WINDOW:
                self._recent = self._recent[-RECENT_WINDOW:]
            total_tokens = sum(t for t, _ in self._recent)
            total_s = sum(s for _, s in self._recent)
            if total_s > 0:
                self.tokens_per_s = total_tokens / total_s

    # -- prediction ------------------------------------------------------

    def predict_seconds(self, output_tokens: int) -> float:
        """Wall-clock estimate for a mission producing ``output_tokens``."""
        rate = self.tokens_per_s if self.tokens_per_s > 0 else 30.0
        return max(0.0, float(output_tokens or 0)) / rate

    def remaining_s(self) -> float:
        return self.deadline_monotonic - time.monotonic()

    def can_start(
        self, predicted_output_tokens: int, accept_partial: bool = False
    ) -> Tuple[bool, str]:
        """Whether a mission predicted to emit ``predicted_output_tokens`` may start.

        Two independent refusals: not enough clock left to finish and still
        leave the shutdown margin, or (unless ``accept_partial``) the mission
        would push cumulative output past the hard ceiling.
        """
        predicted_output_tokens = max(0, int(predicted_output_tokens or 0))
        predicted_s = self.predict_seconds(predicted_output_tokens)
        remaining = self.remaining_s()
        if predicted_s + self.finish_margin_s > remaining:
            return False, (
                "predicted {0:.0f}s + margin {1:.0f}s exceeds {2:.0f}s remaining"
                .format(predicted_s, self.finish_margin_s, remaining)
            )
        if not accept_partial:
            projected = self.cumulative_output + predicted_output_tokens
            if projected > self.output_ceiling:
                return False, (
                    "cumulative {0} + predicted {1} = {2} exceeds ceiling {3} "
                    "(accept_partial=False)"
                    .format(
                        self.cumulative_output,
                        predicted_output_tokens,
                        projected,
                        self.output_ceiling,
                    )
                )
        return True, "ok"

    # -- prediction bookkeeping, for the snapshot -----------------------

    def begin_mission(self, label: str, predicted_output_tokens: int) -> int:
        """Record a prediction before a mission starts; returns its index.

        The index is passed back to :meth:`end_mission` once the mission's
        real numbers are known. Missions that never call ``end_mission`` (a
        crash mid-mission) simply keep their zero actuals in the snapshot.
        """
        predicted_output_tokens = max(0, int(predicted_output_tokens or 0))
        entry = {
            "label": label,
            "predicted_output": predicted_output_tokens,
            "predicted_s": round(self.predict_seconds(predicted_output_tokens), 3),
            "actual_output": 0,
            "actual_s": 0.0,
        }
        self.predictions.append(entry)
        return len(self.predictions) - 1

    def end_mission(self, index: int, actual_output: int, actual_s: float) -> None:
        if 0 <= index < len(self.predictions):
            self.predictions[index]["actual_output"] = max(0, int(actual_output or 0))
            self.predictions[index]["actual_s"] = round(max(0.0, float(actual_s or 0.0)), 3)

    # -- reporting --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        return {
            "elapsed_s": round(time.monotonic() - self._started, 3),
            "cumulative_output": self.cumulative_output,
            "peak_output": self.peak_output,
            "tokens_per_s": round(self.tokens_per_s, 3),
            "predictions": [dict(p) for p in self.predictions],
        }
