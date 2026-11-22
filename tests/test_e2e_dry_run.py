"""Dry-run end-to-end test. Must pass before setting SEND_ENABLED=true.

Drives the whole pipeline through the HTTP API with mocked website, Anthropic and Gmail:
import -> enrich -> intake -> qualify -> portfolio -> generate -> approve (dry run) ->
handoff -> reply opt-out -> suppression blocks any further contact.
"""
import os
import re
import subprocess
import sys

import httpx
import respx

from app.api.deps import get_fetcher, get_llm, get_mailer
from app.config import get_settings
from app.llm import LLM
from app.services.enrichment import PoliteFetcher
from tests.conftest import FakeAnthropic
from tests.test_phase1 import _mock_site
from tests.test_phase2_intake import PNG
from tests.test_phase4_dispatch import FakeMailer

PORTFOLIO = "https://asha.video/medspa"


def smart_llm(req):
    system = req["system"][0]["text"]
    content = req["messages"][0]["content"]
    if "extract structured facts" in system:
        return {"business_name": "Glow Skin Studio", "category": "aesthetic clinic", "services": ["skin care"],
                "country": "IN", "team": [{"name": "Dr. Meera Kulkarni", "role": "Founder"}],
                "snippets": [{"quote": "We share weekly skincare reels on Instagram", "source_url": "https://glowskin.in", "about": "video"}]}
    if "analyse public social-media material" in system:
        return {"observations": [{"text": "Captions appear in a single plain white font", "kind": "captioning",
                                  "evidence_ref": "screenshot:1", "confidence": 0.8}],
                "opportunities": [{"idea": "Animated keyword captions", "supports": [0]}], "unknown": [], "sufficient": True}
    if "judge whether a business is a good prospect" in system:
        return {"go": True, "adjustment": 10, "fit_reason": "Posts weekly reels and runs ads."}
    if "review two outreach drafts" in system:
        return {"score_a": 8, "score_b": 7, "selected": "a", "reason": "specific"}
    if "write one outreach message" in system:
        ev = re.search(r"^\[(\d+)\] \(website_text[^\n]*?: We share weekly skincare reels", content, re.M).group(1)
        dm = "Channel: email" not in content
        body = ("Hi Dr. Meera, I saw that Glow Skin Studio shares weekly skincare reels on Instagram. I edit "
                "short-form reels for studios: tighter openings, animated keyword captions and a consistent look "
                + ("" if dm else "across every post, usually delivered within 48 hours so your weekly schedule never slips. "
                                "I work with a simple shared folder, one round of revisions and clear turnaround. ")
                + f"Examples: {PORTFOLIO} Would you be open to a quick look at how I'd cut your next one?")
        d = {"subject": None if dm else "Editing for your weekly reels", "body": body,
             "claims": [{"text": "Glow Skin Studio shares weekly skincare reels on Instagram", "evidence_ids": [int(ev)]}]}
        return {"draft_a": d, "draft_b": d}
    raise AssertionError("unexpected LLM call: " + system[:80])


def test_full_pipeline_dry_run(client, session, tmp_path):
    cfg = get_settings()
    assert cfg.send_enabled is False
    cfg.upload_dir = str(tmp_path)
    from app.services.niches import seed_niches
    seed_niches(session)
    session.commit()
    fake = FakeAnthropic([smart_llm] * 50)
    mailer = FakeMailer()
    client.app.dependency_overrides[get_llm] = lambda: LLM(session, client=fake)
    client.app.dependency_overrides[get_mailer] = lambda: mailer
    with respx.mock(assert_all_called=False) as router:
        _mock_site(router)
        client.app.dependency_overrides[get_fetcher] = lambda: PoliteFetcher(client=httpx.Client(), sleep=lambda s: None)

        csv = ("name,website,city,country,niche,intent_signal\n"
               "Glow Skin Studio,https://glowskin.in,Pune,IN,medspa,Running Meta ads\n")
        r = client.post("/api/v1/leads/import", files={"file": ("l.csv", csv.encode(), "text/csv")})
        lead_id = r.json()["created_ids"][0]

        assert client.post(f"/api/v1/leads/{lead_id}/enrich").json()["ok"]
        r = client.post(f"/api/v1/leads/{lead_id}/intake",
                        data={"platform": "instagram", "profile_url": "https://instagram.com/glowskin.pune", "bio": "Skin studio"},
                        files=[("screenshots", ("a.png", PNG, "image/png"))])
        assert r.json()["sufficient"]
        assert client.post(f"/api/v1/leads/{lead_id}/qualify").json()["go"]
        client.put("/api/v1/settings", json={"country_focus": "IN"})

    niche_id = client.get(f"/api/v1/leads/{lead_id}").json()["niche_id"]
    client.post("/api/v1/portfolio", json={"niche_id": niche_id, "title": "Medspa reels", "url": PORTFOLIO, "tags": "reels,captions"})

    # email path: approve -> DRY_RUN, nothing sent
    m = client.post(f"/api/v1/leads/{lead_id}/messages/generate", json={"channel": "email"}).json()
    assert m.get("status") == "APPROVAL_PENDING", m.get("validation_json")
    assert m["to_address"] == "hello@glowskin.in"
    res = client.post(f"/api/v1/messages/{m['id']}/approve").json()
    assert res["status"] in ("DRY_RUN", "SCHEDULED")
    assert mailer.sent == []

    # handoff path works in dry run (a human sends)
    client.post(f"/api/v1/messages/{m['id']}/reject", json={"reason": "use DM instead"})
    dm = client.post(f"/api/v1/leads/{lead_id}/messages/generate", json={"channel": "instagram"}).json()
    assert dm.get("status") == "APPROVAL_PENDING", dm
    assert client.post(f"/api/v1/messages/{dm['id']}/approve").json()["status"] == "MANUAL_PENDING"
    assert client.post(f"/api/v1/messages/{dm['id']}/mark-sent-manually").json()["status"] == "SENT"

    # opt-out reply -> suppression -> no further generation
    r = client.post("/api/v1/interactions/manual", json={"lead_id": lead_id, "channel": "instagram", "body": "please stop"})
    assert r.json()["classification"] == "UNSUBSCRIBE"
    blocked = client.post(f"/api/v1/leads/{lead_id}/messages/generate", json={"channel": "email"})
    assert blocked.status_code == 409 and "suppressed" in blocked.json()["detail"]["detail"]

    actions = {a["action"] for a in client.get("/api/v1/audit?limit=500").json()}
    assert {"lead.create", "intake.analyse", "message.generate", "message.approve", "message.mark_sent_manually",
            "suppression.add", "reply.optout"} <= actions
    assert mailer.sent == []
    client.app.dependency_overrides.clear()


def test_migrations_match_models(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/m.db"}
    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    chk = subprocess.run([sys.executable, "-m", "alembic", "check"], env=env, capture_output=True, text=True)
    assert chk.returncode == 0, chk.stderr + chk.stdout
