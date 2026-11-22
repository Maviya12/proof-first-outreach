"""Layered duplicate prevention (spec 4, 'Duplicate prevention')."""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.models import ContactPoint, Lead, Organization
from app.services.normalize import normalize_contact, normalize_domain

FUZZY_THRESHOLD = 88
_STOPWORDS = {"the", "clinic", "studio", "pvt", "ltd", "llp", "inc", "llc", "private", "limited", "&", "and"}


def _clean_name(name: str) -> str:
    return " ".join(w for w in name.lower().replace(",", " ").replace(".", " ").split() if w not in _STOPWORDS)


@dataclass
class DedupResult:
    existing_org: Organization | None = None
    existing_lead_by_contact: Lead | None = None
    matched_on: str | None = None
    fuzzy_candidates: list[tuple[Organization, float]] = field(default_factory=list)

    @property
    def is_exact_duplicate(self) -> bool:
        return self.existing_org is not None or self.existing_lead_by_contact is not None


def find_org_exact(session: Session, *, place_id: str | None = None, domain: str | None = None) -> tuple[Organization | None, str | None]:
    if place_id:
        org = session.exec(select(Organization).where(Organization.google_place_id == place_id)).first()
        if org:
            return org, "google_place_id"
    if domain:
        d = normalize_domain(domain)
        if d:
            org = session.exec(select(Organization).where(Organization.domain_norm == d)).first()
            if org:
                return org, "domain"
    return None, None


def find_contact(session: Session, type_: str, value: str, region: str = "IN") -> ContactPoint | None:
    try:
        norm = normalize_contact(type_, value, region)
    except ValueError:
        return None
    t = "x" if type_ == "twitter" else type_
    return session.exec(select(ContactPoint).where(ContactPoint.type == t, ContactPoint.value_norm == norm)).first()


def fuzzy_org_matches(session: Session, name: str, city: str | None, threshold: int = FUZZY_THRESHOLD,
                      exclude_id: int | None = None) -> list[tuple[Organization, float]]:
    """Same-city fuzzy name matches. Flag for review, never auto-merge."""
    if not name:
        return []
    q = select(Organization)
    if city:
        q = q.where(Organization.city == city)
    target = _clean_name(name)
    out = []
    for org in session.exec(q):
        if org.id == exclude_id:
            continue
        score = fuzz.token_set_ratio(target, _clean_name(org.name))
        if score >= threshold:
            out.append((org, float(score)))
    return sorted(out, key=lambda x: -x[1])


def check_duplicate(session: Session, *, name: str, city: str | None = None, place_id: str | None = None,
                    domain: str | None = None, contacts: list[tuple[str, str]] | None = None,
                    region: str = "IN") -> DedupResult:
    res = DedupResult()
    res.existing_org, res.matched_on = find_org_exact(session, place_id=place_id, domain=domain)
    for type_, value in contacts or []:
        cp = find_contact(session, type_, value, region)
        if cp:
            res.existing_lead_by_contact = session.get(Lead, cp.lead_id)
            res.matched_on = res.matched_on or f"contact:{cp.type}"
            break
    if not res.is_exact_duplicate:
        res.fuzzy_candidates = fuzzy_org_matches(session, name, city)
    return res
