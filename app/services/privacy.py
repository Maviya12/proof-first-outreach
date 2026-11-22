"""Erasure on request and retention purges (DPDP-ready).

Erasure keeps only suppression hashes, so an erased person is never re-contacted.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Session, func, select

from app.audit import audit
from app.config import Settings, get_settings
from app.models import (ContactPoint, Evidence, Interaction, Lead, Message, MessageEvidence, Organization,
                        RawFile, utcnow)
from app.services.suppression import suppress_lead


def _delete_lead_rows(session: Session, lead: Lead) -> dict:
    lead_id = lead.id
    msg_ids = list(session.exec(select(Message.id).where(Message.lead_id == lead_id)))
    if msg_ids:
        session.exec(delete(MessageEvidence).where(MessageEvidence.message_id.in_(msg_ids)))
    counts = {}
    for model in (Message, Evidence, Interaction, ContactPoint):
        res = session.exec(delete(model).where(model.lead_id == lead_id))
        counts[model.__tablename__] = res.rowcount
    for rf in session.exec(select(RawFile).where(RawFile.lead_id == lead_id)):
        Path(rf.path).unlink(missing_ok=True)
        session.delete(rf)
    org_id = lead.organization_id
    session.delete(lead)
    session.flush()
    remaining = session.exec(select(func.count(Lead.id)).where(Lead.organization_id == org_id)).one()
    if remaining == 0:
        org = session.get(Organization, org_id)
        if org:
            session.delete(org)
    return counts


def erase_lead(session: Session, lead: Lead, actor: str = "system") -> dict:
    lead_id = lead.id
    suppress_lead(session, lead, "erasure request", actor=actor)
    session.flush()
    counts = _delete_lead_rows(session, lead)
    audit(session, "privacy.erase", actor=actor, entity="lead", entity_id=lead_id, deleted=counts)
    return {"erased_lead_id": lead_id, "deleted": counts, "suppression_kept": True}


def purge_raw_files(session: Session, now: datetime | None = None, cfg: Settings | None = None) -> int:
    cfg = cfg or get_settings()
    cutoff = (now or utcnow()) - timedelta(days=cfg.raw_retention_days)
    n = 0
    for rf in session.exec(select(RawFile).where(RawFile.created_at < cutoff, RawFile.deleted_at.is_(None))):
        Path(rf.path).unlink(missing_ok=True)
        rf.deleted_at = utcnow()
        session.add(rf)
        n += 1
    if n:
        audit(session, "retention.raw_purge", count=n)
    return n


def purge_stale_leads(session: Session, now: datetime | None = None, months: int = 12) -> int:
    """Delete evidence/history for leads with no activity for `months`."""
    cutoff = (now or utcnow()) - timedelta(days=30 * months)
    n = 0
    for lead in session.exec(select(Lead).where(Lead.updated_at < cutoff)).all():
        last_int = session.exec(select(func.max(Interaction.occurred_at)).where(Interaction.lead_id == lead.id)).one()
        if last_int and last_int >= cutoff:
            continue
        _delete_lead_rows(session, lead)
        n += 1
    if n:
        audit(session, "retention.lead_purge", count=n)
    return n
