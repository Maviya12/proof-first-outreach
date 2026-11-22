"""CSV import of leads gathered by hand (Meta Ad Library, job posts, directories).

Expected headers (case-insensitive, extras ignored):
  name*, website, city, country, niche, email, phone, instagram, linkedin, facebook, x, youtube,
  contact_name, contact_role, intent_signal, source, source_url, notes
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Niche
from app.services.compliance import is_eu_uk, to_country_code
from app.services.leads import add_evidence, create_lead

CONTACT_COLUMNS = ["email", "phone", "instagram", "linkedin", "facebook", "x", "youtube", "whatsapp"]


@dataclass
class ImportReport:
    created: list[int] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"created": len(self.created), "created_ids": self.created, "duplicates": self.duplicates,
                "flagged": self.flagged, "skipped": self.skipped}


def import_csv(session: Session, data: str | bytes, *, default_niche_slug: str | None = None,
               actor: str = "admin") -> ImportReport:
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(data))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValueError("CSV must have a 'name' column")
    niches = {n.slug: n.id for n in session.exec(select(Niche))}
    allow_eu = get_settings().allow_eu_uk
    rep = ImportReport()
    for i, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = row.get("name")
        if not name:
            rep.skipped.append({"row": i, "reason": "missing name"})
            continue
        country = to_country_code(row.get("country")) or row.get("country") or None
        if not allow_eu and is_eu_uk(country, row.get("email") or row.get("website")):
            rep.skipped.append({"row": i, "name": name, "reason": "EU/UK recipient (ALLOW_EU_UK=false)"})
            continue
        slug = row.get("niche") or default_niche_slug
        if slug and slug not in niches:
            rep.skipped.append({"row": i, "name": name, "reason": f"unknown niche '{slug}'"})
            continue
        src_url = row.get("source_url") or None
        contacts = [(c, row[c], src_url) for c in CONTACT_COLUMNS if row.get(c)]
        res = create_lead(session, name=name, website=row.get("website") or None, city=row.get("city") or None,
                          country=country, niche_id=niches.get(slug) if slug else None, contacts=contacts,
                          source=row.get("source") or "csv", intent_signal=row.get("intent_signal") or None,
                          contact_name=row.get("contact_name") or None, contact_role=row.get("contact_role") or None,
                          actor=actor)
        if res.lead is None:
            rep.duplicates.append({"row": i, "name": name, "duplicate_of": res.duplicate_of, "matched_on": res.matched_on})
            continue
        if row.get("notes"):
            add_evidence(session, res.lead, kind="note", source_type="user_note", snippet=row["notes"],
                         source_url=src_url, source_ref=f"csv row {i}")
        rep.created.append(res.lead.id)
        if res.flagged:
            rep.flagged.append({"row": i, "lead_id": res.lead.id, "warnings": res.flagged})
    return rep
