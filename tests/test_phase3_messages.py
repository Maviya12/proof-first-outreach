import json

import pytest
from sqlmodel import select

from app.llm import LLM
from app.models import Lead, MessageEvidence, MessageStatus, Niche, PortfolioAsset
from app.services.messaging import GenerationBlocked, apply_edit, generate_message
from app.services.portfolio import select_asset
from app.services.suppression import suppress_lead
from app.services.validator import Claim, validate_draft
from tests.helpers import PORTFOLIO_URL, good_claims, good_email_body, setup_medspa_lead


def _v(session, lead_ev, body, claims, **kw):
    from app.services.messaging import evidence_map
    lead, e1, e2 = lead_ev
    args = dict(body=body, subject="Reels for the Baner launch", channel="email", claims=claims,
                evidence=evidence_map(session, lead.id), portfolio_url=PORTFOLIO_URL, is_medical=True,
                niche_banned=["results", "before and after"], sample_offer_enabled=False)
    args.update(kw)
    return validate_draft(**args)


@pytest.fixture
def lead_ev(session, make_lead):
    return setup_medspa_lead(session, make_lead)


def test_validator_accepts_good_email(session, lead_ev):
    _, e1, e2 = lead_ev
    r = _v(session, lead_ev, good_email_body(), [Claim(**c) for c in good_claims(e1, e2)])
    assert r.ok, r.errors


@pytest.mark.parametrize("mutate,expect", [
    (lambda b: "I hope this finds you well. " + b, "banned phrase"),
    (lambda b: b.replace(PORTFOLIO_URL, "https://asha.video/other"), "portfolio URL"),
    (lambda b: b.replace("?", "."), "no call to action"),
    (lambda b: b + " Free?", "2 questions"),
    (lambda b: b.replace("tighter openings", "better patient results"), "medical niche"),
    (lambda b: b.replace("tighter openings", "a free sample re-edit"), "sample"),
    (lambda b: b.replace("Hi Meera,", "Hi Meera, I noticed your clinic has 40,000 followers."), "without evidence"),
    (lambda b: b.replace("Hi Meera,", "Hi Meera, I'm a huge fan."), "flattery"),
    (lambda b: b.replace("Hi Meera,", "Hi Meera, as we discussed,"), "fake familiarity"),
    (lambda b: b + " Limited time only.", "pressure"),
    (lambda b: " ".join(b.split()[:40]), "length"),
])
def test_validator_catches_problems(session, lead_ev, mutate, expect):
    _, e1, e2 = lead_ev
    r = _v(session, lead_ev, mutate(good_email_body()), [Claim(**c) for c in good_claims(e1, e2)])
    assert not r.ok and any(expect in e for e in r.errors), r.errors


def test_validator_rejects_bad_citations(session, lead_ev):
    _, e1, e2 = lead_ev
    body = good_email_body()
    assert not _v(session, lead_ev, body, [Claim("weekly skincare reels", [9999])]).ok
    unsupported = _v(session, lead_ev, body, [Claim("Glow Skin Studio shares weekly skincare reels on Instagram", [e2]),
                                              Claim("your Baner branch opens Monday", [e2])])
    assert any("not supported" in e for e in unsupported.errors)


def test_number_must_come_from_evidence(session, lead_ev):
    _, e1, e2 = lead_ev
    body = good_email_body().replace("weekly skincare reels", "12 weekly skincare reels")
    claims = [Claim("Glow Skin Studio shares 12 weekly skincare reels on Instagram", [e1]),
              Claim("your Baner branch opens Monday", [e2])]
    r = _v(session, lead_ev, body, claims)
    assert any("number" in e for e in r.errors)


def test_portfolio_matcher_excludes_before_after_for_medical(session, lead_ev):
    lead, *_ = lead_ev
    asset = select_asset(session, lead)
    assert asset.url == PORTFOLIO_URL  # b/a asset has more tag overlap but is excluded


def test_portfolio_falls_back_to_main_never_other_niche(session, make_lead):
    from app.services.niches import seed_niches
    seed_niches(session)
    session.commit()
    realtor = session.exec(select(Niche).where(Niche.slug == "realtor")).one()
    medspa = session.exec(select(Niche).where(Niche.slug == "medspa")).one()
    session.add(PortfolioAsset(niche_id=medspa.id, title="m", url="https://a/m", type="reel"))
    session.add(PortfolioAsset(niche_id=None, title="Main", url="https://a/main", type="main"))
    session.commit()
    lead = make_lead(niche_id=realtor.id)
    assert select_asset(session, lead).url == "https://a/main"


