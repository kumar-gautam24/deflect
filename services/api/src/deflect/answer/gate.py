"""Decides whether an answer is trustworthy enough to show, or must be escalated."""

import math
from dataclasses import dataclass

from deflect.retrieval.search import Hit


@dataclass(frozen=True)
class GateThresholds:
    min_top_score: float = 0.0
    min_margin: float = 0.0
    require_grounded: bool = True

    def __post_init__(self) -> None:
        # Every eval run persists its thresholds as JSON so the run can be
        # reproduced, and JSON has no infinity. Rejecting it here gives a clear
        # error instead of an opaque failure at insert time.
        for name in ("min_top_score", "min_margin"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite, got {getattr(self, name)}")


@dataclass(frozen=True)
class GateDecision:
    escalate: bool
    reason: str | None
    top_score: float
    margin: float


def evaluate_gate(hits: list[Hit], grounded: bool, thresholds: GateThresholds) -> GateDecision:
    """Escalate unless retrieval was strong, unambiguous, and the answer stayed grounded.

    Ordering matters: retrieval failures are reported ahead of grounding failures
    because they are the actionable ones. An ungrounded answer over good context is a
    prompt problem; an ungrounded answer over bad context is a retrieval problem.
    """
    if not hits:
        return GateDecision(True, "no_results", 0.0, 0.0)

    top_score = hits[0].score
    # A lone hit has nothing to be ambiguous against, so its own score is the margin.
    margin = top_score - hits[1].score if len(hits) > 1 else top_score

    if top_score < thresholds.min_top_score:
        return GateDecision(True, "low_retrieval_score", top_score, margin)
    if margin < thresholds.min_margin:
        return GateDecision(True, "ambiguous_retrieval", top_score, margin)
    if thresholds.require_grounded and not grounded:
        return GateDecision(True, "ungrounded_answer", top_score, margin)

    return GateDecision(False, None, top_score, margin)
