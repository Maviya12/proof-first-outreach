import json

from app.models import Message, MessageStatus
from tests.helpers import good_claims, good_email_body, setup_medspa_lead


def test_ui_requires_login(engine):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(testing=True)) as c:
        r = c.get("/approvals", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_all_pages_render(client, session, make_lead):
    lead, e1, e2 = setup_medspa_lead(session, make_lead)
    d = json.dumps({"subject": "Reels for the Baner launch", "body": good_email_body(), "claims": good_claims(e1, e2)})
    m = Message(lead_id=lead.id, channel="email", draft_a=d, draft_b=d, body=good_email_body(), subject="Reels",
                to_address="hello@glowskin.in", status=MessageStatus.APPROVAL_PENDING, portfolio_asset_id=1,
                validation_json=json.dumps({"a": {"ok": True, "errors": [], "warnings": [], "word_count": 90},
                                            "b": {"ok": True, "errors": [], "warnings": [], "word_count": 90},
                                            "selected": "a", "judge": {"score_a": 8, "score_b": 6, "reason": "x"}}))
    session.add(m)
    session.commit()
    for path in ["/approvals", "/leads", f"/leads/{lead.id}", "/discovery", "/handoff", "/inbox", "/portfolio",
                 "/settings", "/funnel", "/funnel?group_by=channel"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    page = client.get("/approvals").text
    assert "Glow Skin Studio" in page and "Approve" in page and "E%d" % e1 in page
    assert "DRY RUN" in page


def test_ui_approve_and_edit_actions(client, session, make_lead):
    lead, e1, e2 = setup_medspa_lead(session, make_lead)
    d = json.dumps({"subject": "Reels for the Baner launch", "body": good_email_body(), "claims": good_claims(e1, e2)})
    m = Message(lead_id=lead.id, channel="email", draft_a=d, draft_b=d, body=good_email_body(), subject="Reels",
                to_address="hello@glowskin.in", status=MessageStatus.APPROVAL_PENDING, portfolio_asset_id=1,
                validation_json=json.dumps({"a": {"ok": True}, "b": {"ok": True}, "selected": "a"}))
    session.add(m)
    session.commit()
    r = client.post(f"/ui/messages/{m.id}/edit", data={"body": good_email_body() + " guaranteed", "subject": "Reels"})
    assert "banned phrase" in r.text
    r = client.post(f"/ui/messages/{m.id}/edit", data={"select_draft": "a"})
    assert "validation passed" in r.text
    r = client.post(f"/ui/messages/{m.id}/approve")
    assert "DRY_RUN" in r.text or "SCHEDULED" in r.text


def test_ui_settings_and_import(client, session):
    from app.services.niches import seed_niches
    seed_niches(session)
    session.commit()
    r = client.post("/ui/settings", data={"tone": "formal", "packages": '[{"name":"Starter","price":"INR 12,000"}]',
                                          "rates": "{}", "quality_threshold": 60, "followup_gaps_days": "3,8"},
                    follow_redirects=False)
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    assert client.get("/api/v1/settings").json()["followup_gaps_days"] == [3, 8]
    csv = "name,website,city,country,niche\nGlow,https://glow.in,Pune,IN,medspa\n"
    r = client.post("/ui/import", files={"file": ("l.csv", csv.encode(), "text/csv")})
    assert "<b>1</b> created" in r.text
