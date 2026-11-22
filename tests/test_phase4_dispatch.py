import base64
import email
import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.llm import LLM
from app.models import AuditLog, Integration, Interaction, Lead, Message, MessageStatus, Organization
from app.services.dispatcher import approve_message, dispatch_due, mark_sent_manually
from app.services.gmail import build_mime, extract_plain_text, save_credentials
from app.services.replies import handle_inbound
from app.services.reply_sync import sync_gmail_replies
from app.services.suppression import add_suppression, is_suppressed
from tests.helpers import PORTFOLIO_URL, good_claims, good_email_body, setup_medspa_lead

IST_11AM = datetime(2026, 11, 9, 5, 30)


class FakeMailer:
    account_email = "asha@example.com"

    def __init__(self):
        self.sent = []
        self.threads = {}

    def send(self, raw_b64, thread_id=None):
        self.sent.append((raw_b64, thread_id))
        n = len(self.sent)
        return f"gm{n}", thread_id or f"th{n}"

    def thread_messages(self, thread_id):
        return self.threads.get(thread_id, [])


def _approvable(session, lead, e1, e2, channel="email", to="hello@glowskin.in"):
    body = good_email_body()
    d = json.dumps({"subject": "Reels for the Baner launch", "body": body, "claims": good_claims(e1, e2)})
    v = {"a": {"ok": True}, "b": {"ok": True}, "selected": "a"}
    m = Message(lead_id=lead.id, channel=channel, kind="initial", draft_a=d, draft_b=d, body=body,
                subject="Reels for the Baner launch" if channel == "email" else None, to_address=to,
                status=MessageStatus.APPROVAL_PENDING, validation_json=json.dumps(v))
    lead.status = "APPROVAL_PENDING"
    session.add_all([m, lead])
    session.commit()
    return m


@pytest.fixture
def ready(session, make_lead):
    lead, e1, e2 = setup_medspa_lead(session, make_lead)
    return lead, e1, e2


def test_dry_run_runs_all_checks_but_sends_nothing(session, ready, cfg):
    lead, e1, e2 = ready
    m = _approvable(session, lead, e1, e2)
    mailer = FakeMailer()
    res = approve_message(session, m, mailer=mailer, now=IST_11AM, cfg=cfg)
    assert res.status == "DRY_RUN" and mailer.sent == [] and m.status == MessageStatus.APPROVED
    assert session.exec(select(AuditLog).where(AuditLog.action == "dispatch.dry_run")).first()


def test_live_send_headers_footer_lock_and_status(session, ready, cfg):
    lead, e1, e2 = ready
    cfg.send_enabled = True
    m = _approvable(session, lead, e1, e2)
    mailer = FakeMailer()
    res = approve_message(session, m, mailer=mailer, now=IST_11AM, cfg=cfg)
    session.commit()
    assert res.status == "SENT" and m.provider_message_id == "gm1" and m.thread_id == "th1"
    raw = email.message_from_bytes(base64.urlsafe_b64decode(mailer.sent[0][0]))
    assert raw["List-Unsubscribe"] == "<mailto:asha@example.com?subject=unsubscribe>"
    body = raw.get_payload(decode=True).decode()
    assert PORTFOLIO_URL in body and 'reply "stop"' in body and cfg.sender_contact_address in body
    assert session.get(Lead, lead.id).status == "SENT"
    org = session.get(Organization, lead.organization_id)
    assert org.locked_by_lead_id == lead.id and org.contact_locked_until > IST_11AM
    assert session.exec(select(Interaction).where(Interaction.direction == "out")).one()


def test_suppressed_recipient_blocks_approval(session, ready, cfg):
    lead, e1, e2 = ready
    m = _approvable(session, lead, e1, e2)
    add_suppression(session, "domain", "glowskin.in", "test")
    session.commit()
    res = approve_message(session, m, now=IST_11AM, cfg=cfg)
    assert res.status == "BLOCKED" and res.code == "SUPPRESSED" and m.status == MessageStatus.APPROVAL_PENDING


def test_org_lock_returns_409_shape(client, session, ready):
    lead, e1, e2 = ready
    other = Lead(organization_id=lead.organization_id, status="QUALIFIED")
    session.add(other)
    session.commit()
    session.add(Interaction(lead_id=other.id, channel="email", direction="out",
                            occurred_at=datetime.utcnow() - timedelta(days=12)))
    session.commit()
    m = _approvable(session, lead, e1, e2)
    r = client.post(f"/api/v1/messages/{m.id}/approve")
    assert r.status_code == 409
    j = r.json()
    assert j["error"] == "ORG_LOCKED" and "12 days ago" in j["detail"] and j["locked_until"]


