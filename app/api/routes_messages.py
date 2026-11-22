"""Messages, approvals, handoff, manual interactions, portfolio, analytics."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.deps import get_llm, get_mailer
from app.api.errors import APIError
from app.audit import audit
from app.db import get_session
from app.llm import LLM, BudgetExceeded, LLMOutputError
from app.models import Lead, Message, MessageStatus, Organization, PortfolioAsset
from app.security import require_user
from app.services.dispatcher import approve_message, mark_sent_manually, reject_message
from app.services.messaging import GenerationBlocked, apply_edit, generate_message
from app.services.normalize import profile_url
from app.services.replies import handle_inbound

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


def _msg_or_404(session: Session, mid: int) -> Message:
    m = session.get(Message, mid)
    if not m:
        raise APIError(404, "NOT_FOUND", "message not found")
    return m


class GenerateIn(BaseModel):
    channel: str = "email"


@router.post("/leads/{lead_id}/messages/generate")
def generate(lead_id: int, body: GenerateIn, session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
             user: str = Depends(require_user)):
    lead = session.get(Lead, lead_id)
    if not lead:
        raise APIError(404, "NOT_FOUND", "lead not found")
    try:
        msg = generate_message(session, lead, body.channel, llm=llm, actor=user)
    except GenerationBlocked as e:
        raise APIError(409, "GENERATION_BLOCKED", str(e))
    except BudgetExceeded as e:
        raise APIError(402, "LLM_BUDGET_EXCEEDED", str(e))
    except LLMOutputError as e:
        raise APIError(502, "LLM_OUTPUT_INVALID", str(e))
    session.commit()
    return msg.model_dump()


def message_view(session: Session, m: Message) -> dict:
    lead = session.get(Lead, m.lead_id)
    org = session.get(Organization, lead.organization_id)
    asset = session.get(PortfolioAsset, m.portfolio_asset_id) if m.portfolio_asset_id else None
    from app.models import Evidence, MessageEvidence
    ev_ids = list(session.exec(select(MessageEvidence.evidence_id).where(MessageEvidence.message_id == m.id)))
    evidence = [session.get(Evidence, i).model_dump() for i in ev_ids]
    return {**m.model_dump(), "lead": lead.model_dump(), "organization": org.model_dump(),
            "portfolio_asset": asset.model_dump() if asset else None, "evidence": evidence,
            "validation": json.loads(m.validation_json or "{}"),
            "profile_url": profile_url(m.channel, m.to_address or "") if m.channel != "email" else None}


@router.get("/approvals")
def approvals(session: Session = Depends(get_session)):
    msgs = session.exec(select(Message).where(Message.status.in_([MessageStatus.APPROVAL_PENDING, MessageStatus.DRAFT]))
                        .order_by(Message.created_at)).all()
    return [message_view(session, m) for m in msgs]


@router.post("/messages/{mid}/approve")
def approve(mid: int, session: Session = Depends(get_session), user: str = Depends(require_user),
            mailer=Depends(get_mailer)):
    m = _msg_or_404(session, mid)
    res = approve_message(session, m, mailer=mailer, actor=user)
    session.commit()
    if res.status == "BLOCKED":
        return JSONResponse({"error": res.code, "detail": res.detail, **res.extra}, status_code=409)
    return JSONResponse(res.as_dict(), status_code=res.http_status)


class RejectIn(BaseModel):
    reason: str = ""


@router.post("/messages/{mid}/reject")
def reject(mid: int, body: RejectIn, session: Session = Depends(get_session), user: str = Depends(require_user)):
    m = _msg_or_404(session, mid)
    try:
        reject_message(session, m, reason=body.reason, actor=user)
    except ValueError as e:
        raise APIError(409, "INVALID_STATE", str(e))
    session.commit()
    return {"status": m.status}


class EditIn(BaseModel):
    body: Optional[str] = None
    subject: Optional[str] = None
    select: Optional[str] = None  # "a" | "b"


@router.post("/messages/{mid}/edit")
def edit(mid: int, body: EditIn, session: Session = Depends(get_session), user: str = Depends(require_user)):
    m = _msg_or_404(session, mid)
    try:
        res = apply_edit(session, m, body=body.body, subject=body.subject, select=body.select, actor=user)
    except ValueError as e:
        raise APIError(409, "INVALID_STATE", str(e))
    session.commit()
    return {"status": m.status, "validation": res.as_dict()}


@router.post("/messages/{mid}/mark-sent-manually")
def mark_sent(mid: int, session: Session = Depends(get_session), user: str = Depends(require_user)):
    m = _msg_or_404(session, mid)
    res = mark_sent_manually(session, m, actor=user)
    session.commit()
    if res.status == "BLOCKED":
        return JSONResponse({"error": res.code, "detail": res.detail}, status_code=409)
    return res.as_dict()


@router.get("/handoff")
def handoff_queue(session: Session = Depends(get_session)):
    msgs = session.exec(select(Message).where(Message.status == MessageStatus.MANUAL_PENDING)
                        .order_by(Message.approved_at)).all()
    return [message_view(session, m) for m in msgs]


class ManualInteractionIn(BaseModel):
    lead_id: int
    channel: str
    body: str


@router.post("/interactions/manual")
def manual_interaction(body: ManualInteractionIn, session: Session = Depends(get_session), llm: LLM = Depends(get_llm),
                       user: str = Depends(require_user)):
    lead = session.get(Lead, body.lead_id)
    if not lead:
        raise APIError(404, "NOT_FOUND", "lead not found")
    try:
        res = handle_inbound(session, lead, channel=body.channel, text=body.body, llm=llm, actor=user)
    except BudgetExceeded as e:
        raise APIError(402, "LLM_BUDGET_EXCEEDED", str(e))
    session.commit()
    return res


@router.get("/inbox")
def inbox(session: Session = Depends(get_session), limit: int = 100):
    from app.models import Interaction
    rows = session.exec(select(Interaction).where(Interaction.direction == "in")
                        .order_by(Interaction.occurred_at.desc()).limit(limit)).all()
    out = []
    for r in rows:
        lead = session.get(Lead, r.lead_id)
        reply = session.exec(select(Message).where(Message.lead_id == r.lead_id, Message.kind == "reply")
                             .order_by(Message.id.desc())).first()
        out.append({**r.model_dump(), "lead_status": lead.status if lead else None,
                    "organization": session.get(Organization, lead.organization_id).name if lead else None,
                    "suggested_reply": message_view(session, reply) if reply else None})
    return out


# ---------- portfolio ----------

class AssetIn(BaseModel):
    niche_id: Optional[int] = None
    title: str
    url: str
    type: str = "reel"
    tags: str = ""
    active: bool = True


class AssetPatch(BaseModel):
    id: int
    title: Optional[str] = None
    url: Optional[str] = None
    type: Optional[str] = None
    tags: Optional[str] = None
    active: Optional[bool] = None
    niche_id: Optional[int] = None


@router.get("/portfolio")
def list_portfolio(session: Session = Depends(get_session)):
    return [a.model_dump() for a in session.exec(select(PortfolioAsset).order_by(PortfolioAsset.niche_id, PortfolioAsset.id))]


@router.post("/portfolio", status_code=201)
def add_portfolio(body: AssetIn, session: Session = Depends(get_session), user: str = Depends(require_user)):
    if not body.url.startswith("https://"):
        raise APIError(422, "INVALID_URL", "portfolio URLs must be https://")
    a = PortfolioAsset(**body.model_dump())
    session.add(a)
    session.flush()
    audit(session, "portfolio.add", actor=user, entity="portfolio_asset", entity_id=a.id)
    session.commit()
    return a.model_dump()


@router.patch("/portfolio")
def patch_portfolio(body: AssetPatch, session: Session = Depends(get_session), user: str = Depends(require_user)):
    a = session.get(PortfolioAsset, body.id)
    if not a:
        raise APIError(404, "NOT_FOUND", "asset not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        if k != "id":
            setattr(a, k, v)
    session.add(a)
    audit(session, "portfolio.update", actor=user, entity="portfolio_asset", entity_id=a.id)
    session.commit()
    return a.model_dump()


# ---------- follow-ups ----------

@router.post("/followups/run")
def run_followups(session: Session = Depends(get_session), llm: LLM = Depends(get_llm)):
    from app.services.followups import plan_followups
    return plan_followups(session, llm=llm) or {"created": 0, "skipped": 0}


# ---------- analytics ----------

@router.get("/analytics/funnel")
def funnel(group_by: str = "niche", session: Session = Depends(get_session)):
    from app.services.analytics import funnel as f
    if group_by not in ("niche", "channel", "source"):
        raise APIError(422, "INVALID", "group_by must be niche, channel or source")
    return f(session, group_by)
