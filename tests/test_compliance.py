from datetime import datetime, timedelta

from app.models import Interaction, Lead, Message, MessageStatus, Organization
from app.services.compliance import (apply_org_lock, cap_reasons, in_send_window, is_eu_uk, next_window_start,
                                     org_lock_status)

# 2026-11-09 05:30 UTC == 11:00 IST
IST_11AM = datetime(2026, 11, 9, 5, 30)


def test_send_window_ist():
    assert in_send_window(IST_11AM, "Asia/Kolkata", "10:00-18:00")
    assert not in_send_window(datetime(2026, 11, 9, 3, 0), "Asia/Kolkata", "10:00-18:00")  # 08:30 IST
    assert not in_send_window(datetime(2026, 11, 9, 12, 30), "Asia/Kolkata", "10:00-18:00")  # 18:00 IST end excl.


def test_next_window_start():
    early = datetime(2026, 11, 9, 3, 0)  # 08:30 IST
    assert next_window_start(early, "Asia/Kolkata", "10:00-18:00") == datetime(2026, 11, 9, 4, 30)
    late = datetime(2026, 11, 9, 14, 0)  # 19:30 IST
    assert next_window_start(late, "Asia/Kolkata", "10:00-18:00") == datetime(2026, 11, 10, 4, 30)
    assert next_window_start(IST_11AM, "Asia/Kolkata", "10:00-18:00") == IST_11AM


def _sent(session, lead, at, channel="email"):
    session.add(Message(lead_id=lead.id, channel=channel, status=MessageStatus.SENT, sent_at=at, approved_at=at))


def test_daily_cap(session, make_lead, cfg):
    cfg.daily_cap_email, cfg.hourly_cap_email, cfg.min_gap_seconds_email = 3, 100, 0
    lead = make_lead()
    for h in range(3):
        _sent(session, lead, IST_11AM - timedelta(hours=h + 2))  # same IST day (after 05:30 IST)
    session.commit()
    assert any("daily" in r for r in cap_reasons(session, "email", IST_11AM, cfg))


def test_daily_cap_resets_at_local_midnight(session, make_lead, cfg):
    cfg.daily_cap_email, cfg.hourly_cap_email, cfg.min_gap_seconds_email = 1, 100, 0
    lead = make_lead()
    _sent(session, lead, datetime(2026, 11, 8, 18, 0))  # 23:30 IST previous day
    session.commit()
    assert cap_reasons(session, "email", IST_11AM, cfg) == []


def test_hourly_cap_and_min_gap(session, make_lead, cfg):
    cfg.daily_cap_email, cfg.hourly_cap_email, cfg.min_gap_seconds_email = 100, 2, 300
    lead = make_lead()
    _sent(session, lead, IST_11AM - timedelta(minutes=50))
    _sent(session, lead, IST_11AM - timedelta(minutes=2))
    session.commit()
    reasons = cap_reasons(session, "email", IST_11AM, cfg)
    assert any("hourly" in r for r in reasons) and any("gap" in r for r in reasons)


def test_handoff_cap_counts_released_messages(session, make_lead, cfg):
    cfg.daily_cap_handoff = 1
    lead = make_lead()
    session.add(Message(lead_id=lead.id, channel="instagram", status=MessageStatus.MANUAL_PENDING,
                        approved_at=IST_11AM - timedelta(hours=1)))
    session.commit()
    assert cap_reasons(session, "linkedin", IST_11AM, cfg)
    assert cap_reasons(session, "email", IST_11AM, cfg) == []


def test_org_lock_blocks_second_person(session, make_lead, cfg):
    a = make_lead()
    b = Lead(organization_id=a.organization_id, status="QUALIFIED")
    session.add(b)
    session.commit()
    apply_org_lock(session, a, IST_11AM - timedelta(days=12), cfg)
    session.add(Interaction(lead_id=a.id, channel="email", direction="out", occurred_at=IST_11AM - timedelta(days=12)))
    session.commit()

    res = org_lock_status(session, b, "initial", IST_11AM, cfg)
    assert res.locked and "12 days" in res.detail
    # follow-up to the lock holder is fine, to someone else is not
    assert not org_lock_status(session, a, "followup", IST_11AM, cfg).locked
    assert org_lock_status(session, b, "followup", IST_11AM, cfg).locked


def test_org_lock_expires_and_override(session, make_lead, cfg):
    a = make_lead()
    apply_org_lock(session, a, IST_11AM - timedelta(days=91), cfg)
    session.commit()
    assert not org_lock_status(session, a, "initial", IST_11AM, cfg).locked
    apply_org_lock(session, a, IST_11AM - timedelta(days=5), cfg)
    org = session.get(Organization, a.organization_id)
    org.lock_override = True
    session.commit()
    assert not org_lock_status(session, a, "initial", IST_11AM, cfg).locked


def test_cross_channel_interaction_blocks_initial(session, make_lead, cfg):
    a = make_lead()
    session.add(Interaction(lead_id=a.id, channel="instagram", direction="out", occurred_at=IST_11AM - timedelta(days=3)))
    session.commit()
    res = org_lock_status(session, a, "initial", IST_11AM, cfg)
    assert res.locked and "instagram" in res.detail


def test_eu_uk_detection():
    assert is_eu_uk("GB") and is_eu_uk("Germany") and is_eu_uk(None, "info@clinic.de")
    assert is_eu_uk(None, "studio.co.uk")
    assert not is_eu_uk("IN", "hello@glowskin.in")
    assert not is_eu_uk("US", "a@b.com")
