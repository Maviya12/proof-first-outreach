"""Follow-up manager (spec 6.8).

At most `max_followups` (default 2), spaced by `followup_gaps_days` (default 4 and 9) after the
previous outbound message. A follow-up draft is generated only when it is due and goes to approval
like a first message. Plain-code guards: never after any inbound reply, never when suppressed,
never twice in parallel, never on a lead that isn't in SENT.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.llm import LLM, BudgetExceeded, LLMOutputError
from app.models import EMAIL_CHANNEL, Interaction, Lead, LeadStatus, Message, MessageStatus, utcnow
from app.services.messaging import GenerationBlocked, generate_message
from app.services.settings_store import get_setting
from app.services.suppression import lead_suppression_reasons

log = logging.getLogger(__name__)
OPEN = (MessageStatus.DRAFT, MessageStatus.APPROVAL_PENDING, MessageStatus.APPROVED, MessageStatus.SCHEDULED,
        MessageStatus.MANUAL_PENDING)
DONE = (MessageStatus.SENT, MessageStatus.SENT_MANUAL)


def followup_due(session: Session, lead: Lead, now: datetime) -> tuple[int, Message] | None:
    """Return (followup_number, initial_message) if a follow-up is due now, else None."""
    if lead.status != LeadStatus.SENT or lead_suppression_reasons(session, lead):
        return None
    msgs = session.exec(select(Message).where(Message.lead_id == lead.id).order_by(Message.id)).all()
    initial = next((m for m in msgs if m.kind == "initial" and m.status in DONE), None)
    if initial is None or initial.sent_at is None:
        return None
    if session.exec(select(Interaction.id).where(Interaction.lead_id == lead.id, Interaction.direction == "in")).first():
        return None
    followups = [m for m in msgs if m.kind == "followup"]
    if any(m.status in OPEN for m in followups):
        return None
    sent_fu = [m for m in followups if m.status in DONE]
    max_n = int(get_setting(session, "max_followups") or 2)
    gaps = list(get_setting(session, "followup_gaps_days") or [4, 9])
    n = len(sent_fu) + 1
    if n > min(max_n, 2, len(gaps)):
        return None
    last_out = max([initial.sent_at, *[m.sent_at for m in sent_fu if m.sent_at]])
    if now < last_out + timedelta(days=gaps[n - 1]):
        return None
    return n, initial


def plan_followups(session: Session, *, llm: LLM | None = None, now: datetime | None = None,
                   cfg: Settings | None = None) -> dict:
    cfg = cfg or get_settings()
    now = now or utcnow()
    if llm is None:
        if not cfg.anthropic_api_key:
            return {}
        llm = LLM(session, cfg=cfg)
    created, skipped = 0, 0
    for lead in session.exec(select(Lead).where(Lead.status == LeadStatus.SENT)).all():
        due = followup_due(session, lead, now)
        if not due:
            continue
        n, initial = due
        try:
            msg = generate_message(session, lead, initial.channel, llm=llm, kind="followup", followup_number=n, cfg=cfg)
        except (GenerationBlocked, LLMOutputError) as e:
            log.info("follow-up for lead %s skipped: %s", lead.id, e)
            skipped += 1
            continue
        except BudgetExceeded:
            break
        if initial.channel == EMAIL_CHANNEL:
            base = initial.subject or ""
            msg.subject = base if base.lower().startswith("re:") else f"Re: {base}"
            msg.thread_id = initial.thread_id
        session.add(msg)
        session.commit()
        created += 1
    return {"created": created, "skipped": skipped} if created or skipped else {}
