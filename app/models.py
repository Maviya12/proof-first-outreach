"""Database schema (SQLModel). Mirrors BUILD_SPEC section 4, plus a few
operational tables (integrations, discovery_jobs, raw_files, breach_log).

All timestamps are naive UTC. Kept Postgres-compatible: no SQLite-only types.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from pydantic import NaiveDatetime
from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LeadStatus(StrEnum):
    NEW = "NEW"
    RESEARCHED = "RESEARCHED"
    QUALIFIED = "QUALIFIED"
    MESSAGE_READY = "MESSAGE_READY"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    SENT = "SENT"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    MEETING_BOOKED = "MEETING_BOOKED"
    CLIENT = "CLIENT"
    NOT_A_FIT = "NOT_A_FIT"
    NOT_INTERESTED = "NOT_INTERESTED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"


class MessageStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"  # approved follow-up waiting for its date
    SENT = "SENT"
    MANUAL_PENDING = "MANUAL_PENDING"
    SENT_MANUAL = "SENT_MANUAL"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"  # e.g. follow-up cancelled by an inbound reply


EMAIL_CHANNEL = "email"
HANDOFF_CHANNELS = {"instagram", "linkedin", "facebook", "x", "whatsapp"}
ALL_CHANNELS = {EMAIL_CHANNEL} | HANDOFF_CHANNELS


class Niche(SQLModel, table=True):
    __tablename__ = "niches"
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True)
    name: str
    config_json: str = "{}"  # search_queries, rubric, playbook, portfolio_tags, banned_claims, is_medical


class Organization(SQLModel, table=True):
    __tablename__ = "organizations"
    __table_args__ = (Index("ix_org_domain", "domain_norm"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    domain_norm: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None  # ISO-3166 alpha-2 where known
    google_place_id: Optional[str] = Field(default=None, unique=True)
    last_contacted_at: Optional[NaiveDatetime] = None
    contact_locked_until: Optional[NaiveDatetime] = None
    locked_by_lead_id: Optional[int] = None  # the one person we're allowed to keep talking to
    lock_override: bool = False


class Lead(SQLModel, table=True):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_status_niche", "status", "niche_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    organization_id: int = Field(foreign_key="organizations.id")
    niche_id: Optional[int] = Field(default=None, foreign_key="niches.id")
    contact_name: Optional[str] = None
    contact_role: Optional[str] = None
    website: Optional[str] = None
    description: Optional[str] = None
    size_band: Optional[str] = None
    quality_score: Optional[float] = None
    fit_reason: Optional[str] = None
    status: str = Field(default=LeadStatus.NEW)
    source: Optional[str] = None
    intent_signal: Optional[str] = None
    needs_review: Optional[str] = None  # e.g. fuzzy duplicate warning
    intake_status: Optional[str] = None  # None | SUFFICIENT | INSUFFICIENT (Public Profile Intake)
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class ContactPoint(SQLModel, table=True):
    __tablename__ = "contact_points"
    __table_args__ = (UniqueConstraint("type", "value_norm"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id", index=True)
    type: str  # email, phone, instagram, linkedin, facebook, x, youtube, website, whatsapp
    value_raw: str
    value_norm: str
    source_url: Optional[str] = None
    is_public_business_contact: bool = True


class Evidence(SQLModel, table=True):
    __tablename__ = "evidence"
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id", index=True)
    kind: str  # website_text, caption, bio, screenshot_observation, ad_library, job_post, note, opportunity, youtube_meta
    source_type: str  # web, pasted_text, screenshot, user_note, api
    source_url: Optional[str] = None
    source_ref: Optional[str] = None
    snippet: str
    observed_at: NaiveDatetime = Field(default_factory=utcnow)
    confidence: float = 1.0


class PortfolioAsset(SQLModel, table=True):
    __tablename__ = "portfolio_assets"
    id: Optional[int] = Field(default=None, primary_key=True)
    niche_id: Optional[int] = Field(default=None, foreign_key="niches.id")
    title: str
    url: str
    type: str = "reel"  # main, reel, cinematic, before_after, testimonial
    tags: str = ""  # comma separated
    active: bool = True


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_status_channel", "status", "channel"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id", index=True)
    channel: str
    kind: str = "initial"  # initial, followup, reply
    followup_number: int = 0
    draft_a: Optional[str] = None
    draft_b: Optional[str] = None
    body: Optional[str] = None
    subject: Optional[str] = None
    to_address: Optional[str] = None
    portfolio_asset_id: Optional[int] = Field(default=None, foreign_key="portfolio_assets.id")
    status: str = Field(default=MessageStatus.DRAFT)
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    approved_at: Optional[NaiveDatetime] = None
    scheduled_for: Optional[NaiveDatetime] = None
    sent_at: Optional[NaiveDatetime] = None
    provider_message_id: Optional[str] = None
    thread_id: Optional[str] = None
    error: Optional[str] = None
    validation_json: Optional[str] = None


class MessageEvidence(SQLModel, table=True):
    __tablename__ = "message_evidence"
    message_id: int = Field(foreign_key="messages.id", primary_key=True)
    evidence_id: int = Field(foreign_key="evidence.id", primary_key=True)


class Interaction(SQLModel, table=True):
    __tablename__ = "interactions"
    __table_args__ = (Index("ix_interactions_lead_time", "lead_id", "occurred_at"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id")
    channel: str
    direction: str  # out, in
    body: Optional[str] = None
    classification: Optional[str] = None
    occurred_at: NaiveDatetime = Field(default_factory=utcnow)
    raw_ref: Optional[str] = Field(default=None, index=True)  # provider message id / PSID
    handled: bool = False


class Suppression(SQLModel, table=True):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("type", "value_hash"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str  # email, domain, handle, phone, org
    value_hash: str  # SHA-256 of normalised value; never deleted on erasure
    reason: Optional[str] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class SettingRow(SQLModel, table=True):
    __tablename__ = "settings"
    key: str = Field(primary_key=True)
    value: str  # JSON-encoded


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    actor: str
    action: str
    entity: Optional[str] = None
    entity_id: Optional[int] = None
    detail_json: Optional[str] = None
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)


class LLMCall(SQLModel, table=True):
    __tablename__ = "llm_calls"
    id: Optional[int] = Field(default=None, primary_key=True)
    task: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_inr: float = 0.0
    ts: NaiveDatetime = Field(default_factory=utcnow, index=True)


# ---- operational tables ----

class Integration(SQLModel, table=True):
    __tablename__ = "integrations"
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(unique=True)  # google
    token_encrypted: str  # Fernet ciphertext of the OAuth credentials JSON
    account_email: Optional[str] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    updated_at: NaiveDatetime = Field(default_factory=utcnow)


class DiscoveryJob(SQLModel, table=True):
    __tablename__ = "discovery_jobs"
    id: Optional[int] = Field(default=None, primary_key=True)
    niche_id: Optional[int] = Field(default=None, foreign_key="niches.id")
    city: str
    radius_m: int = 5000
    max_results: int = 20
    status: str = "PENDING"  # PENDING, RUNNING, DONE, FAILED
    found: int = 0
    created: int = 0
    skipped: int = 0
    error: Optional[str] = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class RawFile(SQLModel, table=True):
    """Raw HTML / screenshots on disk; deleted after RAW_RETENTION_DAYS."""
    __tablename__ = "raw_files"
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: Optional[int] = Field(default=None, foreign_key="leads.id")
    kind: str  # html, screenshot
    path: str
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    deleted_at: Optional[NaiveDatetime] = None


class BreachLog(SQLModel, table=True):
    __tablename__ = "breach_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    description: str
    affected: Optional[str] = None
    action_taken: Optional[str] = None
    detected_at: NaiveDatetime = Field(default_factory=utcnow)