def test_portfolio_tie_breaker_only_among_eligible(session, lead_ev):
    lead, *_ = lead_ev
    niche_id = lead.niche_id
    session.add(PortfolioAsset(niche_id=niche_id, title="Second", url="https://asha.video/2", type="reel", tags="reels,captions"))
    session.commit()
    outsider = PortfolioAsset(id=999, title="x", url="https://evil", type="reel")
    assert select_asset(session, lead, tie_breaker=lambda l, a: outsider).url == PORTFOLIO_URL
    assert select_asset(session, lead, tie_breaker=lambda l, a: a[1]).url == "https://asha.video/2"


def _pair(e1, e2, a_body=None, b_body=None):
    d = lambda body: {"subject": "Reels for the Baner launch", "body": body, "claims": good_claims(e1, e2)}  # noqa: E731
    return {"draft_a": d(a_body or good_email_body()), "draft_b": d(b_body or good_email_body())}


def test_generate_two_drafts_judge_selects(session, lead_ev, fake_llm, cfg):
    lead, e1, e2 = lead_ev
    fake_llm.queue(_pair(e1, e2), {"score_a": 6, "score_b": 8, "selected": "b", "reason": "more specific"})
    msg = generate_message(session, lead, "email", llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    session.commit()
    assert msg.status == MessageStatus.APPROVAL_PENDING and msg.to_address == "hello@glowskin.in"
    v = json.loads(msg.validation_json)
    assert v["selected"] == "b" and v["a"]["ok"] and v["judge"]["reason"] == "more specific"
    assert {r.evidence_id for r in session.exec(select(MessageEvidence))} == {e1, e2}
    assert session.get(Lead, lead.id).status == "APPROVAL_PENDING"
    assert all(r["model"] == cfg.model_judge for r in fake_llm.requests)
    prompt = fake_llm.requests[0]["messages"][0]["content"]
    assert PORTFOLIO_URL in prompt and f"[{e1}]" in prompt


def test_generate_retries_once_then_keeps_draft(session, lead_ev, fake_llm, cfg):
    lead, e1, e2 = lead_ev
    bad = good_email_body().replace("?", ".")
    fake_llm.queue(_pair(e1, e2, bad, bad), _pair(e1, e2, bad, bad))
    msg = generate_message(session, lead, "email", llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert msg.status == MessageStatus.DRAFT and json.loads(msg.validation_json)["attempts"] == 2
    assert "failed validation" in fake_llm.requests[1]["messages"][0]["content"]
    # human fixes it -> APPROVAL_PENDING
    res = apply_edit(session, msg, body=good_email_body())
    assert res.ok and msg.status == MessageStatus.APPROVAL_PENDING


def test_human_edit_unsupported_sentence_is_warning_not_error(session, lead_ev, fake_llm, cfg):
    lead, e1, e2 = lead_ev
    fake_llm.queue(_pair(e1, e2), {"score_a": 8, "score_b": 6, "selected": "a", "reason": "ok"})
    msg = generate_message(session, lead, "email", llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    res = apply_edit(session, msg, body=good_email_body().replace("Hi Meera,", "Hi Meera, I saw your Diwali reel too."))
    assert res.ok and any("without evidence" in w for w in res.warnings)
    res2 = apply_edit(session, msg, body=good_email_body() + " Guaranteed!")
    assert not res2.ok and msg.status == MessageStatus.DRAFT


def test_generation_blocked_cases(session, lead_ev, fake_llm, cfg):
    lead, *_ = lead_ev
    llm = LLM(session, client=fake_llm, cfg=cfg)
    with pytest.raises(GenerationBlocked, match="no usable x contact"):
        generate_message(session, lead, "x", llm=llm, cfg=cfg)
    lead.intake_status = "INSUFFICIENT"
    with pytest.raises(GenerationBlocked, match="intake insufficient"):
        generate_message(session, lead, "instagram", llm=llm, cfg=cfg)
    lead.intake_status = None
    suppress_lead(session, lead, "stop")
    with pytest.raises(GenerationBlocked, match="suppressed"):
        generate_message(session, lead, "email", llm=llm, cfg=cfg)
    assert fake_llm.requests == []


def test_handoff_dm_has_no_subject(session, lead_ev, fake_llm, cfg):
    lead, e1, e2 = lead_ev
    dm = (f"Hi Meera, I saw your Baner branch opens Monday. I edit short-form reels for studios and clinics, with "
          f"animated keyword captions, tight openings and a consistent look across posts, usually within 48 hours. "
          f"Examples: {PORTFOLIO_URL} Would a couple of launch reels be useful?")
    claims = [{"text": "your Baner branch opens Monday", "evidence_ids": [e2]}]
    fake_llm.queue({"draft_a": {"subject": None, "body": dm, "claims": claims},
                    "draft_b": {"subject": "x", "body": dm, "claims": claims}},
                   {"score_a": 7, "score_b": 7, "selected": "a", "reason": "same"})
    msg = generate_message(session, lead, "instagram", llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert msg.status == MessageStatus.APPROVAL_PENDING, msg.validation_json
    assert msg.subject is None and msg.to_address == "glowskin.pune"
