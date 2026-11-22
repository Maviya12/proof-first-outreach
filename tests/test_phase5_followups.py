"""Phase 5 acceptance: follow-ups never fire after a reply or opt-out."""
import json
import re
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.llm import LLM
from app.models import Lead, Message, MessageStatus
from app.services.analytics import funnel
from app.services.dispatcher import approve_message, dispatch_due
from app.services.followups import followup_due, plan_followups
from app.services.replies import handle_inbound
from app.services.suppression import add_suppression
from tests.helpers import PORTFOLIO_URL, setup_medspa_lead
from tests.test_phase4_dispatch import FakeMailer, _approvable

T0 = datetime(2026, 11, 9, 5, 30)  # 11:00 IST


def followup_responder(req):
    content = req["messages"][0]["content"]
    if "review two outreach drafts" in req["system"][0]["text"]:
        return {"score_a": 8, "score_b": 7, "selected": "a", "reason": "fresh angle"}
    ids = [int(i) for i in re.findall(r"^\[(\d+)\]", content, re.M)]
    n = re.search(r"follow-up #(\d)", content).group(1)
    assert n == "1"
    body = (f"Hi Meera, one more idea since my last note. Your page says you run a monthly live Q&A. I could cut each "
            f"session into a few short captioned clips with a clear opening line, so the answers keep working for weeks "
            f"after the live ends and give you ready-made posts between sessions. I'd match your existing colours and "
            f"fonts so it all feels consistent. Recent clinic work is here: {PORTFOLIO_URL} If that sounds useful, "
            f"shall I outline how I'd cut the next one?")
    d = {"subject": "Q&A clips", "body": body,
         "claims": [{"text": "Your page says you run a monthly live Q&A", "evidence_ids": [ids[0]]}]}
    return {"draft_a": d, "draft_b": d}


@pytest.fixture
def sent_lead(session, make_lead, cfg):
    cfg.send_enabled = True
    lead, e1, e2 = setup_medspa_lead(session, make_lead)
    m = _approvable(session, lead, e1, e2)
    approve_message(session, m, mailer=FakeMailer(), now=T0, cfg=cfg)
    session.commit()
    assert m.status == MessageStatus.SENT
    return lead, m


def test_not_due_before_gap(session, sent_lead):
    lead, _ = sent_lead
    assert followup_due(session, lead, T0 + timedelta(days=3)) is None
    assert followup_due(session, lead, T0 + timedelta(days=4))[0] == 1


def test_followup_generated_threaded_and_approved(session, sent_lead, fake_llm, cfg):
    lead, initial = sent_lead
    fake_llm.queue(*[followup_responder] * 4)
    res = plan_followups(session, llm=LLM(session, client=fake_llm, cfg=cfg), now=T0 + timedelta(days=4), cfg=cfg)
    assert res["created"] == 1
    fu = session.exec(select(Message).where(Message.kind == "followup")).one()
    assert fu.status == MessageStatus.APPROVAL_PENDING, fu.validation_json
    assert fu.subject == "Re: Reels for the Baner launch" and fu.thread_id == initial.thread_id
    assert "follow-up #1" in fake_llm.requests[0]["messages"][0]["content"]
    # no second follow-up while the first is open
    assert plan_followups(session, llm=LLM(session, client=fake_llm, cfg=cfg), now=T0 + timedelta(days=20), cfg=cfg) == {}
    # approve + send in window
    mailer = FakeMailer()
    r = approve_message(session, fu, mailer=mailer, now=T0 + timedelta(days=4, minutes=10), cfg=cfg)
    assert r.status == "SENT" and mailer.sent[0][1] == initial.thread_id


def test_max_two_followups(session, sent_lead, fake_llm, cfg):
    lead, _ = sent_lead
    for i, day in enumerate([4, 14], start=1):
        session.add(Message(lead_id=lead.id, channel="email", kind="followup", followup_number=i,
                            status=MessageStatus.SENT, sent_at=T0 + timedelta(days=day), body=f"fu {i}"))
    session.commit()
    assert followup_due(session, lead, T0 + timedelta(days=60)) is None


