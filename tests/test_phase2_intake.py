import base64

import pytest
from sqlmodel import select

from app.llm import LLM
from app.models import ContactPoint, Evidence, RawFile
from app.services.intake import IntakeInput, run_intake, split_captions

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

ANALYSIS = {
    "observations": [
        {"text": "Captions appear in a single plain white font with no animation", "kind": "captioning", "evidence_ref": "screenshot:1", "confidence": 0.8},
        {"text": "First frame of reel thumbnail shows only the clinic logo, no hook text", "kind": "hook", "evidence_ref": "screenshot:2", "confidence": 0.7},
        {"text": "caption:2 announces a new Baner branch opening", "kind": "other", "evidence_ref": "caption:2", "confidence": 0.9},
        {"text": "Posts roughly twice a week", "kind": "cadence", "evidence_ref": "bio", "confidence": 0.5},
        {"text": "Patients look visibly younger after treatment", "kind": "other", "evidence_ref": "screenshot:3", "confidence": 0.4},
        {"text": "Thumbnail uses three colours", "kind": "branding", "evidence_ref": "screenshot:9", "confidence": 0.6},
        {"text": "Bio lists Pune and Baner locations", "kind": "branding", "evidence_ref": "bio", "confidence": 0.9},
    ],
    "opportunities": [
        {"idea": "Animated keyword captions to lift readability", "supports": [0]},
        {"idea": "Open with a 1-second hook line before the logo", "supports": [1]},
        {"idea": "Show patient results more", "supports": [4]},
    ],
    "unknown": ["video audio quality", "screenshot:3 skipped (sensitive content)"],
    "content_types": ["reels", "talking_head"], "posting_cadence": "twice weekly",
    "visual_style": "clean, pastel", "brand_consistency": "mixed", "sufficient": True,
}


def _input(**kw):
    base = dict(platform="instagram", profile_url="https://www.instagram.com/glowskin.pune/",
                bio="Skin & hair studio | Pune + Baner", captions=split_captions("Weekend offer reel\n---\nOur Baner branch opens Monday!"),
                user_note="reel at 0:12 has no captions", screenshots=[("a.png", PNG), ("b.png", PNG), ("c.png", PNG)])
    base.update(kw)
    return IntakeInput(**base)


def test_intake_creates_cited_observations(session, make_lead, fake_llm, cfg, tmp_path):
    cfg.upload_dir = str(tmp_path)
    lead = make_lead()
    fake_llm.queue(ANALYSIS)
    res = run_intake(session, lead, _input(), llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    session.commit()

    texts = [o["text"] for o in res["observations"]]
    assert len(texts) == 4  # plain-font, logo-hook, caption:2, bio locations
    whys = {d["why"] for d in res["dropped"]}
    assert "cadence without supplied dates" in whys
    assert "speculative/sensitive content" in whys
    assert any("unknown reference" in w for w in whys)
    assert res["posting_cadence"] is None  # no dates supplied
    assert [o["idea"] for o in res["opportunities"]] == ["Animated keyword captions to lift readability",
                                                         "Open with a 1-second hook line before the logo"]
    assert res["sufficient"] and lead.intake_status == "SUFFICIENT"

    shot = session.exec(select(Evidence).where(Evidence.kind == "screenshot_observation")).first()
    assert shot.source_type == "screenshot" and shot.source_ref.startswith("instagram:screenshot:1:a.png")
    assert session.exec(select(Evidence).where(Evidence.kind == "caption")).all()
    assert session.exec(select(ContactPoint).where(ContactPoint.type == "instagram")).one().value_norm == "glowskin.pune"
    assert len(session.exec(select(RawFile).where(RawFile.kind == "screenshot")).all()) == 3

    req = fake_llm.requests[0]
    assert req["model"] == cfg.model_judge
    kinds = [b["type"] for b in req["messages"][0]["content"]]
    assert kinds.count("image") == 3


def test_cadence_kept_when_dates_supplied(session, make_lead, fake_llm, cfg, tmp_path):
    cfg.upload_dir = str(tmp_path)
    lead = make_lead()
    fake_llm.queue(ANALYSIS)
    res = run_intake(session, lead, _input(dates_note="Posts on 1, 4, 8, 11 Nov"), llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert any(o["kind"] == "cadence" for o in res["observations"]) and res["posting_cadence"] == "twice weekly"


def test_insufficient_material_blocks(session, make_lead, fake_llm, cfg, tmp_path):
    cfg.upload_dir = str(tmp_path)
    lead = make_lead()
    fake_llm.queue({"observations": [], "opportunities": [], "unknown": ["only a bio was supplied"], "sufficient": False})
    res = run_intake(session, lead, _input(captions=[], screenshots=[]), llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert not res["sufficient"] and lead.intake_status == "INSUFFICIENT" and "Intake insufficient" in lead.needs_review


@pytest.mark.parametrize("kw,msg", [
    ({"platform": "tiktok"}, "platform"),
    ({"profile_url": "https://www.instagram.com/p/abc/"}, "not a profile"),
    ({"screenshots": [("x.gif", b"GIF89a....")]}, "PNG or JPG"),
    ({"screenshots": [("a.png", PNG)] * 9}, "at most 8"),
    ({"bio": "", "captions": [], "screenshots": []}, "supply"),
])
def test_intake_validation(session, make_lead, fake_llm, cfg, tmp_path, kw, msg):
    cfg.upload_dir = str(tmp_path)
    with pytest.raises(ValueError, match=msg):
        run_intake(session, make_lead(), _input(**kw), llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert fake_llm.requests == []


def test_intake_api_multipart(client, session, make_lead, fake_llm, tmp_path):
    from app.api.deps import get_llm
    from app.config import get_settings
    get_settings().upload_dir = str(tmp_path)
    lead = make_lead()
    client.app.dependency_overrides[get_llm] = lambda: LLM(session, client=fake_llm)
    fake_llm.queue(ANALYSIS)
    r = client.post(f"/api/v1/leads/{lead.id}/intake",
                    data={"platform": "instagram", "profile_url": "instagram.com/glowskin.pune",
                          "bio": "Skin studio", "captions": "one\n\ntwo"},
                    files=[("screenshots", ("a.png", PNG, "image/png")), ("screenshots", ("b.png", PNG, "image/png")),
                           ("screenshots", ("c.png", PNG, "image/png"))])
    assert r.status_code == 200, r.text
    assert r.json()["sufficient"]
    client.app.dependency_overrides.clear()
