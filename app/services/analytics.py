"""Funnel analytics: sent -> replied -> positive -> calls -> paid, grouped by niche/channel/source."""
from __future__ import annotations

from collections import defaultdict

from sqlmodel import Session, select

from app.models import Interaction, Lead, LeadStatus, Message, MessageStatus, Niche

POSITIVE_LABELS = {"SEND_WORK", "PRICING", "PACKAGE", "INTERESTED"}
MEETING_STATES = {LeadStatus.MEETING_BOOKED, LeadStatus.CLIENT}


def funnel(session: Session, group_by: str = "niche") -> dict:
    niches = {n.id: n.slug for n in session.exec(select(Niche))}
    first_channel: dict[int, str] = {}
    sent_leads: set[int] = set()
    for m in session.exec(select(Message).where(Message.kind == "initial",
                                                Message.status.in_([MessageStatus.SENT, MessageStatus.SENT_MANUAL]))
                          .order_by(Message.sent_at)):
        sent_leads.add(m.lead_id)
        first_channel.setdefault(m.lead_id, m.channel)
    replied, positive = set(), set()
    for i in session.exec(select(Interaction).where(Interaction.direction == "in")):
        replied.add(i.lead_id)
        if i.classification in POSITIVE_LABELS:
            positive.add(i.lead_id)

    rows: dict[str, dict[str, int]] = defaultdict(lambda: {"leads": 0, "sent": 0, "replied": 0, "positive": 0,
                                                           "calls": 0, "paid": 0})
    for lead in session.exec(select(Lead)):
        if group_by == "niche":
            key = niches.get(lead.niche_id, "none")
        elif group_by == "channel":
            key = first_channel.get(lead.id, "not_sent")
        else:
            key = lead.source or "unknown"
        r = rows[key]
        r["leads"] += 1
        if lead.id in sent_leads:
            r["sent"] += 1
            r["replied"] += lead.id in replied
            r["positive"] += lead.id in positive or lead.status in (LeadStatus.INTERESTED, *MEETING_STATES)
            r["calls"] += lead.status in MEETING_STATES
            r["paid"] += lead.status == LeadStatus.CLIENT
    total = {k: sum(r[k] for r in rows.values()) for k in ("leads", "sent", "replied", "positive", "calls", "paid")}
    for r in [*rows.values(), total]:
        r["reply_rate"] = round(r["replied"] / r["sent"], 3) if r["sent"] else 0.0
        r["positive_rate"] = round(r["positive"] / r["sent"], 3) if r["sent"] else 0.0
    return {"group_by": group_by, "groups": dict(rows), "total": total}
