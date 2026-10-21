"""Multi-factor ranking engine for the opportunity inbox.

Factors (each normalised to 0..1):
1. Eligibility score   — from the eligibility report's overall verdict
2. Deadline urgency    — closer deadlines score higher (expired = 0)
3. Fit score           — keyword overlap with applicant's stated interests
4. Award value         — higher funding amount scores higher
5. Total app cost      — lower cost scores higher
6. Feasibility         — simpler application requirements score higher

The final score is a weighted sum. Weights are tunable but default to
eligibility > deadline > fit > value > cost > feasibility, reflecting the
Kenya-applicant priority: don't waste time on things you can't apply to.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from app.scrapers.eligibility import assess, extract_funding_amount

if TYPE_CHECKING:
    from app.models import Opportunity

DEFAULT_WEIGHTS = {
    "eligibility": 0.30,
    "deadline": 0.20,
    "fit": 0.15,
    "value": 0.15,
    "cost": 0.10,
    "feasibility": 0.10,
}

_URGENCY_HORIZON_DAYS = 90  # a deadline this far out (or further) scores 0 urgency


def _eligibility_score(report_dict: dict) -> float:
    overall = report_dict.get("overall", "unknown")
    if overall == "eligible":
        return 1.0
    if overall == "unknown":
        return 0.5
    return 0.0


def _deadline_score(opp: "Opportunity") -> float:
    if opp.deadline_at is None:
        return 0.1  # unknown deadline — low urgency but not zero
    days = (opp.deadline_at - date.today()).days
    if days < 0:
        return 0.0  # expired
    if days == 0:
        return 1.0
    if days >= _URGENCY_HORIZON_DAYS:
        return 0.0
    return max(0.0, 1.0 - (days / _URGENCY_HORIZON_DAYS))


def _fit_score(opp: "Opportunity", applicant_interests: list[str]) -> float:
    if not applicant_interests:
        return 0.5  # neutral when no interests declared
    blob = " ".join(
        [
            opp.title or "",
            opp.description or "",
            opp.summary or "",
            opp.field or "",
            opp.tags or "",
        ]
    ).lower()
    if not blob.strip():
        return 0.0
    hits = sum(1 for kw in applicant_interests if kw.lower() in blob)
    return min(1.0, hits / max(1, len(applicant_interests)))


def _value_score(opp: "Opportunity") -> float:
    text = " ".join([opp.title or "", opp.description or "", opp.summary or ""])
    amount = extract_funding_amount(text)
    if amount is None:
        return 0.5  # unknown value — neutral
    # Log-scale: $500 -> ~0.3, $5k -> ~0.55, $50k -> ~0.8, $500k+ -> ~1.0
    if amount <= 0:
        return 0.0
    import math

    return min(1.0, math.log10(amount + 1) / 6.0)


def _cost_score(opp: "Opportunity") -> float:
    """Lower application cost = higher score. We infer cost from text."""
    text = " ".join([opp.description or "", opp.summary or ""]).lower()
    if "no application fee" in text or "free to apply" in text:
        return 1.0
    if "application fee" in text or "registration fee" in text:
        return 0.2
    return 0.7  # assume no fee unless stated


def _feasibility_score(opp: "Opportunity") -> float:
    """Simpler requirements = higher score. Heuristic based on text length
    and presence of heavy requirement keywords."""
    text = " ".join([opp.description or "", opp.summary or ""]).lower()
    heavy = ["phd required", "doctorate required", "5 years experience", "10 years", "extensive experience"]
    light = ["no experience required", "entry level", "beginner", "open to all"]
    if any(kw in text for kw in light):
        return 1.0
    if any(kw in text for kw in heavy):
        return 0.3
    return 0.6


def rank_opportunity(
    opp: "Opportunity",
    applicant_interests: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    """Return a ranking dict with per-factor scores and a final 0..100 score."""
    w = weights or DEFAULT_WEIGHTS
    report = assess(opp).to_dict()
    interests = applicant_interests or []

    scores = {
        "eligibility": _eligibility_score(report),
        "deadline": _deadline_score(opp),
        "fit": _fit_score(opp, interests),
        "value": _value_score(opp),
        "cost": _cost_score(opp),
        "feasibility": _feasibility_score(opp),
    }
    final = sum(scores[k] * w.get(k, 0) for k in scores) * 100
    return {
        "score": round(final, 1),
        "factors": {k: round(v, 3) for k, v in scores.items()},
        "eligibility": report,
    }
