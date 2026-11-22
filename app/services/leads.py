"""Lead/organisation/contact/evidence helpers shared by all pipelines."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.audit import audit
from app.config import get_settings
from app.models import ContactPoint, Evidence, Lead, LeadStatus, Organization, utcnow
from app.services.dedup import check_duplicate
from app.services.normalize import normalize_contact, normalize_domain

TERMINAL = {LeadStatus.DO_NOT_CONTACT, LeadStatus.BLOCKED}


def set_status(session: Session, lead: Lead, new: str, *, actor: str = "system", reason: str | None = None) -> None:
    if lead.status == new:
        return
    if lead.status in TERMINAL and new not in TERMINAL:
        raise ValueError(f"lead {lead.id} is {lead.status}; cannot move to {new}")
    old = lead.status
    lead.status = new
    lead.updated_at = utcnow()
    session.add(lead)
    audit(session, "lead.status", actor=actor, entity="lead", entity_id=lead.id, old=old, new=new, reason=reason)


def add_contact_point(session: Session, lead: Lead, type_: str, value: str, source_url: str | None,
                      *, public_business: bool = True) -> ContactPoint | None:
    """Add a contact point if it normalises and isn't already known (UNIQUE(type, value_norm))."""
    t = "x" if type_ == "twitter" else type_.lower()
    try:
        norm = normalize_contact(t, value, get_settings().default_phone_region)
    except ValueError:
        return None
    existing = session.exec(select(ContactPoint).where(ContactPoint.type == t, ContactPoint.value_norm == norm)).first()
    if existing:
        return existing if existing.lead_id == lead.id else None
    cp = ContactPoint(lead_id=lead.id, type=t, value_raw=value.strip(), value_norm=norm, source_url=source_url,
                      is_public_business_contact=public_business)
    session.add(cp)
    session.flush()
    return cp


def add_evidence(session: Session, lead: Lead, *, kind: str, source_type: str, snippet: str,
                 source_url: str | None = None, source_ref: str | None = None, confidence: float = 1.0) -> Evidence:
    ev = Evidence(lead_id=lead.id, kind=kind, source_type=source_type, snippet=snippet.strip()[:2000],
                  source_url=source_url, source_ref=source_ref, confidence=max(0.0, min(1.0, confidence)))
    session.add(ev)
    session.flush()
    return ev


def contacts_of(session: Session, lead_id: int, type_: str | None = None) -> list[ContactPoint]:
    q = select(ContactPoint).where(ContactPoint.lead_id == lead_id)
    if type_:
        q = q.where(ContactPoint.type == type_)
    return list(session.exec(q))


def evidence_of(session: Session, lead_id: int) -> list[Evidence]:
    return list(session.exec(select(Evidence).where(Evidence.lead_id == lead_id).order_by(Evidence.id)))


@dataclass
class CreateResult:
    lead: Lead | None = None
    duplicate_of: int | None = None
    matched_on: str | None = None
    flagged: list[str] = field(default_factory=list)


def create_lead(session: Session, *, name: str, website: str | None = None, city: str | None = None,
                country: str | None = None, niche_id: int | None = None, place_id: str | None = None,
                contacts: list[tuple[str, str, str | None]] | None = None, source: str = "manual",
                intent_signal: str | None = None, contact_name: str | None = None,
                contact_role: str | None = None, description: str | None = None,
                actor: str = "system") -> CreateResult:
    """Create organisation + lead with layered dedup. contacts: (type, value, source_url)."""
    contacts = contacts or []
    dup = check_duplicate(session, name=name, city=city, place_id=place_id, domain=website,
                          contacts=[(t, v) for t, v, _ in contacts], region=get_settings().default_phone_region)
    if dup.is_exact_duplicate:
        lead_id = dup.existing_lead_by_contact.id if dup.existing_lead_by_contact else None
        if lead_id is None and dup.existing_org:
            first = session.exec(select(Lead).where(Lead.organization_id == dup.existing_org.id)).first()
            lead_id = first.id if first else None
        return CreateResult(duplicate_of=lead_id, matched_on=dup.matched_on)

    org = Organization(name=name.strip(), domain_norm=normalize_domain(website) if website else None,
                       city=city, country=country, google_place_id=place_id)
    session.add(org)
    session.flush()
    lead = Lead(organization_id=org.id, niche_id=niche_id, website=website, source=source,
                intent_signal=intent_signal, contact_name=contact_name, contact_role=contact_role,
                description=description)
    flagged = [f"possible duplicate of '{o.name}' (org {o.id}, {score:.0f}%)" for o, score in dup.fuzzy_candidates]
    if flagged:
        lead.needs_review = "; ".join(flagged)
    session.add(lead)
    session.flush()
    for t, v, src in contacts:
        add_contact_point(session, lead, t, v, src)
    if intent_signal:
        add_evidence(session, lead, kind="note", source_type="user_note", snippet=f"Intent signal: {intent_signal}",
                     source_ref=source)
    audit(session, "lead.create", actor=actor, entity="lead", entity_id=lead.id, source=source)
    return CreateResult(lead=lead, flagged=flagged)