def test_outside_window_is_scheduled_then_sent_by_job(session, ready, cfg):
    lead, e1, e2 = ready
    cfg.send_enabled = True
    m = _approvable(session, lead, e1, e2)
    night = datetime(2026, 11, 9, 16, 0)  # 21:30 IST
    res = approve_message(session, m, mailer=FakeMailer(), now=night, cfg=cfg)
    assert res.status == "SCHEDULED" and m.scheduled_for == datetime(2026, 11, 10, 4, 30)
    session.commit()
    mailer = FakeMailer()
    assert dispatch_due(session, mailer=mailer, now=datetime(2026, 11, 10, 4, 31), cfg=cfg) == {"SENT": 1}
    assert m.status == MessageStatus.SENT


def test_daily_cap_defers_to_next_day(session, make_lead, cfg):
    cfg.send_enabled, cfg.daily_cap_email, cfg.min_gap_seconds_email = True, 1, 0
    lead, e1, e2 = setup_medspa_lead(session, make_lead)
    m1 = _approvable(session, lead, e1, e2)
    approve_message(session, m1, mailer=FakeMailer(), now=IST_11AM, cfg=cfg)
    lead2 = make_lead(name="Other", domain="other.in", email="hi@other.in", niche_id=lead.niche_id, status="QUALIFIED")
    m2 = _approvable(session, lead2, e1, e2, to="hi@other.in")
    m2.validation_json = json.dumps({"a": {"ok": True}, "selected": "a"})
    res = approve_message(session, m2, mailer=FakeMailer(), now=IST_11AM + timedelta(minutes=5), cfg=cfg)
    assert res.status == "SCHEDULED" and "daily" in res.detail
    assert m2.scheduled_for == datetime(2026, 11, 10, 4, 30)


def test_send_rechecks_suppression_at_send_time(session, ready, cfg):
    lead, e1, e2 = ready
    cfg.send_enabled = True
    m = _approvable(session, lead, e1, e2)
    approve_message(session, m, mailer=FakeMailer(), now=datetime(2026, 11, 9, 16, 0), cfg=cfg)  # scheduled
    add_suppression(session, "email", "hello@glowskin.in", "late opt-out")
    session.commit()
    mailer = FakeMailer()
    dispatch_due(session, mailer=mailer, now=datetime(2026, 11, 10, 5, 0), cfg=cfg)
    assert mailer.sent == [] and m.status == MessageStatus.FAILED and "SUPPRESSED" in m.error


def test_handoff_flow(session, ready, cfg):
    lead, e1, e2 = ready
    m = _approvable(session, lead, e1, e2, channel="instagram", to="glowskin.pune")
    res = approve_message(session, m, now=IST_11AM, cfg=cfg)
    assert res.status == "MANUAL_PENDING" and res.extra["profile_url"] == "https://www.instagram.com/glowskin.pune/"
    out = mark_sent_manually(session, m, now=IST_11AM, cfg=cfg)
    assert out.status == "SENT" and m.status == MessageStatus.SENT_MANUAL
    assert session.get(Lead, lead.id).status == "SENT"
    assert session.exec(select(Interaction).where(Interaction.channel == "instagram")).one().direction == "out"


def test_eu_uk_blocked_at_dispatch(session, ready, cfg):
    lead, e1, e2 = ready
    org = session.get(Organization, lead.organization_id)
    org.country = "GB"
    session.commit()
    m = _approvable(session, lead, e1, e2)
    assert approve_message(session, m, now=IST_11AM, cfg=cfg).code == "EU_UK_EXCLUDED"


# ---------- replies ----------

def _sent_lead(session, ready, cfg):
    lead, e1, e2 = ready
    cfg.send_enabled = True
    m = _approvable(session, lead, e1, e2)
    approve_message(session, m, mailer=FakeMailer(), now=IST_11AM, cfg=cfg)
    fu = Message(lead_id=lead.id, channel="email", kind="followup", followup_number=1, status=MessageStatus.SCHEDULED,
                 scheduled_for=IST_11AM + timedelta(days=4), body="x")
    session.add(fu)
    session.commit()
    return lead, m, fu


