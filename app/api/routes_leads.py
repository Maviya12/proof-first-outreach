"""Leads, discovery, enrichment, intake, qualification, message generation."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.deps import get_fetcher, get_llm
from app.api.errors import APIError
from app.config import get_settings
from app.db import get_engine, get_session
from app.llm import LLM, BudgetExceeded, LLMOutputError
from app.models import (ContactPoint, DiscoveryJob, Interaction, Lead, Message, Niche, Organization)
from app.security import require_user
from app.services.csv_import import import_csv
from app.services.enrichment import enrich_lead
from app.services.leads import evidence_of
from app.services.qualification import qualify_lead

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


def _lead_or_404(session: Session, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if not lead:
        raise APIError(404, "NOT_FOUND", "lead not found")
    return lead


def _llm_errors(fn):
    try:
        return fn()
    except BudgetExceeded as e:
        raise APIError(402, "LLM_BUDGET_EXCEEDED", str(e))
    except LLMOutputError as e:
        raise APIError(502, "LLM_OUTPUT_INVALID", str(e))


# ---------- discovery ----------

class DiscoveryIn(BaseModel):
    niche: str
    city: str
    radius_m: int = 5000
    limit: int = 20


def _run_discovery_bg(job_id: int) -> None:
    from app.services.places import PlacesClient, run_discovery
    with Session(get_engine()) as s:
        job = s.get(DiscoveryJob, job_id)
        try:
            run_discovery(s, job, places=PlacesClient(get_settings().google_places_api_key))
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "FAILED", str(e)[:500]
            s.add(job)
            s.commit()


@router.post("/discovery/jobs", status_code=202)
def start_discovery(body: DiscoveryIn, bg: BackgroundTasks, session: Session = Depends(get_session)):
    if not get_settings().places_discovery_enabled:
        raise APIError(409, "PLACES_DISABLED", "Places discovery is off. Confirm the Google Places caching terms "
                                               "allow your use, then set PLACES_DISCOVERY_ENABLED=true. CSV import works now.")
    niche = session.exec(select(Niche).where(Niche.slug == body.niche)).first()
    if not niche:
        raise APIError(404, "NOT_FOUND", f"niche '{body.niche}' not found")
    job = DiscoveryJob(niche_id=niche.id, city=body.city, radius_m=body.radius_m, max_results=min(body.limit, 60))
    session.add(job)
    session.commit()
    bg.add_task(_run_discovery_bg, job.id)
    return job.model_dump()


@router.get("/discovery/jobs/{job_id}")
def discovery_status(job_id: int, session: Session = Depends(get_session)):
    job = session.get(DiscoveryJob, job_id)
    if not job:
        raise APIError(404, "NOT_FOUND", "job not found")
    return job.model_dump()


# ---------- leads ----------

@router.post("/leads/import")
async def import_leads(file: UploadFile = File(...), niche: Optional[str] = Form(None),
                       session: Session = Depends(get_session), user: str = Depends(require_user)):
    data = await file.read()
    if len(data) > 5_000_000:
        raise APIError(413, "TOO_LARGE", "CSV over 5 MB")
    try:
        rep = import_csv(session, data, default_niche_slug=niche, actor=user)
    except ValueError as e:
        raise APIError(422, "INVALID_CSV", str(e))
    session.commit()
    return rep.as_dict()


@router.get("/leads")
def list_leads(niche: Optional[str] = None, country: Optional[str] = None, city: Optional[str] = None,
               status: Optional[str] = None, min_quality: Optional[float] = None, platform: Optional[str] = None,
               limit: int = 200, session: Session = Depends(get_session)):
    q = select(Lead, Organization).join(Organization, Organization.id == Lead.organization_id)
    if niche:
        q = q.join(Niche, Niche.id == Lead.niche_id).where(Niche.slug == niche)
    if country:
        q = q.where(Organization.country == country.upper())
    if city:
        q = q.where(Organization.city == city)
    if status:
        q = q.where(Lead.status == status)
    if min_quality is not None:
        q = q.where(Lead.quality_score >= min_quality)
    if platform:
        q = q.where(Lead.id.in_(select(ContactPoint.lead_id).where(ContactPoint.type == platform)))
    rows = session.exec(q.order_by(Lead.updated_at.desc()).limit(min(limit, 1000))).all()
    return [{**lead.model_dump(), "organization": org.model_dump()} for lead, org in rows]


@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    return {
        **lead.model_dump(),
        "organization": session.get(Organization, lead.organization_id).model_dump(),
        "contact_points": [c.model_dump() for c in session.exec(select(ContactPoint).where(ContactPoint.lead_id == lead_id))],
        "evidence": [e.model_dump() for e in evidence_of(session, lead_id)],
        "messages": [m.model_dump() for m in session.exec(select(Message).where(Message.lead_id == lead_id).order_by(Message.id))],
        "interactions": [i.model_dump() for i in session.exec(select(Interaction).where(Interaction.lead_id == lead_id)
                                                              .order_by(Interaction.occurred_at))],
    }


@router.post("/leads/{lead_id}/enrich")
def enrich(lead_id: int, session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
           fetcher=Depends(get_fetcher), user: str = Depends(require_user)):
    lead = _lead_or_404(session, lead_id)
    res = _llm_errors(lambda: enrich_lead(session, lead, fetcher=fetcher, llm=llm, actor=user))
    session.commit()
    return res


@router.post("/leads/{lead_id}/qualify")
def qualify(lead_id: int, session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
            user: str = Depends(require_user)):
    lead = _lead_or_404(session, lead_id)
    res = _llm_errors(lambda: qualify_lead(session, lead, llm=llm, actor=user))
    session.commit()
    return res


@router.post("/leads/{lead_id}/intake")
async def intake(lead_id: int, platform: str = Form(...), profile_url: str = Form(...), bio: str = Form(""),
                 captions: str = Form(""), dates_note: str = Form(""), user_note: str = Form(""),
                 video_urls: str = Form(""), screenshots: list[UploadFile] = File(default=[]),
                 session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
                 user: str = Depends(require_user)):
    from app.services.intake import IntakeInput, run_intake, split_captions
    lead = _lead_or_404(session, lead_id)
    shots = [(f.filename or f"screenshot{i}", await f.read()) for i, f in enumerate(screenshots, 1) if f.filename]
    data = IntakeInput(platform=platform, profile_url=profile_url, bio=bio, captions=split_captions(captions),
                       dates_note=dates_note, user_note=user_note,
                       video_urls=[u for u in video_urls.split() if u.startswith("http")], screenshots=shots)
    try:
        res = _llm_errors(lambda: run_intake(session, lead, data, llm=llm, actor=user))
    except ValueError as e:
        session.rollback()
        raise APIError(422, "INVALID_INTAKE", str(e))
    session.commit()
    return res


@router.post("/leads/{lead_id}/override-lock")
def override_lock(lead_id: int, session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.audit import audit
    lead = _lead_or_404(session, lead_id)
    org = session.get(Organization, lead.organization_id)
    org.lock_override = True
    session.add(org)
    audit(session, "org.lock_override", actor=user, entity="organization", entity_id=org.id)
    session.commit()
    return {"ok": True}


class YouTubeIn(BaseModel):
    url: str


@router.post("/leads/{lead_id}/youtube")
def youtube(lead_id: int, body: YouTubeIn, session: Session = Depends(get_session)):
    from app.services.youtube import add_youtube_evidence
    lead = _lead_or_404(session, lead_id)
    try:
        res = add_youtube_evidence(session, lead, body.url)
    except (ValueError, RuntimeError) as e:
        raise APIError(422, "YOUTUBE_ERROR", str(e))
    session.commit()
    return res


class StatusIn(BaseModel):
    status: str
    reason: str = ""


MANUAL_STATUSES = {"MEETING_BOOKED", "CLIENT", "NOT_A_FIT", "NOT_INTERESTED", "BLOCKED", "DO_NOT_CONTACT", "INTERESTED"}


@router.patch("/leads/{lead_id}/status")
def update_status(lead_id: int, body: StatusIn, session: Session = Depends(get_session),
                  user: str = Depends(require_user)):
    from app.services.leads import set_status
    from app.services.suppression import suppress_lead
    lead = _lead_or_404(session, lead_id)
    if body.status not in MANUAL_STATUSES:
        raise APIError(422, "INVALID_STATUS", f"manual status must be one of {sorted(MANUAL_STATUSES)}")
    if body.status in ("DO_NOT_CONTACT", "NOT_INTERESTED"):
        suppress_lead(session, lead, body.reason or f"manual: {body.status}", status=body.status, actor=user)
    else:
        try:
            set_status(session, lead, body.status, actor=user, reason=body.reason)
        except ValueError as e:
            raise APIError(409, "INVALID_TRANSITION", str(e))
    session.commit()
    return {"status": lead.status}


@router.get("/niches")
def niches(session: Session = Depends(get_session)):
    return [n.model_dump() for n in session.exec(select(Niche))]
