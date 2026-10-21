"""Evidence-backed eligibility engine for a Kenya-based applicant.

The core principle: never fabricate eligibility. Every verdict is one of
``"eligible"`` (verified from explicit evidence), ``"ineligible"`` (verified
from explicit evidence), or ``"unknown"`` (no decisive evidence found).
"unknown" is a first-class state — it is NOT the same as "eligible", so the
ranking and UI can separate "we confirmed you qualify" from "we couldn't
tell and a human must check the source page".

Each check returns an ``EligibilityCheck`` with:
- ``verdict``: "eligible" | "ineligible" | "unknown"
- ``evidence``: the exact substring(s) from the listing that drove the verdict
- ``reason``: a short human-readable explanation
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Opportunity


@dataclass
class EligibilityCheck:
    verdict: str  # "eligible" | "ineligible" | "unknown"
    reason: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class EligibilityReport:
    kenya: EligibilityCheck
    remote: EligibilityCheck
    fully_funded: EligibilityCheck
    direct_assistance: EligibilityCheck

    @property
    def overall(self) -> str:
        """Most-optimistic-safe roll-up: if any dimension is 'ineligible',
        the overall is 'ineligible'. Otherwise, if any is 'unknown', overall
        is 'unknown'. Only 'eligible' if every applicable dimension is
        'eligible'."""
        checks = [self.kenya, self.remote, self.fully_funded, self.direct_assistance]
        if any(c.verdict == "ineligible" for c in checks):
            return "ineligible"
        if any(c.verdict == "unknown" for c in checks):
            return "unknown"
        return "eligible"

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "kenya": _check_to_dict(self.kenya),
            "remote": _check_to_dict(self.remote),
            "fully_funded": _check_to_dict(self.fully_funded),
            "direct_assistance": _check_to_dict(self.direct_assistance),
        }


def _check_to_dict(c: EligibilityCheck) -> dict:
    return {"verdict": c.verdict, "reason": c.reason, "evidence": c.evidence}


# ---------------------------------------------------------------------------
# Keyword sets — tuned to minimise false positives. A match must be explicit
# in the listing text; absence always yields "unknown", never "eligible".
# ---------------------------------------------------------------------------
_KENYA_POSITIVE = [
    "kenya",
    "kenyan",
    "east africa",
    "east african",
    "sub-saharan africa",
    "sub saharan africa",
    "africa",
    "african",
    "developing countries",
    "developing country",
    "global",
    "worldwide",
    "international",
    "all countries",
    "all nationalities",
    "open to all",
    "any nationality",
]
_KENYA_NEGATIVE = [
    "only open to",
    "restricted to",
    "not available in",
    "excludes",
    "us citizens only",
    "uk residents only",
    "eu citizens only",
    "australian citizens only",
    "canadian citizens only",
    "india only",
    "nigeria only",
    "south africa only",
    "ghana only",
    "tanzania only",
    "uganda only",
    "not eligible in kenya",
]

_REMOTE_POSITIVE = [
    "remote",
    "work from home",
    "work-from-home",
    "telecommute",
    "distributed team",
    "fully remote",
    "100% remote",
    "anywhere in the world",
    "location independent",
    "virtual",
]
_REMOTE_NEGATIVE = [
    "on-site only",
    "onsite only",
    "must relocate",
    "in-person required",
    "no remote",
    "not remote",
]

_FULLY_FUNDED_POSITIVE = [
    "fully funded",
    "full funding",
    "fully-funded",
    "full scholarship",
    "full tuition",
    "tuition waiver",
    "all expenses paid",
    "fully sponsored",
    "costs covered",
    "travel and accommodation covered",
    "stipend included",
    "living stipend",
    "covers tuition",
    "covers travel",
    "covers accommodation",
]
_FULLY_FUNDED_NEGATIVE = [
    "partial funding",
    "partially funded",
    "tuition only",
    "no stipend",
    "self-funded",
    "self funded",
    "unpaid",
    "no funding",
    "fee required",
    "application fee",
]

_DIRECT_ASSISTANCE_POSITIVE = [
    "direct financial assistance",
    "cash assistance",
    "cash grant",
    "direct cash",
    "unconditional cash",
    "emergency cash",
    "cash transfer",
    "financial aid",
    "monetary assistance",
    "direct support",
    "cash support",
    "stipend",
    "living allowance",
    "subsistence allowance",
]
_DIRECT_ASSISTANCE_NEGATIVE = [
    "no cash",
    "in-kind only",
    "non-monetary",
    "no financial aid",
]


def _scan(text: str, positives: list[str], negatives: list[str]) -> tuple[str, list[str]]:
    """Return (verdict, evidence). Checks negatives first — an explicit
    exclusion beats a positive mention."""
    if not text:
        return "unknown", []
    lower = text.lower()
    for neg in negatives:
        if neg in lower:
            idx = lower.index(neg)
            snippet = text[max(0, idx - 30) : idx + len(neg) + 30].strip()
            return "ineligible", [snippet]
    hits: list[str] = []
    for pos in positives:
        if pos in lower:
            idx = lower.index(pos)
            snippet = text[max(0, idx - 30) : idx + len(pos) + 30].strip()
            hits.append(snippet)
    if hits:
        return "eligible", hits
    return "unknown", []


def _opportunity_text(opp: Opportunity) -> str:
    """Concatenate all free-text fields into one searchable blob."""
    parts = [opp.title or "", opp.description or "", opp.summary or "", opp.tags or ""]
    if opp.location:
        parts.append(opp.location)
    if opp.source_name:
        parts.append(opp.source_name)
    return " \n ".join(parts)


def assess_kenya(opp: Opportunity) -> EligibilityCheck:
    text = _opportunity_text(opp)
    verdict, evidence = _scan(text, _KENYA_POSITIVE, _KENYA_NEGATIVE)
    reason_map = {
        "eligible": "Listing explicitly mentions Kenya, Africa, or open/global eligibility.",
        "ineligible": "Listing explicitly restricts to other countries/regions.",
        "unknown": "No explicit country-eligibility statement found — verify on source page.",
    }
    return EligibilityCheck(verdict=verdict, reason=reason_map[verdict], evidence=evidence)


def assess_remote(opp: Opportunity) -> EligibilityCheck:
    text = _opportunity_text(opp)
    verdict, evidence = _scan(text, _REMOTE_POSITIVE, _REMOTE_NEGATIVE)
    reason_map = {
        "eligible": "Listing explicitly mentions remote work.",
        "ineligible": "Listing explicitly requires on-site / relocation.",
        "unknown": "No explicit remote/on-site statement — verify on source page.",
    }
    return EligibilityCheck(verdict=verdict, reason=reason_map[verdict], evidence=evidence)


def assess_fully_funded(opp: Opportunity) -> EligibilityCheck:
    text = _opportunity_text(opp)
    verdict, evidence = _scan(text, _FULLY_FUNDED_POSITIVE, _FULLY_FUNDED_NEGATIVE)
    reason_map = {
        "eligible": "Listing explicitly mentions full funding / covered costs.",
        "ineligible": "Listing mentions partial funding, fees, or no stipend.",
        "unknown": "No explicit funding-coverage statement — verify on source page.",
    }
    return EligibilityCheck(verdict=verdict, reason=reason_map[verdict], evidence=evidence)


def assess_direct_assistance(opp: Opportunity) -> EligibilityCheck:
    text = _opportunity_text(opp)
    verdict, evidence = _scan(text, _DIRECT_ASSISTANCE_POSITIVE, _DIRECT_ASSISTANCE_NEGATIVE)
    reason_map = {
        "eligible": "Listing explicitly mentions direct cash/financial assistance.",
        "ineligible": "Listing mentions in-kind only / no cash.",
        "unknown": "No explicit direct-assistance statement — verify on source page.",
    }
    return EligibilityCheck(verdict=verdict, reason=reason_map[verdict], evidence=evidence)


def assess(opp: Opportunity) -> EligibilityReport:
    """Run all four eligibility checks and return a full report."""
    return EligibilityReport(
        kenya=assess_kenya(opp),
        remote=assess_remote(opp),
        fully_funded=assess_fully_funded(opp),
        direct_assistance=assess_direct_assistance(opp),
    )


# ---------------------------------------------------------------------------
# Filter helper — used by the ranked-inbox endpoint to filter by eligibility
# dimension without duplicating the keyword logic.
# ---------------------------------------------------------------------------
_FILTER_MAP = {
    "kenya": assess_kenya,
    "remote": assess_remote,
    "fully_funded": assess_fully_funded,
    "direct_assistance": assess_direct_assistance,
    "grants": None,
    "scholarships": None,
}


def passes_filter(opp: Opportunity, filter_name: str) -> bool:
    """Return True if the opportunity passes the named filter.

    For type-based filters (grants, scholarships), this checks
    opportunity_type. For eligibility filters, it returns True only when
    the verdict is 'eligible' (not 'unknown' — that would be fabrication)."""
    if filter_name in ("grants",):
        return opp.opportunity_type == "grant"
    if filter_name in ("scholarships",):
        return opp.opportunity_type == "scholarship"
    fn = _FILTER_MAP.get(filter_name)
    if fn is None:
        return True
    return fn(opp).verdict == "eligible"


# ---------------------------------------------------------------------------
# Funding-amount extraction — best-effort, returns None when not found.
# Matches patterns like "$5,000", "USD 10,000", "€2,500", "£1,000-3,000".
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(
    r"(?:USD|US\$|\$|€|£|KES|KSh|EUR|GBP)\s?([\d,]+(?:\.\d+)?)"
    r"(?:\s?[-–to]\s?(?:USD|US\$|\$|€|£|KES|KSh|EUR|GBP)?\s?([\d,]+(?:\.\d+)?))?",
    re.IGNORECASE,
)


def extract_funding_amount(text: str) -> float | None:
    """Extract a funding amount from free text. Returns the max when a
    range is given (the upper bound is the relevant figure for ranking)."""
    if not text:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    def _num(s: str) -> float:
        return float(s.replace(",", ""))
    low = _num(m.group(1))
    if m.group(2):
        return _num(m.group(2))
    return low