def test_second_gap_counts_from_last_followup(session, sent_lead):
    lead, _ = sent_lead
    session.add(Message(lead_id=lead.id, channel="email", kind="followup", followup_number=1,
                        status=MessageStatus.SENT, sent_at=T0 + timedelta(days=5), body="fu 1"))
    session.commit()
    assert followup_due(session, lead, T0 + timedelta(days=13)) is None
    assert followup_due(session, lead, T0 + timedelta(days=14))[0] == 2


def test_never_after_reply(session, sent_lead, fake_llm, cfg):
    lead, _ = sent_lead
    handle_inbound(session, lead, channel="email", text="Thanks, will check and revert", llm=None, cfg=cfg, draft=False)
    session.commit()
    assert followup_due(session, lead, T0 + timedelta(days=30)) is None
    assert plan_followups(session, llm=LLM(session, client=fake_llm, cfg=cfg), now=T0 + timedelta(days=30), cfg=cfg) == {}
    assert fake_llm.requests == []


def test_never_after_optout(session, sent_lead, fake_llm, cfg):
    lead, _ = sent_lead
    add_suppression(session, "email", "hello@glowskin.in", "stop via phone call")
    session.commit()
    assert followup_due(session, lead, T0 + timedelta(days=30)) is None


def test_reply_after_approval_cancels_scheduled_followup(session, sent_lead, fake_llm, cfg):
    lead, _ = sent_lead
    fake_llm.queue(*[followup_responder] * 2)
    plan_followups(session, llm=LLM(session, client=fake_llm, cfg=cfg), now=T0 + timedelta(days=4), cfg=cfg)
    fu = session.exec(select(Message).where(Message.kind == "followup")).one()
    night = datetime(2026, 11, 13, 16, 0)  # outside window -> scheduled
    assert approve_message(session, fu, mailer=FakeMailer(), now=night, cfg=cfg).status == "SCHEDULED"
    session.commit()
    handle_inbound(session, lead, channel="instagram", text="Replied on Insta: yes interested!", llm=None, cfg=cfg, draft=False)
    session.commit()
    assert fu.status == MessageStatus.CANCELLED
    mailer = FakeMailer()
    dispatch_due(session, mailer=mailer, now=datetime(2026, 11, 14, 5, 0), cfg=cfg)
    assert mailer.sent == []


def test_dispatcher_blocks_followup_if_reply_slipped_in(session, sent_lead, cfg):
    lead, _ = sent_lead
    fu = Message(lead_id=lead.id, channel="email", kind="followup", followup_number=1, to_address="hello@glowskin.in",
                 body="x", subject="Re: x", status=MessageStatus.SCHEDULED, scheduled_for=T0 + timedelta(days=4))
    session.add(fu)
    from app.models import Interaction
    session.add(Interaction(lead_id=lead.id, channel="whatsapp", direction="in", body="call me",
                            occurred_at=T0 + timedelta(days=2)))
    session.commit()
    mailer = FakeMailer()
    dispatch_due(session, mailer=mailer, now=T0 + timedelta(days=4, hours=1), cfg=cfg)
    assert mailer.sent == [] and fu.status == MessageStatus.CANCELLED


def test_repeated_followup_text_rejected(session, sent_lead, fake_llm, cfg):
    lead, initial = sent_lead
    same = json.loads(initial.draft_a)
    same["subject"] = "again"
    fake_llm.queue({"draft_a": same, "draft_b": same}, {"draft_a": same, "draft_b": same})
    plan_followups(session, llm=LLM(session, client=fake_llm, cfg=cfg), now=T0 + timedelta(days=4), cfg=cfg)
    fu = session.exec(select(Message).where(Message.kind == "followup")).one()
    v = json.loads(fu.validation_json)
    assert fu.status == MessageStatus.DRAFT and any("repeats" in e for e in v["a"]["errors"])


def test_funnel_counts(session, sent_lead, cfg):
    lead, _ = sent_lead
    handle_inbound(session, lead, channel="email", text="What are your packages?", llm=None, cfg=cfg, draft=False)
    session.get(Lead, lead.id).status = "CLIENT"
    session.commit()
    data = funnel(session, "niche")
    row = data["groups"]["medspa"]
    assert row["sent"] == 1 and row["replied"] == 1 and row["paid"] == 1 and row["calls"] == 1
    assert funnel(session, "channel")["groups"]["email"]["sent"] == 1
