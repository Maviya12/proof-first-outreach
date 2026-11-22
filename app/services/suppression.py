"""Suppression list: hashed, plain-code, never deleted on erasure (rule 5 & 9)."""
from __future__ import annotations

import hashlib

from sqlmodel import Session, select

from app.audit import audit
from app.models import (ContactPoint, Lead, LeadStatus, Message, MessageStatus, Organization,
                        Suppression, utcnow)
from app.services.normalize import email_domain, normalize_contact, normalize_domain

SOCIAL_TYPES = {"instagram", "facebook", "x", "linkedin", "youtube"}


def hash_value(type_: str, value_norm: str) -> str:
    return hashlib.sha256(f"{type_}:{value_norm}".encode()).hexdigest()


def _suppression_key(type_: str, value: str, normalized: bool = False) -> tuple[str, str]:
    """Map a contact type/value to the (suppression_type, normalised_value) pair."""
    t = type_.lower()
    if t == "domain":
        return "domain", value if normalized else normalize_domain(value)
    if t == "org":
        return "org", str(value)
    if t in SOCIAL_TYPES or t in {"ig_psid", "fb_psid"}:
        return "handle", f"{t}:{value if normalized else normalize_contact(t, value)}"
    if t in {"phone", "whatsapp"}:
        return "phone", value if normalized else normalize_contact("phone", value)
    if t == "email":
        return "email", value if normalized else normalize_contact("email", value)
    raise ValueError(f"cannot suppress type {type_!r}")


def add_suppression(session: Session, type_: str, value: str, reason: str, *,
                    normalized: bool = False, actor: str = "system") -> bool:
    """Add one suppression. Returns True if newly added."""
    s_type, norm = _suppression_key(type_, value, normalized)
    if not norm:
        return False
    h = hash_value(s_type, norm)
    if session.exec(select(Suppression).where(Suppression.type == s_type,
                                              Suppression.value_hash == h)).first():
        return False
    session.add(Suppression(type=s_type, value_hash=h, reason=reason))
    audit(session, "suppression.add", actor=actor, entity="suppression", type=s_type, reason=reason)
    return True


def is_suppressed(session: Session, type_: str, value: str, *, normalized: bool = False) -> bool:
    try:
        s_type, norm = _suppression_key(type_, value, normalized)
    except ValueError:
        return False
    if not norm:
        return False
    h = hash_value(s_type, norm)
    return session.exec(select(Suppression.id).where(Suppression.type == s_type,
                                                     Suppression.value_hash == h)).first() is not None


def lead_suppression_reasons(session: Session, lead: Lead) -> list[str]:
    """Every reason this lead must not be contacted. Empty list = clear."""
    reasons: list[str] = []
    org = session.get(Organization, lead.organization_id)
    if org:
        if is_suppressed(session, "org", str(org.id), normalized=True):
            reasons.append("organisation suppressed")
        if org.domain_norm and is_suppressed(session, "domain", org.domain_norm, normalized=True):
            reasons.append(f"domain {org.domain_norm} suppressed")
    for cp in session.exec(select(ContactPoint).where(ContactPoint.lead_id == lead.id)):
        if cp.type == "website":
            continue
        if is_suppressed(session, cp.type, cp.value_norm, normalized=True):
            reasons.append(f"{cp.type} suppressed")
        if cp.type == "email" and is_suppressed(session, "domain", email_domain(cp.value_norm), normalized=True):
            reasons.append("email domain suppressed")
    if lead.status in (LeadStatus.DO_NOT_CONTACT, LeadStatus.BLOCKED):
        reasons.append(f"lead status {lead.status}")
    return sorted(set(reasons))


def address_suppressed(session: Session, channel: str, address: str) -> bool:
    """Check a single outbound address (email or handle) plus its email domain."""
    if channel == "email":
        try:
            return (is_suppressed(session, "email", address)
                    or is_suppressed(session, "domain", email_domain(address), normalized=True))
        except ValueError:
            return True  # fail closed on unparseable address
    return is_suppressed(session, channel, address)


PERSONAL_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
                          "yahoo.co.in", "rediffmail.com", "proton.me", "protonmail.com", "live.com"}


def suppress_lead(session: Session, lead: Lead, reason: str, *, status: str = LeadStatus.DO_NOT_CONTACT,
                  actor: str = "system") -> None:
    """Suppress the lead's contact points AND its organisation, cancel pending messages."""
    org = session.get(Organization, lead.organization_id)
    if org:
        add_suppression(session, "org", str(org.id), reason, normalized=True, actor=actor)
        if org.domain_norm and org.domain_norm not in PERSONAL_EMAIL_DOMAINS:
            add_suppression(session, "domain", org.domain_norm, reason, normalized=True, actor=actor)
    for cp in session.exec(select(ContactPoint).where(ContactPoint.lead_id == lead.id)):
        if cp.type == "website":
            continue
        add_suppression(session, cp.type, cp.value_norm, reason, normalized=True, actor=actor)
    cancel_pending_messages(session, lead.id, reason)
    old = lead.status
    lead.status = status
    lead.updated_at = utcnow()
    session.add(lead)
    audit(session, "lead.status", actor=actor, entity="lead", entity_id=lead.id, old=old, new=status,
          reason=reason)


def cancel_pending_messages(session: Session, lead_id: int, reason: str) -> int:
    pending = {MessageStatus.DRAFT, MessageStatus.APPROVAL_PENDING, MessageStatus.APPROVED,
               MessageStatus.SCHEDULED, MessageStatus.MANUAL_PENDING}
    n = 0
    for m in session.exec(select(Message).where(Message.lead_id == lead_id)):
        if m.status in pending:
            m.status = MessageStatus.CANCELLED
            m.error = reason
            session.add(m)
            n += 1
    return n
