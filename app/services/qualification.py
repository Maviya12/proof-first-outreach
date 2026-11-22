"""Qualification (spec 6.3): deterministic rubric score + Opus judgement for fit_reason.

The LLM can nudge the score by at most ±15 and write the reason; it cannot clear a lead that
has no usable contact channel.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlmodel import Session

from app.config import Settings, get_settings
from app.llm import LLM, load_prompt
from app.models import Lead, LeadStatus, Niche, Organization
from app.services.leads import contacts_of, evidence_of, set_status
from app.services.niches import niche_config
from app.services.settings_store import get_setting

VIDEO_WORDS = ("video", "reel", "youtube", "instagram", "tiktok", "shorts", "embed", "caption", "hook", "walkthrough")
CONTACT_CHANNELS = {"email", "instagram", "linkedin", "facebook", "x", "whatsapp"}


class Judgement(BaseModel):
    go: bool
    adjustment: int = Field(ge=-15, le=15, description="score nudge, -15..15")
    fit_reason: str = Field(description="2-3 sentences citing evidence ids like [E12]")
    size_band: str | None = Field(default=None, description="solo | small | medium | large, only if evident")


def deterministic_score(session: Session, lead: Lead, cfg: Settings) -> tuple[float, dict]:
    niche = session.get(Niche, lead.niche_id) if lead.niche_id else None
    ncfg = niche_config(niche)
    w = ncfg["rubric"]
    org = session.get(Organization, lead.organization_id)
    evidence = evidence_of(session, lead.id)
    contacts = {c.type for c in contacts_of(session, lead.id)}
    parts: dict[str, float] = {}

    # niche match: niche set and the site description mentions any niche term
    terms = [t.lower() for t in ncfg.get("search_queries", [])] + ([niche.slug] if niche else [])
    desc = (lead.description or "").lower()
    if niche and any(t.split()[0] in desc for t in terms if t):
        parts["niche_match"] = w["niche_match"]
    elif niche:
        parts["niche_match"] = w["niche_match"] * 0.5
    else:
        parts["niche_match"] = 0

    focus_country = (get_setting(session, "country_focus") or "").upper()
    if org and org.country and focus_country:
        parts["location"] = w["location"] if org.country.upper() == focus_country else 0
    else:
        parts["location"] = w["location"] * 0.5

    if "email" in contacts:
        parts["contactability"] = w["contactability"]
    elif contacts & CONTACT_CHANNELS:
        parts["contactability"] = w["contactability"] * 0.6
    else:
        parts["contactability"] = 0

    video_hits = sum(1 for e in evidence if any(v in e.snippet.lower() for v in VIDEO_WORDS))
    video_hits += len(contacts & {"instagram", "youtube"})
    parts["video_activity"] = w["video_activity"] * min(1.0, video_hits / 4)

    parts["intent_signal"] = w["intent_signal"] if lead.intent_signal else 0
    return round(sum(parts.values()), 1), parts


def qualify_lead(session: Session, lead: Lead, *, llm: LLM | None = None, cfg: Settings | None = None,
                 actor: str = "system") -> dict:
    cfg = cfg or get_settings()
    if lead.status in (LeadStatus.DO_NOT_CONTACT, LeadStatus.BLOCKED):
        return {"ok": False, "reason": f"lead is {lead.status}"}
    det, parts = deterministic_score(session, lead, cfg)
    threshold = float(get_setting(session, "quality_threshold") or 55)
    reachable = parts["contactability"] > 0
    adj, reason, go = 0, None, reachable and det >= threshold
    if llm is not None:
        niche = session.get(Niche, lead.niche_id) if lead.niche_id else None
        ev_lines = "\n".join(f"[E{e.id}] ({e.kind}, {e.source_type}) {e.snippet}" for e in evidence_of(session, lead.id))
        org = session.get(Organization, lead.organization_id)
        content = (f"Niche: {niche.name if niche else 'unspecified'}\nPlaybook: {niche_config(niche).get('playbook', '')}\n"
                   f"Business: {org.name if org else ''} ({org.city if org else ''})\nDescription: {lead.description or ''}\n"
                   f"Intent signal: {lead.intent_signal or 'none'}\nDeterministic score: {det} {parts}\n\nEvidence:\n{ev_lines or '(none)'}")
        j = llm.json(task="qualify", model=cfg.model_judge, system=load_prompt("qualify_system.md"),
                     content=content, schema=Judgement, max_tokens=800)
        adj, reason = j.adjustment, j.fit_reason
        if j.size_band:
            lead.size_band = j.size_band
        go = reachable and j.go and (det + adj) >= threshold
    score = max(0.0, min(100.0, det + adj))
    lead.quality_score = score
    lead.fit_reason = reason or f"Rubric score {det}: " + ", ".join(f"{k}={v:g}" for k, v in parts.items())
    if not reachable:
        lead.fit_reason += " | No usable contact channel."
    session.add(lead)
    set_status(session, lead, LeadStatus.QUALIFIED if go else LeadStatus.NOT_A_FIT, actor=actor,
               reason=f"score {score:.0f} vs threshold {threshold:.0f}")
    return {"ok": True, "score": score, "deterministic": det, "parts": parts, "go": go, "fit_reason": lead.fit_reason}
