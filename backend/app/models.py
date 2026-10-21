import secrets

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


def _generate_manage_token() -> str:
    """URL-safe token identifying a subscriber without a password."""
    return secrets.token_urlsafe(32)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    # Lowercased/punctuation-stripped title, used to catch the same
    # opportunity reposted verbatim under a different URL by another
    # aggregator. See app/scrapers/dedup.py.
    title_normalized = Column(String(600), index=True, nullable=True)
    description = Column(Text)
    # Original one-line synopsis we generate — the primary text shown on
    # cards, so people aren't just reading copy-pasted third-party text.
    # See app/scrapers/synopsis.py.
    summary = Column(String(300))
    # scholarship | fellowship | grant | job | other
    opportunity_type = Column(String(50), nullable=False, index=True)
    # STEM, Medicine, Arts, etc.
    field = Column(String(200), index=True)
    location = Column(String(200))
    deadline = Column(String(100))
    # Parsed from `deadline` when possible (see deadline_utils.parse_deadline_date)
    # so the frontend can show a day countdown instead of making people
    # read and calculate it themselves. Null when the deadline is "Rolling"
    # or couldn't be confidently parsed.
    deadline_at = Column(Date, nullable=True, index=True)
    url = Column(String(2000), unique=True, nullable=False)
    source_name = Column(String(200))
    tags = Column(String(500))           # comma-separated
    is_active = Column(Boolean, default=True)
    # Moderation gate, independent of is_active (which tracks expiry).
    # "approved" | "pending" | "rejected". Curated RSS feeds are a
    # pre-vetted trust tier and auto-approve (see rss_ingest.py); open
    # web-search discovery (Google/You.com/scrape, opportunity_scraper.py)
    # is a much lower-trust tier and starts "pending" until a human
    # reviews it in the admin moderation queue. A pending or rejected row
    # must never appear in any public response — see _public_visible() in
    # routes/opportunities.py.
    review_status = Column(String(20), nullable=False, default="approved", index=True)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<Opportunity {self.title[:60]}>"


