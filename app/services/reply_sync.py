"""Gmail reply poller: reads only threads the app started (every 5 minutes via the scheduler)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.llm import LLM
from app.models import EMAIL_CHANNEL, Lead, Message, MessageStatus, utcnow
from app.services.gmail import extract_plain_text, header
from app.services.replies import handle_inbound

log = logging.getLogger(__name__)


def sync_gmail_replies(session: Session, *, mailer=None, llm: LLM | None = None, cfg: Settings | None = None,  # noqa: ANN001
                       lookback_days: int = 60) -> dict:
    cfg = cfg or get_settings()
    if mailer is None:
        from app.services.gmail import load_gmail
        mailer = load_gmail(session)
        if mailer is None:
            return {}
    if llm is None and cfg.anthropic_api_key:
        llm = LLM(session, cfg=cfg)
    since = utcnow() - timedelta(days=lookback_days)
    threads: dict[str, int] = {}
    for m in session.exec(select(Message).where(Message.channel == EMAIL_CHANNEL, Message.thread_id.is_not(None),
                                                Message.status == MessageStatus.SENT, Message.sent_at >= since)):
        threads[m.thread_id] = m.lead_id
    me = (mailer.account_email or cfg.sender_email or "").lower()
    stats = {"threads": len(threads), "new_replies": 0}
    for thread_id, lead_id in threads.items():
        try:
            msgs = mailer.thread_messages(thread_id)
        except Exception:  # noqa: BLE001
            log.exception("could not read thread %s", thread_id)
            continue
        for gm in msgs:
            sender = (header(gm, "From") or "").lower()
            if me and me in sender:
                continue
            msg_id = header(gm, "Message-ID") or f"gmail:{gm.get('id')}"
            lead = session.get(Lead, lead_id)
            ts = datetime.fromtimestamp(int(gm.get("internalDate", "0")) / 1000, tz=timezone.utc).replace(tzinfo=None) \
                if gm.get("internalDate") else utcnow()
            res = handle_inbound(session, lead, channel=EMAIL_CHANNEL, text=extract_plain_text(gm.get("payload", {})),
                                 raw_ref=msg_id, occurred_at=ts, llm=llm, cfg=cfg)
            if not res.get("duplicate"):
                stats["new_replies"] += 1
            session.commit()
    return stats if stats["new_replies"] else {}
