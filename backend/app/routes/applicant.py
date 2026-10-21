"""Applicant autopilot routes — ranked inbox, submission ledger, checklist.

These endpoints serve the personal-applicant use case (Kenya-based single
applicant). They are deliberately separate from the public opportunity
listing routes and the admin moderation routes.

Submission rules enforced here:
- State machine transitions are validated — illegal jumps are rejected.
- 'receipt_verified' requires a non-empty receipt_ref.
- 'submitted' requires a submitted_at timestamp (set automatically).
- No endpoint fabricates qualifications, references, or documents.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    ApplicantProfile,
    ApplicationChecklist,
    Opportunity,
    SubmissionLedger,
)
from app.schemas import (
    ApplicantProfileResponse,
    ApplicantProfileUpdate,
    ChecklistItemCreate,
    ChecklistItemResponse,
    ChecklistItemUpdate,
    LedgerCreate,
    LedgerResponse,
    LedgerUpdate,
)
from app.scrapers.eligibility import passes_filter
from app.scrapers.ranking import rank_opportunity

router = APIRouter(prefix="/applicant", tags=["Applicant Autopilot"])

# ---------------------------------------------------------------------------
# Ledger state machine
# ---------------------------------------------------------------------------
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"submitted", "rejected"},
    "submitted": {"receipt_verified", "shortlisted", "awarded", "rejected"},
    "receipt_verified": {"shortlisted", "awarded", "paid", "rejected"},
    "shortlisted": {"awarded", "rejected"},
    "awarded": {"paid", "rejected"},
    "paid": set(),  # terminal
    "rejected": {"pending"},  # allow re-queue
}

_TERMINAL_STATES = {"paid"}

_LEDGER_STATUS_PATTERN = r"^(pending|submitted|receipt_verified|shortlisted|awarded|paid|rejected)$"
_INBOX_FILTER_PATTERN = r"^(kenya|remote|fully_funded|direct_assistance|grants|scholarships)$"


def _validate_transition(current: str, target: str) -> None:
    allowed = _VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
        f"Cannot transition from '{current}' to '{target}'. "
        f"Allowed: {sorted(allowed) or 'none (terminal)'}"
    ),
        )


def _get_or_create_profile(db: Session) -> ApplicantProfile:
    profile = db.query(ApplicantProfile).first()
    if profile is None:
        profile = ApplicantProfile(country="Kenya")
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _parse_interests(profile: ApplicantProfile | None) -> list[str]:
    if not profile or not profile.interests:
        return []
    return [k.strip() for k in profile.interests.split(",") if k.strip()]


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@router.get("/profile", response_model=ApplicantProfileResponse)
def get_profile(db: Session = Depends(get_db)):
    return _get_or_create_profile(db)


@router.put("/profile", response_model=ApplicantProfileResponse)
def update_profile(payload: ApplicantProfileUpdate, db: Session = Depends(get_db)):
    profile = _get_or_create_profile(db)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field_name, value)
    db.commit()
    db.refresh(profile)
    return profile


# ---------------------------------------------------------------------------
# Ranked inbox
# ---------------------------------------------------------------------------
@router.get("/inbox")
def ranked_inbox(
    filter: str | None = Query(None, pattern=_INBOX_FILTER_PATTERN),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return opportunities ranked by eligibility, deadline urgency, fit,
    award value, app cost, and feasibility. Optionally filtered."""
    from app.routes.opportunities import _public_visible

    q = _public_visible(db.query(Opportunity).filter(Opportunity.is_active.is_(True)))
    if search:
        term = f"%{search}%"
        q = q.filter(Opportunity.title.ilike(term))

    opps = q.order_by(Opportunity.scraped_at.desc()).limit(limit * 3).all()

    profile = _get_or_create_profile(db)
    interests = _parse_interests(profile)

    ranked: list[dict[str, Any]] = []
    for opp in opps:
        if filter and not passes_filter(opp, filter):
            continue
        result = rank_opportunity(opp, interests)
        ranked.append(
            {
                "id": opp.id,
                "title": opp.title,
                "summary": opp.summary,
                "opportunity_type": opp.opportunity_type,
                "field": opp.field,
                "location": opp.location,
                "deadline": opp.deadline,
                "deadline_at": opp.deadline_at.isoformat() if opp.deadline_at else None,
                "url": opp.url,
                "source_name": opp.source_name,
                "scraped_at": opp.scraped_at.isoformat() if opp.scraped_at else None,
                "score": result["score"],
                "factors": result["factors"],
                "eligibility": result["eligibility"],
            }
        )

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return {"total": len(ranked), "data": ranked[:limit]}


# ---------------------------------------------------------------------------
# Submission ledger
# ---------------------------------------------------------------------------
@router.get("/ledger", response_model=list[LedgerResponse])
def list_ledger(db: Session = Depends(get_db)):
    return db.query(SubmissionLedger).order_by(SubmissionLedger.created_at.desc()).all()


