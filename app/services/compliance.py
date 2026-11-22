"""Plain-code compliance checks: send window, caps, organisation lock, EU/UK gate.

Nothing in this module calls an LLM (rule 5). Every function takes `now` explicitly so
it is deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, func, select

from app.config import Settings, parse_window
from app.models import (EMAIL_CHANNEL, HANDOFF_CHANNELS, Interaction, Lead, Message, MessageStatus,
                        Organization)

EU_UK_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",  # EU27
    "IS", "LI", "NO",  # EEA (GDPR applies)
    "GB", "UK",
}
EU_UK_TLDS = {c.lower() for c in EU_UK_COUNTRIES} | {"eu"}
COUNTRY_NAMES = {"united kingdom": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
                 "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT", "ireland": "IE",
                 "netherlands": "NL", "india": "IN", "united states": "US", "usa": "US",
                 "united arab emirates": "AE", "uae": "AE", "canada": "CA", "australia": "AU"}


def to_country_code(country: str | None) -> str | None:
    if not country:
        return None
    c = country.strip()
    if len(c) == 2:
        return c.upper()
    return COUNTRY_NAMES.get(c.lower())


def is_eu_uk(country: str | None, domain_or_email: str | None = None) -> bool:
    code = to_country_code(country)
    if code and code in EU_UK_COUNTRIES:
        return True
    if domain_or_email:
        host = domain_or_email.split("@")[-1].lower().rstrip(".")
        tld = host.rsplit(".", 1)[-1]
        if tld in EU_UK_TLDS:
            return True
    return False


# ---------------- send window ----------------

def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def in_send_window(now: datetime, tz: str, window: str) -> bool:
    start, end = parse_window(window)
    local = _utc(now).astimezone(ZoneInfo(tz))
    return start <= local.time() < end


def next_window_start(now: datetime, tz: str, window: str) -> datetime:
    """Earliest naive-UTC moment >= now that is inside the window."""
    if in_send_window(now, tz, window):
        return _utc(now).replace(tzinfo=None)
    start, _ = parse_window(window)
    zone = ZoneInfo(tz)
    local = _utc(now).astimezone(zone)
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def local_day_start_utc(now: datetime, tz: str) -> datetime:
    local = _utc(now).astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------- caps ----------------

SENT_STATES = (MessageStatus.SENT, MessageStatus.SENT_MANUAL, MessageStatus.MANUAL_PENDING)


def _channel_filter(channel: str):
    if channel == EMAIL_CHANNEL:
        return Message.channel == EMAIL_CHANNEL
    return Message.channel.in_(HANDOFF_CHANNELS)


def _released_at():
    # a handoff message counts against the cap from the moment it is released to the human
    return func.coalesce(Message.sent_at, Message.approved_at)


def sent_count_since(session: Session, channel: str, since: datetime) -> int:
    q = select(func.count(Message.id)).where(_channel_filter(channel), Message.status.in_(SENT_STATES),
                                            _released_at() >= since)
    return session.exec(q).one()


def cap_reasons(session: Session, channel: str, now: datetime, cfg: Settings) -> list[str]:
    now = _utc(now).replace(tzinfo=None)
    reasons: list[str] = []
    day_start = local_day_start_utc(now, cfg.timezone)
    if channel == EMAIL_CHANNEL:
        if sent_count_since(session, channel, day_start) >= cfg.daily_cap_email:
            reasons.append(f"daily email cap {cfg.daily_cap_email} reached")
        if sent_count_since(session, channel, now - timedelta(hours=1)) >= cfg.hourly_cap_email:
            reasons.append(f"hourly email cap {cfg.hourly_cap_email} reached")
        last = session.exec(select(func.max(Message.sent_at)).where(
            Message.channel == EMAIL_CHANNEL, Message.status == MessageStatus.SENT)).one()
        if last and (now - last).total_seconds() < cfg.min_gap_seconds_email:
            reasons.append(f"min gap {cfg.min_gap_seconds_email}s between emails")
    else:
        if sent_count_since(session, channel, day_start) >= cfg.daily_cap_handoff:
            reasons.append(f"daily handoff cap {cfg.daily_cap_handoff} reached")
    return reasons


# ---------------- organisation lock ----------------

@dataclass
class LockResult:
    locked: bool
    detail: str = ""
    locked_until: datetime | None = None


def org_lock_status(session: Session, lead: Lead, kind: str, now: datetime, cfg: Settings) -> LockResult:
    """One person per organisation per CONTACT_LOCK_DAYS, across all channels.

    - initial messages are blocked if the org is locked or has *any* interaction in the cooldown.
    - follow-ups / replies are allowed only to the lead that holds the lock.
    - `lock_override` on the organisation (set by the user) bypasses both.
    """
    now = _utc(now).replace(tzinfo=None)
    org = session.get(Organization, lead.organization_id)
    if org is None or org.lock_override:
        return LockResult(False)
    cooldown_start = now - timedelta(days=cfg.contact_lock_days)
    lead_ids = [i for i in session.exec(select(Lead.id).where(Lead.organization_id == org.id))]
    recent = session.exec(
        select(Interaction).where(Interaction.lead_id.in_(lead_ids), Interaction.occurred_at >= cooldown_start)
        .order_by(Interaction.occurred_at.desc())).all()

    if kind == "initial":
        if org.contact_locked_until and org.contact_locked_until > now:
            days = (now - org.last_contacted_at).days if org.last_contacted_at else None
            return LockResult(True, f"Organisation contacted {days} days ago" if days is not None
                              else "Organisation is locked", org.contact_locked_until)
        if recent:
            last = recent[0]
            return LockResult(True, f"Interaction with organisation {(now - last.occurred_at).days} days ago "
                                    f"by {last.channel}", last.occurred_at + timedelta(days=cfg.contact_lock_days))
        return LockResult(False)

    # followup / reply
    if org.locked_by_lead_id and org.locked_by_lead_id != lead.id and \
            org.contact_locked_until and org.contact_locked_until > now:
        return LockResult(True, "Organisation lock is held by a different contact", org.contact_locked_until)
    others = [r for r in recent if r.lead_id != lead.id]
    if others:
        return LockResult(True, f"Recent interaction with another contact at this organisation via "
                                f"{others[0].channel}", others[0].occurred_at + timedelta(days=cfg.contact_lock_days))
    return LockResult(False)


def apply_org_lock(session: Session, lead: Lead, now: datetime, cfg: Settings) -> None:
    now = _utc(now).replace(tzinfo=None)
    org = session.get(Organization, lead.organization_id)
    if org is None:
        return
    org.last_contacted_at = now
    org.contact_locked_until = now + timedelta(days=cfg.contact_lock_days)
    org.locked_by_lead_id = lead.id
    session.add(org)
