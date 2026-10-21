from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class OpportunityBase(BaseModel):
    title: str
    description: str | None = None
    summary: str | None = None
    opportunity_type: str
    field: str | None = None
    location: str | None = None
    deadline: str | None = None
    deadline_at: date | None = None
    url: str
    source_name: str | None = None
    tags: str | None = None


class OpportunityCreate(OpportunityBase):
    pass


class OpportunityResponse(OpportunityBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    review_status: str
    scraped_at: datetime


class PaginatedOpportunities(BaseModel):
    total: int
    page: int
    per_page: int
    total_pages: int
    data: list[OpportunityResponse]


class SuggestionsResponse(BaseModel):
    suggestions: list[str]


class ScrapeRequest(BaseModel):
    opportunity_types: list[str] | None = Field(
        default_factory=lambda: ["scholarship", "fellowship", "grant", "job"]
    )
    extra_keywords: list[str] | None = None
    max_results: int = Field(default=50, ge=1, le=200)


class ScrapeResponse(BaseModel):
    status: str
    message: str
    scraped_count: int = 0
    saved_count: int = 0


class StatsResponse(BaseModel):
    total: int
    scholarships: int
    fellowships: int
    grants: int
    jobs: int
    last_scraped: datetime | None = None


class SaveOpportunityRequest(BaseModel):
    email: EmailStr
    opportunity_id: int


class SavedOpportunityActionResponse(BaseModel):
    status: str
    message: str


class AlertCreateRequest(BaseModel):
    email: EmailStr
    opportunity_type: str | None = None
    field: str | None = None
    location: str | None = None
    keyword: str | None = Field(default=None, max_length=200)


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opportunity_type: str | None = None
    field: str | None = None
    location: str | None = None
    keyword: str | None = None
    created_at: datetime
    last_notified_at: datetime | None = None


class AnalyticsEventRequest(BaseModel):
    event_type: str
    client_id: str = Field(min_length=1, max_length=64)
    value: str | None = Field(default=None, max_length=200)
    opportunity_id: int | None = None


class ModerationActionResponse(BaseModel):
    status: str
    id: int
    review_status: str


class BulkModerationRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


class BulkModerationResponse(BaseModel):
    status: str
    updated: int
    ids: list[int]


class AdminOpportunityCreate(BaseModel):
    """A manually-added listing — trusted by definition (an admin typed
    it in directly), so it skips the moderation queue and goes live
    immediately as review_status="approved", unlike the low-trust
    open web-search discovery path (see routes/moderation.py).
    """

    title: str = Field(min_length=1, max_length=500)
    opportunity_type: str
    url: str
    description: str | None = None
    summary: str | None = None
    field: str | None = None
    location: str | None = None
    deadline: str | None = None
    deadline_at: date | None = None
    source_name: str | None = None
    tags: str | None = None


class AdminOpportunityUpdate(BaseModel):
    """Partial update for the admin listing-management table — every
    field optional, only the ones the admin actually changed are sent.
    """

    title: str | None = None
    description: str | None = None
    summary: str | None = None
    opportunity_type: str | None = None
    field: str | None = None
    location: str | None = None
    deadline: str | None = None
    deadline_at: date | None = None
    url: str | None = None
    source_name: str | None = None
    tags: str | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Applicant autopilot schemas
# ---------------------------------------------------------------------------
class ApplicantProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    interests: str | None = None
    country: str = "Kenya"


class ApplicantProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str | None = None
    interests: str | None = None
    country: str
    created_at: datetime
    updated_at: datetime | None = None


class ChecklistItemCreate(BaseModel):
    opportunity_id: int
    item_name: str = Field(min_length=1, max_length=300)
    status: str = Field(default="missing", pattern="^(ready|missing|not_applicable|paused)$")
    verified: bool = False
    notes: str | None = None


class ChecklistItemUpdate(BaseModel):
    item_name: str | None = Field(default=None, min_length=1, max_length=300)
    status: str | None = Field(default=None, pattern="^(ready|missing|not_applicable|paused)$")
    verified: bool | None = None
    notes: str | None = None


class ChecklistItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opportunity_id: int
    item_name: str
    status: str
    verified: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class LedgerCreate(BaseModel):
    opportunity_id: int
    status: str = Field(default="pending", pattern="^(pending|submitted|receipt_verified|shortlisted|awarded|paid|rejected)$")
    receipt_ref: str | None = Field(default=None, max_length=2000)
    notes: str | None = None


class LedgerUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(pending|submitted|receipt_verified|shortlisted|awarded|paid|rejected)$")
    receipt_ref: str | None = Field(default=None, max_length=2000)
    notes: str | None = None


class LedgerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opportunity_id: int
    status: str
    receipt_ref: str | None = None
    notes: str | None = None
    submitted_at: datetime | None = None
    receipt_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class RankedOpportunityResponse(BaseModel):
    id: int
    title: str
    summary: str | None = None
    opportunity_type: str
    field: str | None = None
    location: str | None = None
    deadline: str | None = None
    deadline_at: date | None = None
    url: str
    source_name: str | None = None
    scraped_at: datetime
    score: float
    factors: dict
    eligibility: dict


# ---------------------------------------------------------------------------
# Applicant autopilot schemas
# ---------------------------------------------------------------------------
class ApplicantProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    interests: str | None = None
    country: str = "Kenya"


class ApplicantProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str | None = None
    interests: str | None = None
    country: str
    created_at: datetime
    updated_at: datetime | None = None


class ChecklistItemCreate(BaseModel):
    opportunity_id: int
    item_name: str = Field(min_length=1, max_length=300)
    status: str = Field(default="missing", pattern="^(ready|missing|not_applicable|paused)$")
    verified: bool = False
    notes: str | None = None


class ChecklistItemUpdate(BaseModel):
    item_name: str | None = Field(default=None, min_length=1, max_length=300)
    status: str | None = Field(default=None, pattern="^(ready|missing|not_applicable|paused)$")
    verified: bool | None = None
    notes: str | None = None


class ChecklistItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opportunity_id: int
    item_name: str
    status: str
    verified: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class LedgerCreate(BaseModel):
    opportunity_id: int
    status: str = Field(default="pending", pattern="^(pending|submitted|receipt_verified|shortlisted|awarded|paid|rejected)$")
    receipt_ref: str | None = Field(default=None, max_length=2000)
    notes: str | None = None


class LedgerUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(pending|submitted|receipt_verified|shortlisted|awarded|paid|rejected)$")
    receipt_ref: str | None = Field(default=None, max_length=2000)
    notes: str | None = None


class LedgerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    opportunity_id: int
    status: str
    receipt_ref: str | None = None
    notes: str | None = None
    submitted_at: datetime | None = None
    receipt_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class RankedOpportunityResponse(BaseModel):
    id: int
    title: str
    summary: str | None = None
    opportunity_type: str
    field: str | None = None
    location: str | None = None
    deadline: str | None = None
    deadline_at: date | None = None
    url: str
    source_name: str | None = None
    scraped_at: datetime
    score: float
    factors: dict
    eligibility: dict