@router.post("/ledger", response_model=LedgerResponse, status_code=201)
def create_ledger_entry(payload: LedgerCreate, db: Session = Depends(get_db)):
    opp = db.query(Opportunity).filter(Opportunity.id == payload.opportunity_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    existing = (
        db.query(SubmissionLedger)
        .filter(
            SubmissionLedger.opportunity_id == payload.opportunity_id,
            SubmissionLedger.status != "rejected",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Active ledger entry already exists for this opportunity (status: {existing.status})",
        )

    entry = SubmissionLedger(
        opportunity_id=payload.opportunity_id,
        status=payload.status,
        receipt_ref=payload.receipt_ref,
        notes=payload.notes,
    )
    if payload.status == "submitted":
        entry.submitted_at = datetime.now(UTC)
    if payload.status == "receipt_verified":
        if not payload.receipt_ref:
            raise HTTPException(
                status_code=422,
                detail="receipt_ref is required to set status to 'receipt_verified'",
            )
        entry.receipt_verified_at = datetime.now(UTC)

    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.patch("/ledger/{entry_id}", response_model=LedgerResponse)
def update_ledger_entry(entry_id: int, payload: LedgerUpdate, db: Session = Depends(get_db)):
    entry = db.query(SubmissionLedger).filter(SubmissionLedger.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Ledger entry not found")

    if payload.status is not None and payload.status != entry.status:
        _validate_transition(entry.status, payload.status)
        entry.status = payload.status
        if payload.status == "submitted":
            entry.submitted_at = datetime.now(UTC)
        if payload.status == "receipt_verified":
            ref = payload.receipt_ref or entry.receipt_ref
            if not ref:
                raise HTTPException(
                    status_code=422,
                    detail="receipt_ref is required to set status to 'receipt_verified'",
                )
            entry.receipt_ref = ref
            entry.receipt_verified_at = datetime.now(UTC)

    if payload.receipt_ref is not None:
        entry.receipt_ref = payload.receipt_ref
    if payload.notes is not None:
        entry.notes = payload.notes

    db.commit()
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Application checklist
# ---------------------------------------------------------------------------
@router.get("/checklist/{opportunity_id}", response_model=list[ChecklistItemResponse])
def list_checklist(opportunity_id: int, db: Session = Depends(get_db)):
    return (
        db.query(ApplicationChecklist)
        .filter(ApplicationChecklist.opportunity_id == opportunity_id)
        .order_by(ApplicationChecklist.created_at.asc())
        .all()
    )


@router.post("/checklist", response_model=ChecklistItemResponse, status_code=201)
def create_checklist_item(payload: ChecklistItemCreate, db: Session = Depends(get_db)):
    opp = db.query(Opportunity).filter(Opportunity.id == payload.opportunity_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    item = ApplicationChecklist(
        opportunity_id=payload.opportunity_id,
        item_name=payload.item_name,
        status=payload.status,
        verified=payload.verified,
        notes=payload.notes,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/checklist/{item_id}", response_model=ChecklistItemResponse)
def update_checklist_item(item_id: int, payload: ChecklistItemUpdate, db: Session = Depends(get_db)):
    item = db.query(ApplicationChecklist).filter(ApplicationChecklist.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")

    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field_name, value)

    db.commit()
    db.refresh(item)
    return item


@router.delete("/checklist/{item_id}", status_code=204)
def delete_checklist_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(ApplicationChecklist).filter(ApplicationChecklist.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    db.delete(item)
    db.commit()


# ---------------------------------------------------------------------------
# Export — CSV / JSON
# ---------------------------------------------------------------------------
@router.get("/export/inbox")
def export_inbox(
    format: str = Query("csv", pattern="^(csv|json)$"),
    filter: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Export the ranked inbox as CSV or JSON for offline use."""
    from app.routes.opportunities import _public_visible

    q = _public_visible(db.query(Opportunity).filter(Opportunity.is_active.is_(True)))
    opps = q.order_by(Opportunity.scraped_at.desc()).limit(500).all()
    profile = _get_or_create_profile(db)
    interests = _parse_interests(profile)

    rows: list[dict[str, Any]] = []
    for opp in opps:
        if filter and not passes_filter(opp, filter):
            continue
        result = rank_opportunity(opp, interests)
        rows.append(
            {
                "id": opp.id,
                "title": opp.title,
                "type": opp.opportunity_type,
                "field": opp.field,
                "location": opp.location,
                "deadline": opp.deadline,
                "deadline_at": opp.deadline_at.isoformat() if opp.deadline_at else "",
                "url": opp.url,
                "source": opp.source_name,
                "score": result["score"],
                "eligibility_overall": result["eligibility"]["overall"],
                "kenya_verdict": result["eligibility"]["kenya"]["verdict"],
                "remote_verdict": result["eligibility"]["remote"]["verdict"],
                "fully_funded_verdict": result["eligibility"]["fully_funded"]["verdict"],
                "direct_assistance_verdict": result["eligibility"]["direct_assistance"]["verdict"],
                "scraped_at": opp.scraped_at.isoformat() if opp.scraped_at else "",
            }
        )

    rows.sort(key=lambda x: x["score"], reverse=True)

    if format == "json":
        return StreamingResponse(
            io.BytesIO(json.dumps(rows, indent=2).encode()),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=opportunity_inbox.json"},
        )

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=opportunity_inbox.csv"},
    )


@router.get("/export/ledger")
def export_ledger(
    format: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
):
    """Export the submission ledger as CSV or JSON."""
    entries = db.query(SubmissionLedger).order_by(SubmissionLedger.created_at.desc()).all()
    rows: list[dict[str, Any]] = []
    for e in entries:
        opp = db.query(Opportunity).filter(Opportunity.id == e.opportunity_id).first()
        rows.append(
            {
                "id": e.id,
                "opportunity_id": e.opportunity_id,
                "title": opp.title if opp else "",
                "url": opp.url if opp else "",
                "status": e.status,
                "receipt_ref": e.receipt_ref or "",
                "submitted_at": e.submitted_at.isoformat() if e.submitted_at else "",
                "receipt_verified_at": e.receipt_verified_at.isoformat() if e.receipt_verified_at else "",
                "notes": e.notes or "",
                "created_at": e.created_at.isoformat() if e.created_at else "",
            }
        )

    if format == "json":
        return StreamingResponse(
            io.BytesIO(json.dumps(rows, indent=2).encode()),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=submission_ledger.json"},
        )

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=submission_ledger.csv"},
    )