def test_keyword_optout_suppresses_before_llm(session, ready, cfg, fake_llm):
    lead, m, fu = _sent_lead(session, ready, cfg)
    res = handle_inbound(session, lead, channel="email", text="please remove me", llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert res["classification"] == "UNSUBSCRIBE" and res["method"].startswith("keyword")
    assert fake_llm.requests == []
    assert lead.status == "DO_NOT_CONTACT" and is_suppressed(session, "domain", "glowskin.in")
    assert fu.status == MessageStatus.CANCELLED


def test_hinglish_not_interested(session, ready, cfg):
    lead, *_ = _sent_lead(session, ready, cfg)
    res = handle_inbound(session, lead, channel="email", text="bhai abhi interest nahi hai", llm=None, cfg=cfg)
    assert res["classification"] == "NOT_INTERESTED" and lead.status == "NOT_INTERESTED"
    assert is_suppressed(session, "email", "hello@glowskin.in")


def test_llm_classified_optout_also_suppresses(session, ready, cfg, fake_llm):
    lead, *_ = _sent_lead(session, ready, cfg)
    fake_llm.queue({"label": "UNSUBSCRIBE", "language": "mr", "confidence": 0.9})
    res = handle_inbound(session, lead, channel="email", text="कृपया पुन्हा संदेश पाठवू नये",
                         llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert res["method"] == "llm" and lead.status == "DO_NOT_CONTACT"
    assert fake_llm.requests[0]["model"] == cfg.model_classify


def test_pricing_reply_without_settings_asks_user(session, ready, cfg, fake_llm):
    lead, *_ = _sent_lead(session, ready, cfg)
    fake_llm.queue({"label": "PRICING", "language": "en", "confidence": 0.9},
                   {"classification": "PRICING", "body": "My rate is INR 2,500 per reel. Want to start?", "needs_user_input": []})
    res = handle_inbound(session, lead, channel="email", text="What do you charge per reel?",
                         llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    reply = session.get(Message, res["reply_message_id"])
    v = json.loads(reply.validation_json)
    assert reply.status == MessageStatus.DRAFT and reply.body is None
    assert any("not in settings" in n for n in v["needs_user_input"])
    assert lead.status == "INTERESTED" and res["followups_cancelled"] == 1


def test_pricing_reply_with_settings(session, ready, cfg, fake_llm):
    from app.services.settings_store import set_setting
    set_setting(session, "packages", [{"name": "Starter", "description": "8 reels/month, 48h turnaround, 1 revision",
                                       "price": "INR 12,000"}])
    lead, *_ = _sent_lead(session, ready, cfg)
    fake_llm.queue({"label": "PRICING", "language": "en", "confidence": 0.9},
                   {"classification": "PRICING", "needs_user_input": [],
                    "body": "Thanks for asking! My Starter package is 8 reels a month with 48h turnaround and one "
                            "revision, for INR 12,000. Happy to tailor it if you need more or fewer reels."})
    res = handle_inbound(session, lead, channel="email", text="pricing?", llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    reply = session.get(Message, res["reply_message_id"])
    assert reply.status == MessageStatus.APPROVAL_PENDING, reply.validation_json
    assert reply.subject == "Re: Reels for the Baner launch" and reply.thread_id == "th1"


def test_gmail_reply_sync(session, ready, cfg, fake_llm):
    lead, m, fu = _sent_lead(session, ready, cfg)
    mailer = FakeMailer()
    body = base64.urlsafe_b64encode(b"Stop emailing me.\n\nOn Mon, Asha wrote:\n> hi").decode()
    mailer.threads["th1"] = [
        {"id": "gm1", "payload": {"headers": [{"name": "From", "value": "Asha <asha@example.com>"}]}},
        {"id": "r1", "internalDate": "1762700000000",
         "payload": {"mimeType": "text/plain", "body": {"data": body},
                     "headers": [{"name": "From", "value": "Meera <hello@glowskin.in>"},
                                 {"name": "Message-ID", "value": "<abc@glowskin.in>"}]}}]
    stats = sync_gmail_replies(session, mailer=mailer, llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert stats["new_replies"] == 1 and lead.status == "DO_NOT_CONTACT"
    assert sync_gmail_replies(session, mailer=mailer, llm=None, cfg=cfg) == {}  # dedup by Message-ID


def test_build_mime_requires_sender_identity(cfg):
    cfg.sender_contact_address = ""
    with pytest.raises(ValueError):
        build_mime(cfg=cfg, from_email="a@b.com", to="c@d.com", subject="s", body="b")


def test_extract_plain_text_html_fallback():
    html = base64.urlsafe_b64encode(b"<p>Hello <b>there</b></p>").decode()
    assert "Hello" in extract_plain_text({"mimeType": "text/html", "body": {"data": html}})


def test_oauth_token_encrypted_at_rest(session):
    save_credentials(session, '{"refresh_token": "1//secret-refresh"}', "asha@example.com")
    session.commit()
    row = session.exec(select(Integration)).one()
    assert "secret-refresh" not in row.token_encrypted


def test_manual_reply_api_and_handoff_api(client, session, ready, fake_llm):
    from app.api.deps import get_llm
    lead, e1, e2 = ready
    m = _approvable(session, lead, e1, e2, channel="instagram", to="glowskin.pune")
    assert client.post(f"/api/v1/messages/{m.id}/approve").json()["status"] == "MANUAL_PENDING"
    q = client.get("/api/v1/handoff").json()
    assert q[0]["profile_url"] == "https://www.instagram.com/glowskin.pune/"
    assert client.post(f"/api/v1/messages/{m.id}/mark-sent-manually").json()["status"] == "SENT"
    client.app.dependency_overrides[get_llm] = lambda: LLM(session, client=fake_llm)
    r = client.post("/api/v1/interactions/manual", json={"lead_id": lead.id, "channel": "instagram", "body": "STOP"})
    assert r.json()["classification"] == "UNSUBSCRIBE"
    client.app.dependency_overrides.clear()
    assert client.get("/api/v1/inbox").json()[0]["classification"] == "UNSUBSCRIBE"
