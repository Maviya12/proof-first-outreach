"""Inbound replies (spec 6.7): keyword opt-out first, Haiku classification second, Opus draft.

Any inbound reply cancels scheduled follow-ups. Opt-outs suppress the contact AND organisation
immediately, before any LLM is called.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.audit import audit
from app.config import Settings, get_settings
from app.llm import LLM, load_prompt
from app.models import (EMAIL_CHANNEL, Interaction, Lead, LeadStatus, Message, MessageStatus, Niche, PortfolioAsset,
                        utcnow)
from app.services.leads import set_status
from app.services.niches import niche_config
from app.services.optout import NOT_INTERESTED, UNSUBSCRIBE, detect_optout
from app.services.portfolio import select_asset
from app.services.settings_store import all_settings
from app.services.suppression import suppress_lead

LABELS = ["SEND_WORK", "PRICING", "PACKAGE", "INTERESTED", "NOT_INTERESTED", "UNSUBSCRIBE", "OUT_OF_OFFICE",
          "QUESTION", "OTHER"]
POSITIVE = {"SEND_WORK", "PRICING", "PACKAGE", "INTERESTED"}


class ClassifyOut(BaseModel):
    label: Literal["SEND_WORK", "PRICING", "PACKAGE", "INTERESTED", "NOT_INTERESTED", "UNSUBSCRIBE",
                   "OUT_OF_OFFICE", "QUESTION", "OTHER"]
    language: str = "en"
    confidence: float = Field(ge=0, le=1, default=0.7)


class ReplyDraftOut(BaseModel):
    classification: str
    body: str = ""
    needs_user_input: list[str] = []


CLASSIFY_SYSTEM = ("Classify this reply to a freelance video editor's outreach message into exactly one label: "
                   + ", ".join(LABELS) + ". Also give the language (en, hi-Latn for Hinglish, hi, mr, ...). "
                   "Any request to stop contact, in any language, is UNSUBSCRIBE. A polite decline is NOT_INTERESTED.")


def cancel_followups(session: Session, lead_id: int, reason: str) -> int:
    n = 0
    for m in session.exec(select(Message).where(Message.lead_id == lead_id, Message.kind == "followup")):
        if m.status in (MessageStatus.DRAFT, MessageStatus.APPROVAL_PENDING, MessageStatus.APPROVED,
                        MessageStatus.SCHEDULED, MessageStatus.MANUAL_PENDING):
            m.status, m.error = MessageStatus.CANCELLED, reason
            session.add(m)
            n += 1
    return n


def classify(text: str, *, llm: LLM | None, cfg: Settings) -> tuple[str, str, str]:
    """Returns (label, language, method)."""
    oo = detect_optout(text)
    if oo:
        return (UNSUBSCRIBE if oo.kind == UNSUBSCRIBE else NOT_INTERESTED), "", f"keyword:{oo.matched}"
    if re.search(r"\b(out of (the )?office|on leave|auto-?reply|automatic reply|away until)\b", text, re.I):
        return "OUT_OF_OFFICE", "", "keyword:ooo"
    if llm is None:
        return "OTHER", "", "none"
    out = llm.json(task="reply_classify", model=cfg.model_classify, system=CLASSIFY_SYSTEM, content=text[:4000],
                   schema=ClassifyOut, max_tokens=150)
    return out.label, out.language, "llm"


def handle_inbound(session: Session, lead: Lead, *, channel: str, text: str, raw_ref: str | None = None,
                   occurred_at: datetime | None = None, llm: LLM | None = None, cfg: Settings | None = None,
                   draft: bool = True, actor: str = "system") -> dict:
    cfg = cfg or get_settings()
    if raw_ref and session.exec(select(Interaction.id).where(Interaction.raw_ref == raw_ref,
                                                             Interaction.direction == "in")).first():
        return {"duplicate": True}
    inter = Interaction(lead_id=lead.id, channel=channel, direction="in", body=text, raw_ref=raw_ref,
                        occurred_at=occurred_at or utcnow())
    session.add(inter)
    session.flush()
    cancelled = cancel_followups(session, lead.id, "inbound reply received")

    label, lang, method = classify(text, llm=llm, cfg=cfg)
    inter.classification = label
    session.add(inter)
    result = {"interaction_id": inter.id, "classification": label, "language": lang, "method": method,
              "followups_cancelled": cancelled, "reply_message_id": None}

    if label in (UNSUBSCRIBE, NOT_INTERESTED):
        status = LeadStatus.DO_NOT_CONTACT if label == UNSUBSCRIBE else LeadStatus.NOT_INTERESTED
        suppress_lead(session, lead, f"reply: {label.lower()} ({method})", status=status, actor=actor)
        inter.handled = True
        audit(session, "reply.optout", actor=actor, entity="lead", entity_id=lead.id, label=label, method=method)
        return result

    if lead.status not in (LeadStatus.DO_NOT_CONTACT, LeadStatus.BLOCKED):
        if lead.status in (LeadStatus.SENT, LeadStatus.APPROVAL_PENDING, LeadStatus.QUALIFIED, LeadStatus.MESSAGE_READY):
            set_status(session, lead, LeadStatus.REPLIED, actor=actor)
        if label in POSITIVE and lead.status == LeadStatus.REPLIED:
            set_status(session, lead, LeadStatus.INTERESTED, actor=actor)
    audit(session, "reply.received", actor=actor, entity="lead", entity_id=lead.id, label=label, method=method)

    if draft and llm is not None and label not in ("OUT_OF_OFFICE",):
        msg = draft_reply(session, lead, inter, llm=llm, cfg=cfg, actor=actor)
        result["reply_message_id"] = msg.id if msg else None
    return result


MONEY_RE = re.compile(r"(?:₹|rs\.?|inr|\$|usd|eur|€|£)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:₹|rs|inr|/-|k\b)", re.I)


def _money_tokens(s: str) -> set[str]:
    return {re.sub(r"[^\d.]", "", m) for m in MONEY_RE.findall(s or "")}


def draft_reply(session: Session, lead: Lead, inter: Interaction, *, llm: LLM, cfg: Settings | None = None,
                actor: str = "system") -> Message | None:
    """Draft a reply into approval. Prices may only come from settings (checked in code)."""
    cfg = cfg or get_settings()
    st = all_settings(session)
    niche = session.get(Niche, lead.niche_id) if lead.niche_id else None
    asset = select_asset(session, lead)
    prev_out = session.exec(select(Message).where(Message.lead_id == lead.id, Message.status.in_(
        [MessageStatus.SENT, MessageStatus.SENT_MANUAL])).order_by(Message.id.desc())).first()
    history = session.exec(select(Interaction).where(Interaction.lead_id == lead.id).order_by(Interaction.occurred_at)).all()
    convo = "\n".join(f"[{h.direction.upper()} {h.channel}] {h.body or ''}" for h in history[-6:])
    settings_block = json.dumps({"packages": st.get("packages"), "rates": st.get("rates"),
                                 "booking_link": st.get("booking_link"), "tone": st.get("tone"),
                                 "language": st.get("language")}, ensure_ascii=False)
    content = (f"Settings (the ONLY allowed prices/packages): {settings_block}\n"
               f"Portfolio URL: {asset.url if asset else 'none'}\n"
               f"Niche: {niche.name if niche else 'general'} | medical: {niche_config(niche).get('is_medical')}\n"
               f"Recipient: {lead.contact_name or 'unknown'}\n\nConversation so far:\n{convo}\n\n"
               f"Latest reply to answer:\n{inter.body}")
    out = llm.json(task="reply_draft", model=cfg.model_judge, system=load_prompt("reply_system.md"), content=content,
                   schema=ReplyDraftOut, max_tokens=900)
    needs = list(out.needs_user_input)
    body = (out.body or "").strip()
    # plain-code guard: every amount in the draft must appear in settings
    allowed = _money_tokens(settings_block)
    stray = _money_tokens(body) - allowed
    if stray:
        needs.append(f"price(s) {sorted(stray)} not in settings — fill packages/rates")
        body = ""
    if inter.classification in ("PRICING", "PACKAGE") and not (st.get("packages") or st.get("rates")):
        needs.append("packages / rates are empty in Settings")
        body = ""
    if out.classification in ("NOT_INTERESTED", "UNSUBSCRIBE"):
        return None  # never push after a decline
    subject = None
    if inter.channel == EMAIL_CHANNEL and prev_out and prev_out.subject:
        subject = prev_out.subject if prev_out.subject.lower().startswith("re:") else f"Re: {prev_out.subject}"
    from app.services.messaging import validate_for_lead
    v = None
    if body:
        v = validate_for_lead(session, lead, body=body, subject=subject, channel=inter.channel, claims=[],
                              asset=asset, kind="reply")
    approval_needed = st.get("approval_required_for_thread_replies", True)
    status = MessageStatus.APPROVAL_PENDING if body and v and v.ok else MessageStatus.DRAFT
    msg = Message(lead_id=lead.id, channel=inter.channel, kind="reply", body=body or None, subject=subject,
                  to_address=prev_out.to_address if prev_out else None, thread_id=prev_out.thread_id if prev_out else None,
                  portfolio_asset_id=asset.id if asset else None, status=status,
                  validation_json=json.dumps({"final": v.as_dict() if v else {"ok": False, "errors": ["needs input"]},
                                              "needs_user_input": needs, "classification": out.classification,
                                              "auto_approve_allowed": not approval_needed}))
    session.add(msg)
    session.flush()
    inter.handled = True
    session.add(inter)
    audit(session, "reply.draft", actor=actor, entity="message", entity_id=msg.id, needs_user_input=needs)
    return msg
