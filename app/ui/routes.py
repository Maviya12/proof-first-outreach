"""Server-rendered UI (Jinja + HTMX). Thin layer over the same services as the JSON API."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, func, select

from app.api.deps import get_fetcher, get_llm, get_mailer
from app.config import get_settings
from app.db import get_session
from app.llm import LLM, BudgetExceeded, LLMOutputError, month_spend_inr
from app.models import (ContactPoint, DiscoveryJob, Integration, Interaction, Lead, Message, MessageStatus, Niche,
                        Organization, PortfolioAsset, utcnow)
from app.security import require_user
from app.services.compliance import cap_reasons
from app.services.leads import evidence_of
from app.services.settings_store import all_settings, set_setting
from app.ui.templating import templates

router = APIRouter(dependencies=[Depends(require_user)], include_in_schema=False)


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _card_ctx(session: Session, m: Message) -> dict:
    from app.api.routes_messages import message_view
    from app.services.suppression import lead_suppression_reasons
    view = message_view(session, m)
    lead = session.get(Lead, m.lead_id)
    warnings = []
    if lead.needs_review:
        warnings.append(lead.needs_review)
    warnings += [f"Suppression: {r}" for r in lead_suppression_reasons(session, lead)]
    warnings += [f"Cap: {r}" for r in cap_reasons(session, m.channel, utcnow(), get_settings())]
    drafts = {}
    for k in ("a", "b"):
        raw = m.draft_a if k == "a" else m.draft_b
        drafts[k] = json.loads(raw) if raw else None
    all_ev = evidence_of(session, lead.id)
    return {"m": view, "drafts": drafts, "warnings": warnings, "all_evidence": all_ev}


@router.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/approvals", status_code=303)


# ---------------- Approval queue ----------------

@router.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, session: Session = Depends(get_session)):
    msgs = session.exec(select(Message).where(Message.status.in_([MessageStatus.APPROVAL_PENDING, MessageStatus.DRAFT]))
                        .order_by(Message.created_at)).all()
    return _render(request, "approvals.html", cards=[_card_ctx(session, m) for m in msgs])


def _card(request: Request, session: Session, m: Message, flash: str | None = None, error: str | None = None):
    session.refresh(m)
    return _render(request, "_approval_card.html", **_card_ctx(session, m), flash=flash, error=error)


@router.post("/ui/messages/{mid}/approve", response_class=HTMLResponse)
def ui_approve(mid: int, request: Request, session: Session = Depends(get_session), mailer=Depends(get_mailer),
               user: str = Depends(require_user)):
    from app.services.dispatcher import approve_message
    m = session.get(Message, mid)
    res = approve_message(session, m, mailer=mailer, actor=user)
    session.commit()
    if res.status == "BLOCKED":
        return _card(request, session, m, error=f"{res.code}: {res.detail}")
    return _render(request, "_done.html", text=f"{res.status}: {res.detail}")


@router.post("/ui/messages/{mid}/edit", response_class=HTMLResponse)
def ui_edit(mid: int, request: Request, body: Optional[str] = Form(None), subject: Optional[str] = Form(None),
            select_draft: Optional[str] = Form(None), session: Session = Depends(get_session),
            user: str = Depends(require_user)):
    from app.services.messaging import apply_edit
    m = session.get(Message, mid)
    try:
        res = apply_edit(session, m, body=body if not select_draft else None,
                         subject=subject if not select_draft else None, select=select_draft, actor=user)
    except ValueError as e:
        return _card(request, session, m, error=str(e))
    session.commit()
    return _card(request, session, m, flash="Saved — validation passed" if res.ok else None,
                 error=None if res.ok else "; ".join(res.errors))


@router.post("/ui/messages/{mid}/reject", response_class=HTMLResponse)
def ui_reject(mid: int, request: Request, reason: str = Form(""), session: Session = Depends(get_session),
              user: str = Depends(require_user)):
    from app.services.dispatcher import reject_message
    reject_message(session, session.get(Message, mid), reason=reason, actor=user)
    session.commit()
    return _render(request, "_done.html", text="Rejected")


# ---------------- Leads ----------------

@router.get("/leads", response_class=HTMLResponse)
def leads(request: Request, status: str = "", niche: str = "", q: str = "", session: Session = Depends(get_session)):
    stmt = select(Lead, Organization).join(Organization, Organization.id == Lead.organization_id)
    if status:
        stmt = stmt.where(Lead.status == status)
    if niche:
        stmt = stmt.join(Niche, Niche.id == Lead.niche_id).where(Niche.slug == niche)
    if q:
        stmt = stmt.where(Organization.name.contains(q))
    rows = session.exec(stmt.order_by(Lead.updated_at.desc()).limit(300)).all()
    counts = dict(session.exec(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    return _render(request, "leads.html", rows=rows, counts=counts, niches=session.exec(select(Niche)).all(),
                   f={"status": status, "niche": niche, "q": q})


def _lead_ctx(session: Session, lead_id: int) -> dict:
    lead = session.get(Lead, lead_id)
    return {"lead": lead, "org": session.get(Organization, lead.organization_id),
            "niche": session.get(Niche, lead.niche_id) if lead.niche_id else None,
            "contacts": session.exec(select(ContactPoint).where(ContactPoint.lead_id == lead_id)).all(),
            "evidence": evidence_of(session, lead_id),
            "messages": session.exec(select(Message).where(Message.lead_id == lead_id).order_by(Message.id)).all(),
            "interactions": session.exec(select(Interaction).where(Interaction.lead_id == lead_id)
                                         .order_by(Interaction.occurred_at)).all()}


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, flash: str = "", error: str = "", session: Session = Depends(get_session)):
    if not session.get(Lead, lead_id):
        return RedirectResponse("/leads", status_code=303)
    return _render(request, "lead_detail.html", **_lead_ctx(session, lead_id), flash=flash or None, error=error or None)


def _back(lead_id: int, flash: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in {"flash": flash, "error": error}.items() if v})
    return RedirectResponse(f"/leads/{lead_id}" + (f"?{qs}" if qs else ""), status_code=303)


def _guard(lead_id: int, fn):  # noqa: ANN001
    try:
        return fn()
    except BudgetExceeded as e:
        return _back(lead_id, error=str(e))
    except LLMOutputError as e:
        return _back(lead_id, error=f"Model output invalid: {e}")
    except ValueError as e:
        return _back(lead_id, error=str(e))


@router.post("/ui/leads/{lead_id}/enrich")
def ui_enrich(lead_id: int, session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
              fetcher=Depends(get_fetcher), user: str = Depends(require_user)):
    from app.services.enrichment import enrich_lead

    def run():
        res = enrich_lead(session, session.get(Lead, lead_id), fetcher=fetcher, llm=llm, actor=user)
        session.commit()
        if not res.get("ok"):
            return _back(lead_id, error=res.get("reason", "failed"))
        return _back(lead_id, flash=f"Enriched {res['pages']} pages: +{res['contacts']} contacts, +{res['evidence']} evidence")
    return _guard(lead_id, run)


@router.post("/ui/leads/{lead_id}/qualify")
def ui_qualify(lead_id: int, session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
               user: str = Depends(require_user)):
    from app.services.qualification import qualify_lead

    def run():
        res = qualify_lead(session, session.get(Lead, lead_id), llm=llm, actor=user)
        session.commit()
        return _back(lead_id, flash=f"Score {res.get('score', 0):.0f} — {'QUALIFIED' if res.get('go') else 'NOT A FIT'}")
    return _guard(lead_id, run)


@router.post("/ui/leads/{lead_id}/generate")
def ui_generate(lead_id: int, channel: str = Form("email"), session: Session = Depends(get_session),
                llm: LLM = Depends(get_llm), user: str = Depends(require_user)):
    from app.services.messaging import GenerationBlocked, generate_message

    def run():
        try:
            m = generate_message(session, session.get(Lead, lead_id), channel, llm=llm, actor=user)
        except GenerationBlocked as e:
            return _back(lead_id, error=f"Blocked: {e}")
        session.commit()
        return RedirectResponse(f"/approvals#m{m.id}", status_code=303)
    return _guard(lead_id, run)


@router.post("/ui/leads/{lead_id}/intake")
async def ui_intake(lead_id: int, platform: str = Form(...), profile_url: str = Form(...), bio: str = Form(""),
                    captions: str = Form(""), dates_note: str = Form(""), user_note: str = Form(""),
                    video_urls: str = Form(""), screenshots: list[UploadFile] = File(default=[]),
                    session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
                    user: str = Depends(require_user)):
    from app.services.intake import IntakeInput, run_intake, split_captions
    shots = [(f.filename, await f.read()) for f in screenshots if f.filename]
    data = IntakeInput(platform=platform, profile_url=profile_url, bio=bio, captions=split_captions(captions),
                       dates_note=dates_note, user_note=user_note,
                       video_urls=[u for u in video_urls.split() if u.startswith("http")], screenshots=shots)

    def run():
        try:
            res = run_intake(session, session.get(Lead, lead_id), data, llm=llm, actor=user)
        except ValueError:
            session.rollback()
            raise
        session.commit()
        msg = f"{len(res['observations'])} observations, {len(res['opportunities'])} ideas, {len(res['dropped'])} dropped"
        return _back(lead_id, flash=msg) if res["sufficient"] else _back(lead_id, error="Insufficient material: " + msg)
    return _guard(lead_id, run)


@router.post("/ui/leads/{lead_id}/reply")
def ui_log_reply(lead_id: int, channel: str = Form(...), body: str = Form(...), session: Session = Depends(get_session),
                 llm: LLM = Depends(get_llm), user: str = Depends(require_user)):
    from app.services.replies import handle_inbound

    def run():
        res = handle_inbound(session, session.get(Lead, lead_id), channel=channel, text=body, llm=llm, actor=user)
        session.commit()
        return _back(lead_id, flash=f"Logged reply: {res.get('classification')}")
    return _guard(lead_id, run)


@router.post("/ui/leads/{lead_id}/status")
def ui_status(lead_id: int, status: str = Form(...), session: Session = Depends(get_session),
              user: str = Depends(require_user)):
    from app.services.leads import set_status
    from app.services.suppression import suppress_lead
    lead = session.get(Lead, lead_id)
    if status in ("DO_NOT_CONTACT", "NOT_INTERESTED"):
        suppress_lead(session, lead, f"manual: {status}", status=status, actor=user)
    else:
        try:
            set_status(session, lead, status, actor=user, reason="manual")
        except ValueError as e:
            return _back(lead_id, error=str(e))
    session.commit()
    return _back(lead_id, flash=f"Status set to {status}")


@router.post("/ui/leads/{lead_id}/override-lock")
def ui_override(lead_id: int, session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.audit import audit
    org = session.get(Organization, session.get(Lead, lead_id).organization_id)
    org.lock_override = True
    audit(session, "org.lock_override", actor=user, entity="organization", entity_id=org.id)
    session.commit()
    return _back(lead_id, flash="Organisation lock overridden")


@router.post("/ui/leads/{lead_id}/erase")
def ui_erase(lead_id: int, session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.services.privacy import erase_lead
    erase_lead(session, session.get(Lead, lead_id), actor=user)
    session.commit()
    return RedirectResponse("/leads", status_code=303)


# ---------------- Discovery ----------------

@router.get("/discovery", response_class=HTMLResponse)
def discovery(request: Request, flash: str = "", error: str = "", session: Session = Depends(get_session)):
    jobs = session.exec(select(DiscoveryJob).order_by(DiscoveryJob.id.desc()).limit(20)).all()
    return _render(request, "discovery.html", jobs=jobs, niches=session.exec(select(Niche)).all(),
                   places_enabled=get_settings().places_discovery_enabled, flash=flash or None, error=error or None,
                   report=None)


@router.post("/ui/discovery", response_class=HTMLResponse)
def ui_discovery(bg: BackgroundTasks, niche: str = Form(...), city: str = Form(...), radius_m: int = Form(5000),
                 limit: int = Form(20), session: Session = Depends(get_session)):
    from app.api.routes_leads import _run_discovery_bg
    if not get_settings().places_discovery_enabled:
        return RedirectResponse("/discovery?error=Places+discovery+is+disabled", status_code=303)
    n = session.exec(select(Niche).where(Niche.slug == niche)).first()
    job = DiscoveryJob(niche_id=n.id if n else None, city=city, radius_m=radius_m, max_results=min(limit, 60))
    session.add(job)
    session.commit()
    bg.add_task(_run_discovery_bg, job.id)
    return RedirectResponse("/discovery?flash=Discovery+started", status_code=303)


@router.post("/ui/import", response_class=HTMLResponse)
async def ui_import(request: Request, file: UploadFile = File(...), niche: str = Form(""),
                    session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.services.csv_import import import_csv
    try:
        rep = import_csv(session, await file.read(), default_niche_slug=niche or None, actor=user).as_dict()
        session.commit()
        error = None
    except ValueError as e:
        rep, error = None, str(e)
    jobs = session.exec(select(DiscoveryJob).order_by(DiscoveryJob.id.desc()).limit(20)).all()
    return _render(request, "discovery.html", jobs=jobs, niches=session.exec(select(Niche)).all(),
                   places_enabled=get_settings().places_discovery_enabled, report=rep, error=error, flash=None)


# ---------------- Handoff ----------------

@router.get("/handoff", response_class=HTMLResponse)
def handoff(request: Request, session: Session = Depends(get_session)):
    from app.api.routes_messages import message_view
    msgs = session.exec(select(Message).where(Message.status == MessageStatus.MANUAL_PENDING).order_by(Message.approved_at)).all()
    return _render(request, "handoff.html", items=[message_view(session, m) for m in msgs])


@router.post("/ui/messages/{mid}/mark-sent", response_class=HTMLResponse)
def ui_mark_sent(mid: int, request: Request, session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.services.dispatcher import mark_sent_manually
    res = mark_sent_manually(session, session.get(Message, mid), actor=user)
    session.commit()
    return _render(request, "_done.html", text="Marked as sent" if res.status == "SENT" else f"{res.code}: {res.detail}")


# ---------------- Inbox ----------------

@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, session: Session = Depends(get_session)):
    rows = session.exec(select(Interaction, Lead, Organization).join(Lead, Lead.id == Interaction.lead_id)
                        .join(Organization, Organization.id == Lead.organization_id)
                        .where(Interaction.direction == "in").order_by(Interaction.occurred_at.desc()).limit(200)).all()
    replies = {m.lead_id: m for m in session.exec(select(Message).where(Message.kind == "reply").order_by(Message.id))}
    return _render(request, "inbox.html", rows=rows, replies=replies)


# ---------------- Portfolio ----------------

@router.get("/portfolio", response_class=HTMLResponse)
def portfolio(request: Request, error: str = "", session: Session = Depends(get_session)):
    return _render(request, "portfolio.html", assets=session.exec(select(PortfolioAsset).order_by(PortfolioAsset.niche_id)).all(),
                   niches={n.id: n for n in session.exec(select(Niche))}, error=error or None)


@router.post("/ui/portfolio")
def ui_portfolio_add(title: str = Form(...), url: str = Form(...), type: str = Form("reel"), tags: str = Form(""),
                     niche_id: str = Form(""), session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.audit import audit
    if not url.startswith("https://"):
        return RedirectResponse("/portfolio?error=URL+must+start+with+https://", status_code=303)
    a = PortfolioAsset(title=title, url=url.strip(), type=type, tags=tags, niche_id=int(niche_id) if niche_id else None)
    session.add(a)
    session.flush()
    audit(session, "portfolio.add", actor=user, entity="portfolio_asset", entity_id=a.id)
    session.commit()
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/ui/portfolio/{aid}/toggle")
def ui_portfolio_toggle(aid: int, session: Session = Depends(get_session)):
    a = session.get(PortfolioAsset, aid)
    a.active = not a.active
    session.add(a)
    session.commit()
    return RedirectResponse("/portfolio", status_code=303)


# ---------------- Settings ----------------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, flash: str = "", error: str = "", connected: str = "",
                  session: Session = Depends(get_session)):
    gi = session.exec(select(Integration).where(Integration.provider == "google")).first()
    return _render(request, "settings.html", s=all_settings(session), gmail=gi, spend=month_spend_inr(session),
                   flash=flash or ("Gmail connected" if connected else None), error=error or None)


@router.post("/ui/settings")
def ui_settings(tone: str = Form("friendly"), language: str = Form("English"), sample_offer_enabled: str = Form(""),
                packages: str = Form("[]"), rates: str = Form("{}"), booking_link: str = Form(""),
                niche_focus: str = Form(""), country_focus: str = Form("IN"), quality_threshold: int = Form(55),
                followup_gaps_days: str = Form("4,9"), session: Session = Depends(get_session),
                user: str = Depends(require_user)):
    from app.audit import audit
    try:
        pk, rt = json.loads(packages or "[]"), json.loads(rates or "{}")
        gaps = [int(x) for x in followup_gaps_days.split(",") if x.strip()][:2]
    except (json.JSONDecodeError, ValueError) as e:
        return RedirectResponse(f"/settings?error=Invalid+JSON:+{e}", status_code=303)
    for k, v in {"tone": tone, "language": language, "sample_offer_enabled": bool(sample_offer_enabled),
                 "packages": pk, "rates": rt, "booking_link": booking_link.strip(), "niche_focus": niche_focus,
                 "country_focus": country_focus.upper(), "quality_threshold": quality_threshold,
                 "followup_gaps_days": gaps or [4, 9]}.items():
        set_setting(session, k, v)
    audit(session, "settings.update", actor=user)
    session.commit()
    return RedirectResponse("/settings?flash=Saved", status_code=303)


@router.post("/ui/suppressions")
def ui_suppress(type: str = Form(...), value: str = Form(...), session: Session = Depends(get_session),
                user: str = Depends(require_user)):
    from app.services.suppression import add_suppression
    try:
        add_suppression(session, type, value, "manual", actor=user)
    except ValueError as e:
        return RedirectResponse(f"/settings?error={e}", status_code=303)
    session.commit()
    return RedirectResponse("/settings?flash=Added+to+do-not-contact", status_code=303)


# ---------------- Funnel ----------------

@router.get("/funnel", response_class=HTMLResponse)
def funnel_page(request: Request, group_by: str = "niche", session: Session = Depends(get_session)):
    from app.services.analytics import funnel
    return _render(request, "funnel.html", data=funnel(session, group_by if group_by in ("niche", "channel", "source") else "niche"))