class Subscriber(Base):
    """A user identified only by email — no password. Everything the
    subscriber can manage (saved opportunities, alert subscriptions) is
    reached through their unique, unguessable manage_token (emailed to
    them), never through a login form.
    """

    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    manage_token = Column(String(64), unique=True, nullable=False, default=_generate_manage_token, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    saved_opportunities = relationship(
        "SavedOpportunity", back_populates="subscriber", cascade="all, delete-orphan"
    )
    alert_subscriptions = relationship(
        "AlertSubscription", back_populates="subscriber", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Subscriber {self.email}>"


class SavedOpportunity(Base):
    __tablename__ = "saved_opportunities"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "opportunity_id", name="uq_saved_subscriber_opportunity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    subscriber_id = Column(
        Integer, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opportunity_id = Column(
        Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    saved_at = Column(DateTime(timezone=True), server_default=func.now())
    # Set once a deadline-approaching reminder email has been sent for
    # this saved item, so we never email the same reminder twice.
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)

    subscriber = relationship("Subscriber", back_populates="saved_opportunities")
    opportunity = relationship("Opportunity")


class AlertSubscription(Base):
    """A saved search: notify this subscriber by email when new
    opportunities matching these (optional) filters are ingested.
    """

    __tablename__ = "alert_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    subscriber_id = Column(
        Integer, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opportunity_type = Column(String(50), nullable=True)
    field = Column(String(200), nullable=True)
    location = Column(String(200), nullable=True)
    keyword = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_notified_at = Column(DateTime(timezone=True), nullable=True)

    subscriber = relationship("Subscriber", back_populates="alert_subscriptions")


class RateLimitCooldown(Base):
    """Persisted state for CooldownLimiter (see app/services/rate_limit.py).

    Used to survive process restarts (a deploy, a crash, Render's
    free-tier idle-then-wake cycle) — this used to be an in-memory dict
    that silently reset to empty on every restart, which meant a
    determined caller could bypass the cooldown just by waiting for (or
    triggering) one. `namespace` keeps independent limiter instances
    from colliding on the same key: the save-cooldown and alert-cooldown
    limiters in services/subscribers.py are both keyed by the same
    subscriber email, so without a namespace a save would reset the
    alert cooldown and vice versa.
    """

    __tablename__ = "rate_limit_cooldowns"
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_rate_limit_cooldown_namespace_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    namespace = Column(String(50), nullable=False, index=True)
    key = Column(String(320), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    # Optimistic-concurrency token, incremented on every update. Lets
    # CooldownLimiter detect a concurrent writer via a conditional
    # UPDATE ... WHERE version = :expected instead of needing
    # SELECT ... FOR UPDATE (not supported by SQLite). See
    # CooldownLimiter.check for the compare-and-swap loop this guards.
    version = Column(Integer, nullable=False, default=0)


class RateLimitLockout(Base):
    """Persisted state for LoginAttemptLimiter (see
    app/services/rate_limit.py) — same restart-durability rationale as
    RateLimitCooldown above, and the same `version` compare-and-swap
    token to keep concurrent failed-login attempts from undercounting
    each other (a lost-update race here would let an attacker dodge the
    lockout by racing requests).
    """

    __tablename__ = "rate_limit_lockouts"
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_rate_limit_lockout_namespace_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    namespace = Column(String(50), nullable=False, index=True)
    key = Column(String(320), nullable=False)
    failure_count = Column(Integer, nullable=False, default=0)
    first_failure_at = Column(DateTime(timezone=True), nullable=True)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=0)


class AnalyticsEvent(Base):
    """Minimal, self-hosted visitor analytics — no third-party tracker,
    no cookies, no PII. `client_id` is a random UUID the frontend
    generates and stores in localStorage purely to distinguish repeat
    visitors from new ones in aggregate counts; it identifies a browser,
    never a person.
    """

    __tablename__ = "analytics_events"

    id = Column(Integer, primary_key=True, index=True)
    # pageview | search | filter_type | filter_field | filter_location |
    # apply_click | save_click | alert_create
    event_type = Column(String(50), nullable=False, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    value = Column(String(200), nullable=True)  # search term, filter value, etc.
    opportunity_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class ApplicantProfile(Base):
    """Single-tenant applicant profile — stores the Kenya-based applicant's
    interests and context so the ranking engine can score fit. There is at
    most one row in practice (personal app), but the model supports multiple
    for future flexibility. No PII beyond an optional display name.
    """

    __tablename__ = "applicant_profiles"

    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String(200), nullable=True)
    # Comma-separated interest keywords used by the ranking fit score
    interests = Column(Text, nullable=True)
    country = Column(String(100), nullable=False, default="Kenya")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class SubmissionLedger(Base):
    """Application submission ledger — the state machine tracking an
    application from discovery through payment.

    State machine: pending -> submitted -> receipt_verified -> shortlisted
    -> awarded -> paid. Any state can transition to 'rejected'.
    'receipt_verified' is the guarantee that a verifiable receipt exists
    (URL or file reference stored in receipt_ref). 'form_filled' is NOT
    a valid state — filling a form is not submitting (see submission rules).
    """

    __tablename__ = "submission_ledger"

    id = Column(Integer, primary_key=True, index=True)
    opportunity_id = Column(
        Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # pending | submitted | receipt_verified | shortlisted | awarded | paid | rejected
    status = Column(String(30), nullable=False, default="pending", index=True)
    # Verifiable receipt — URL to confirmation page, confirmation email ref,
       # or file reference. Required to transition to 'receipt_verified'.
    receipt_ref = Column(String(2000), nullable=True)
    # Free-text notes (e.g. "paused for identity check", "awaiting signature")
    notes = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    receipt_verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    opportunity = relationship("Opportunity")


class ApplicationChecklist(Base):
    """Private application checklist — document readiness and per-item
    status for a specific opportunity application. Private to the
    applicant; never exposed in public Pages/reports.
    """

    __tablename__ = "application_checklist"

    id = Column(Integer, primary_key=True, index=True)
    opportunity_id = Column(
        Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_name = Column(String(300), nullable=False)
    # ready | missing | not_applicable | paused
    status = Column(String(30), nullable=False, default="missing", index=True)
    # True when the item has been verified against a real document/reference
    verified = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    opportunity = relationship("Opportunity")


class ApplicantProfile(Base):
    """Single-tenant applicant profile — stores the Kenya-based applicant's
    interests and context so the ranking engine can score fit. There is at
    most one row in practice (personal app), but the model supports multiple
    for future flexibility. No PII beyond an optional display name.
    """

    __tablename__ = "applicant_profiles"

    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String(200), nullable=True)
    # Comma-separated interest keywords used by the ranking fit score
    interests = Column(Text, nullable=True)
    country = Column(String(100), nullable=False, default="Kenya")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class SubmissionLedger(Base):
    """Application submission ledger — the state machine tracking an
    application from discovery through payment.

    State machine: pending -> submitted -> receipt_verified -> shortlisted
    -> awarded -> paid. Any state can transition to 'rejected'.
    'receipt_verified' is the guarantee that a verifiable receipt exists
    (URL or file reference stored in receipt_ref). 'form_filled' is NOT
    a valid state — filling a form is not submitting (see submission rules).
    """

    __tablename__ = "submission_ledger"

    id = Column(Integer, primary_key=True, index=True)
    opportunity_id = Column(
        Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # pending | submitted | receipt_verified | shortlisted | awarded | paid | rejected
    status = Column(String(30), nullable=False, default="pending", index=True)
    # Verifiable receipt — URL to confirmation page, confirmation email ref,
       # or file reference. Required to transition to 'receipt_verified'.
    receipt_ref = Column(String(2000), nullable=True)
    # Free-text notes (e.g. "paused for identity check", "awaiting signature")
    notes = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    receipt_verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    opportunity = relationship("Opportunity")


class ApplicationChecklist(Base):
    """Private application checklist — document readiness and per-item
    status for a specific opportunity application. Private to the
    applicant; never exposed in public Pages/reports.
    """

    __tablename__ = "application_checklist"

    id = Column(Integer, primary_key=True, index=True)
    opportunity_id = Column(
        Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_name = Column(String(300), nullable=False)
    # ready | missing | not_applicable | paused
    status = Column(String(30), nullable=False, default="missing", index=True)
    # True when the item has been verified against a real document/reference
    verified = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    opportunity = relationship("Opportunity")
