"""Instagram / Messenger INBOUND automation (Phase 6, needs Meta app review).

Only people who messaged the user's own Page/Instagram account first are handled, and replies go
out only inside Meta's 24-hour standard messaging window. Nothing here initiates contact.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from app.audit import audit
from app.config import Settings, get_settings
from app.llm import LLM
from app.models import ContactPoint, Interaction, Lead, LeadStatus, Message, utcnow
from app.services.leads import add_contact_point, create_lead
from app.services.replies import handle_inbound

WINDOW = timedelta(hours=24)


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def _psid_type(obj: str) -> tuple[str, str]:
    return ("ig_psid", "instagram") if obj == "instagram" else ("fb_psid", "facebook")


def lead_for_psid(session: Session, psid_type: str, psid: str, channel: str) -> Lead:
    cp = session.exec(select(ContactPoint).where(ContactPoint.type == psid_type, ContactPoint.value_norm == psid)).first()
    if cp:
        return session.get(Lead, cp.lead_id)
    res = create_lead(session, name=f"{channel.title()} inbound {psid[-6:]}", source="meta_inbound",
                      intent_signal="Messaged first via " + channel)
    lead = res.lead
    lead.status = LeadStatus.REPLIED
    session.add(lead)
    add_contact_point(session, lead, psid_type, psid, None)
    return lead


def process_webhook(session: Session, payload: dict, *, llm: LLM | None = None, cfg: Settings | None = None) -> dict:
    cfg = cfg or get_settings()
    obj = payload.get("object", "")
    if obj not in ("instagram", "page"):
        return {"ignored": obj}
    psid_type, channel = _psid_type(obj)
    handled = 0
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []):
            msg = ev.get("message") or {}
            if not msg or msg.get("is_echo"):
                continue
            psid = str((ev.get("sender") or {}).get("id", ""))
            text = msg.get("text") or ("[attachment]" if msg.get("attachments") else "")
            if not psid.isdigit() or not text:
                continue
            ts = datetime.utcfromtimestamp(ev["timestamp"] / 1000) if ev.get("timestamp") else utcnow()
            lead = lead_for_psid(session, psid_type, psid, channel)
            handle_inbound(session, lead, channel=channel, text=text, raw_ref=f"meta:{msg.get('mid', '')}",
                           occurred_at=ts, llm=llm, cfg=cfg)
            # reply drafts for inbound DMs address the PSID
            for m in session.exec(select(Message).where(Message.lead_id == lead.id, Message.kind == "reply",
                                                        Message.to_address.is_(None))):
                m.to_address = psid
                session.add(m)
            handled += 1
    audit(session, "meta.webhook", count=handled, object=obj)
    return {"handled": handled}


def within_window(session: Session, lead: Lead, now: datetime) -> bool:
    last_in = session.exec(select(Interaction.occurred_at).where(Interaction.lead_id == lead.id,
                                                                 Interaction.direction == "in",
                                                                 Interaction.channel.in_(["instagram", "facebook"]))
                           .order_by(Interaction.occurred_at.desc())).first()
    return bool(last_in and now - last_in <= WINDOW)


def can_auto_send(session: Session, msg: Message, cfg: Settings) -> bool:
    if not (cfg.meta_inbound_enabled and cfg.meta_page_access_token) or msg.kind != "reply":
        return False
    if msg.channel not in ("instagram", "facebook"):
        return False
    t = "ig_psid" if msg.channel == "instagram" else "fb_psid"
    return session.exec(select(ContactPoint.id).where(ContactPoint.lead_id == msg.lead_id, ContactPoint.type == t,
                                                      ContactPoint.value_norm == (msg.to_address or ""))).first() is not None


def send_reply(msg: Message, cfg: Settings, client: httpx.Client | None = None) -> str:
    http = client or httpx.Client(timeout=20.0)
    url = f"https://graph.facebook.com/{cfg.meta_graph_version}/me/messages"
    r = http.post(url, params={"access_token": cfg.meta_page_access_token},
                  json={"recipient": {"id": msg.to_address}, "messaging_type": "RESPONSE", "message": {"text": msg.body}})
    r.raise_for_status()
    return r.json().get("message_id", "")
