"""Outreach dispatcher (spec 6.6). Plain code only, fails closed.

approve_message(): preflight (validation, suppression, EU/UK, organisation lock) -> APPROVED,
then dispatch().  A preflight failure leaves the message in APPROVAL_PENDING and returns a 409.

dispatch(): re-checks everything (time may have passed), then caps / send window. If caps or the
window block, the message is SCHEDULED for later and the dispatch_due job retries it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.audit import audit
from app.config import Settings, get_settings
from app.models import (EMAIL_CHANNEL, HANDOFF_CHANNELS, Interaction, Lead, LeadStatus, Message, MessageStatus,
                        Organization, utcnow)
from app.services.compliance import (apply_org_lock, cap_reasons, in_send_window, is_eu_uk, local_day_start_utc,
                                     next_window_start, org_lock_status)
from app.services.leads import set_status
from app.services.normalize import profile_url
from app.services.suppression import address_suppressed, lead_suppression_reasons


@dataclass
class DispatchResult:
    status: str  # SENT | DRY_RUN | MANUAL_PENDING | SCHEDULED | BLOCKED | FAILED
    code: str | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def http_status(self) -> int:
        return {"BLOCKED": 409, "FAILED": 502}.get(self.status, 200)

    def as_dict(self) -> dict:
        return {"status": self.status, "error": self.code, "detail": self.detail, **self.extra}


def _preflight(session: Session, msg: Message, lead: Lead, now: datetime, cfg: Settings) -> DispatchResult | None:
    reasons = lead_suppression_reasons(session, lead)
    if msg.to_address and address_suppressed(session, msg.channel, msg.to_address):
        reasons.append(f"{msg.channel} address suppressed")
    if reasons:
        return DispatchResult("BLOCKED", "SUPPRESSED", "; ".join(sorted(set(reasons))))
    if not msg.to_address:
        return DispatchResult("BLOCKED", "NO_RECIPIENT", "message has no recipient")
    if msg.kind == "followup":
        replied = session.exec(select(Interaction.id).where(Interaction.lead_id == lead.id,
                                                            Interaction.direction == "in")).first()
        if replied or lead.status != LeadStatus.SENT:
            return DispatchResult("BLOCKED", "LEAD_REPLIED", f"follow-ups stop once a lead replies (status {lead.status})")
    org = session.get(Organization, lead.organization_id)
    if not cfg.allow_eu_uk and org and is_eu_uk(org.country, msg.to_address if msg.channel == EMAIL_CHANNEL else org.domain_norm):
        return DispatchResult("BLOCKED", "EU_UK_EXCLUDED", "EU/UK recipients are excluded (ALLOW_EU_UK=false)")
    lock = org_lock_status(session, lead, "initial" if msg.kind == "initial" else msg.kind, now, cfg)
    if lock.locked:
        return DispatchResult("BLOCKED", "ORG_LOCKED", lock.detail,
                              {"locked_until": lock.locked_until.date().isoformat() if lock.locked_until else None})
    return None


def _validation_ok(msg: Message) -> bool:
    v = json.loads(msg.validation_json or "{}")
    if "final" in v:
        return bool(v["final"].get("ok"))
    sel = v.get("selected")
    return bool(sel and v.get(sel, {}).get("ok")) or msg.kind == "reply" and msg.status == MessageStatus.APPROVAL_PENDING


def approve_message(session: Session, msg: Message, *, mailer=None, now: datetime | None = None,  # noqa: ANN001
                    cfg: Settings | None = None, actor: str = "admin") -> DispatchResult:
    cfg = cfg or get_settings()
    now = now or utcnow()
    if msg.status != MessageStatus.APPROVAL_PENDING:
        return DispatchResult("BLOCKED", "INVALID_STATE", f"message is {msg.status}, not APPROVAL_PENDING")
    if not _validation_ok(msg):
        return DispatchResult("BLOCKED", "VALIDATION_FAILED", "draft has validation errors; edit it first")
    lead = session.get(Lead, msg.lead_id)
    pre = _preflight(session, msg, lead, now, cfg)
    if pre:
        audit(session, "message.approve_blocked", actor=actor, entity="message", entity_id=msg.id, code=pre.code,
              detail=pre.detail)
        return pre
    msg.status = MessageStatus.APPROVED
    msg.approved_at = now
    session.add(msg)
    audit(session, "message.approve", actor=actor, entity="message", entity_id=msg.id, channel=msg.channel)
    return dispatch(session, msg, mailer=mailer, now=now, cfg=cfg)


def dispatch(session: Session, msg: Message, *, mailer=None, now: datetime | None = None,  # noqa: ANN001
             cfg: Settings | None = None) -> DispatchResult:
    cfg = cfg or get_settings()
    now = now or utcnow()
    lead = session.get(Lead, msg.lead_id)
    if msg.status not in (MessageStatus.APPROVED, MessageStatus.SCHEDULED):
        return DispatchResult("BLOCKED", "INVALID_STATE", f"message is {msg.status}")

    # 2-3: suppression, EU/UK, organisation lock — re-checked at send time
    pre = _preflight(session, msg, lead, now, cfg)
    if pre:
        msg.status = MessageStatus.CANCELLED if pre.code == "LEAD_REPLIED" else MessageStatus.FAILED
        msg.error = f"{pre.code}: {pre.detail}"
        session.add(msg)
        audit(session, "dispatch.blocked", entity="message", entity_id=msg.id, code=pre.code, detail=pre.detail)
        return pre

    # 4: caps, min gap, send window
    reasons = cap_reasons(session, msg.channel, now, cfg)
    window_ok = in_send_window(now, cfg.timezone, cfg.send_window) if msg.channel == EMAIL_CHANNEL else True
    if reasons or not window_ok:
        if any("daily" in r for r in reasons):
            nxt = next_window_start(local_day_start_utc(now, cfg.timezone) + timedelta(days=1), cfg.timezone, cfg.send_window)
        elif reasons:
            nxt = now + timedelta(seconds=max(cfg.min_gap_seconds_email, 600))
        else:
            nxt = next_window_start(now, cfg.timezone, cfg.send_window)
        msg.status, msg.scheduled_for = MessageStatus.SCHEDULED, nxt
        session.add(msg)
        why = reasons or ["outside send window"]
        audit(session, "dispatch.deferred", entity="message", entity_id=msg.id, reasons=why, until=nxt)
        return DispatchResult("SCHEDULED", None, "; ".join(why), {"scheduled_for": nxt.isoformat()})

    # Phase 6: replies to people who messaged first, inside Meta's 24h window, via the official Send API
    if msg.channel in ("instagram", "facebook") and msg.kind == "reply":
        from app.services.meta_inbound import can_auto_send, send_reply, within_window
        if can_auto_send(session, msg, cfg) and within_window(session, lead, now):
            if not cfg.send_enabled:
                audit(session, "dispatch.dry_run", entity="message", entity_id=msg.id, channel=msg.channel)
                return DispatchResult("DRY_RUN", None, "SEND_ENABLED=false: Meta reply not sent")
            try:
                mid = send_reply(msg, cfg)
            except Exception as e:  # noqa: BLE001
                msg.status, msg.error = MessageStatus.FAILED, str(e)[:500]
                session.add(msg)
                return DispatchResult("FAILED", "SEND_FAILED", str(e)[:200])
            mark_sent(session, msg, now=now, cfg=cfg, provider_message_id=mid)
            audit(session, "dispatch.sent_meta", entity="message", entity_id=msg.id)
            return DispatchResult("SENT", None, "sent via Meta Send API (24h window)")
        # outside the window or not enabled: the human replies from their own inbox

    # 6: handoff channels — the human sends
    if msg.channel in HANDOFF_CHANNELS:
        msg.status = MessageStatus.MANUAL_PENDING
        msg.approved_at = msg.approved_at or now
        msg.scheduled_for = None
        session.add(msg)
        audit(session, "dispatch.handoff", entity="message", entity_id=msg.id, channel=msg.channel)
        return DispatchResult("MANUAL_PENDING", None, "ready in the handoff queue",
                              {"profile_url": profile_url(msg.channel, msg.to_address or "")})

    # 1: dry run
    if not cfg.send_enabled:
        audit(session, "dispatch.dry_run", entity="message", entity_id=msg.id, to=msg.to_address)
        return DispatchResult("DRY_RUN", None, "SEND_ENABLED=false: all checks passed, nothing sent")

    # 5: email via Gmail
    if mailer is None:
        return DispatchResult("BLOCKED", "GMAIL_NOT_CONNECTED", "connect Gmail in Settings first")
    from app.services.gmail import build_mime
    try:
        in_reply_to = refs = None
        if msg.thread_id:
            last_in = session.exec(select(Interaction).where(Interaction.lead_id == lead.id, Interaction.direction == "in")
                                   .order_by(Interaction.occurred_at.desc())).first()
            in_reply_to = last_in.raw_ref if last_in and last_in.raw_ref and last_in.raw_ref.startswith("<") else None
        raw, _ = build_mime(cfg=cfg, from_email=mailer.account_email or cfg.sender_email, to=msg.to_address,
                            subject=msg.subject or "", body=msg.body or "", in_reply_to=in_reply_to, references=refs)
        provider_id, thread_id = mailer.send(raw, thread_id=msg.thread_id)
    except Exception as e:  # noqa: BLE001
        msg.status, msg.error = MessageStatus.FAILED, str(e)[:500]
        session.add(msg)
        audit(session, "dispatch.failed", entity="message", entity_id=msg.id, error=str(e)[:200])
        return DispatchResult("FAILED", "SEND_FAILED", str(e)[:200])
    mark_sent(session, msg, now=now, cfg=cfg, provider_message_id=provider_id, thread_id=thread_id)
    audit(session, "dispatch.sent", entity="message", entity_id=msg.id, provider_message_id=provider_id)
    return DispatchResult("SENT", None, "sent", {"provider_message_id": provider_id})


def mark_sent(session: Session, msg: Message, *, now: datetime, cfg: Settings, provider_message_id: str | None = None,
              thread_id: str | None = None, manual: bool = False) -> None:
    lead = session.get(Lead, msg.lead_id)
    msg.status = MessageStatus.SENT_MANUAL if manual else MessageStatus.SENT
    msg.sent_at = now
    msg.scheduled_for = None
    msg.provider_message_id = provider_message_id or msg.provider_message_id
    msg.thread_id = thread_id or msg.thread_id
    session.add(msg)
    session.add(Interaction(lead_id=lead.id, channel=msg.channel, direction="out", body=msg.body, occurred_at=now,
                            raw_ref=provider_message_id))
    if msg.kind == "initial":
        apply_org_lock(session, lead, now, cfg)
        if lead.status in (LeadStatus.APPROVAL_PENDING, LeadStatus.MESSAGE_READY, LeadStatus.QUALIFIED):
            set_status(session, lead, LeadStatus.SENT)


def mark_sent_manually(session: Session, msg: Message, *, now: datetime | None = None, cfg: Settings | None = None,
                       actor: str = "admin") -> DispatchResult:
    cfg = cfg or get_settings()
    now = now or utcnow()
    if msg.status != MessageStatus.MANUAL_PENDING:
        return DispatchResult("BLOCKED", "INVALID_STATE", f"message is {msg.status}, not MANUAL_PENDING")
    lead = session.get(Lead, msg.lead_id)
    reasons = lead_suppression_reasons(session, lead)
    if reasons:  # opted out between approval and the human sending
        msg.status, msg.error = MessageStatus.CANCELLED, "suppressed before manual send"
        session.add(msg)
        return DispatchResult("BLOCKED", "SUPPRESSED", "; ".join(reasons))
    mark_sent(session, msg, now=now, cfg=cfg, manual=True)
    audit(session, "message.mark_sent_manually", actor=actor, entity="message", entity_id=msg.id, channel=msg.channel)
    return DispatchResult("SENT", None, "marked as sent")


def reject_message(session: Session, msg: Message, *, reason: str = "", actor: str = "admin") -> None:
    if msg.status in (MessageStatus.SENT, MessageStatus.SENT_MANUAL):
        raise ValueError("cannot reject a sent message")
    msg.status, msg.error = MessageStatus.REJECTED, reason or None
    session.add(msg)
    lead = session.get(Lead, msg.lead_id)
    if msg.kind == "initial" and lead.status == LeadStatus.APPROVAL_PENDING:
        set_status(session, lead, LeadStatus.QUALIFIED, actor=actor, reason="draft rejected")
    audit(session, "message.reject", actor=actor, entity="message", entity_id=msg.id, reason=reason)


def dispatch_due(session: Session, *, mailer=None, now: datetime | None = None, cfg: Settings | None = None) -> dict:  # noqa: ANN001
    cfg = cfg or get_settings()
    now = now or utcnow()
    due = session.exec(select(Message).where(Message.status == MessageStatus.SCHEDULED, Message.scheduled_for <= now)
                       .order_by(Message.scheduled_for)).all()
    if not due:
        return {}
    if mailer is None and any(m.channel == EMAIL_CHANNEL for m in due) and cfg.send_enabled:
        from app.services.gmail import load_gmail
        mailer = load_gmail(session)
    out: dict[str, int] = {}
    for m in due:
        res = dispatch(session, m, mailer=mailer, now=now, cfg=cfg)
        out[res.status] = out.get(res.status, 0) + 1
        session.commit()
        if res.status == "SCHEDULED" and "cap" in res.detail:
            break  # caps hit: stop for this run
    return out
