from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.security import TokenCipher, hash_password, verify_password


def test_healthz_and_auth_required(engine):
    with TestClient(create_app(testing=True)) as c:
        assert c.get("/healthz").json()["send_enabled"] is False
        assert c.get("/api/v1/settings").status_code == 401
        assert c.post("/login", data={"password": "wrong"}).status_code == 401


def test_settings_roundtrip_and_audit(client):
    r = client.put("/api/v1/settings", json={"tone": "formal", "packages": [{"name": "8 reels", "price": "INR 12000"}]})
    assert r.status_code == 200 and r.json()["tone"] == "formal"
    actions = [a["action"] for a in client.get("/api/v1/audit").json()]
    assert "settings.update" in actions and "auth.login" in actions


def test_add_suppression_api(client):
    assert client.post("/api/v1/suppressions", json={"type": "email", "value": "A@b.com"}).json() == {"added": True}
    assert client.post("/api/v1/suppressions", json={"type": "email", "value": "a@B.com"}).json() == {"added": False}
    assert client.post("/api/v1/suppressions", json={"type": "email", "value": "bad"}).status_code == 422


def test_password_and_token_crypto():
    h = hash_password("s3cret-pass!")
    assert verify_password("s3cret-pass!", h) and not verify_password("nope", h)
    c = TokenCipher()
    ct = c.encrypt('{"refresh_token": "abc"}')
    assert "abc" not in ct and c.decrypt(ct) == '{"refresh_token": "abc"}'


def test_erasure_keeps_suppression(client, session, make_lead):
    from app.models import Lead
    from app.services.suppression import is_suppressed
    lead_id = make_lead().id
    r = client.post(f"/api/v1/leads/{lead_id}/erase")
    assert r.status_code == 200 and r.json()["suppression_kept"]
    session.expire_all()
    assert session.get(Lead, lead_id) is None
    assert is_suppressed(session, "email", "hello@glowskin.in")


def test_sites_origin_requires_shared_token(client, monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "sites_origin_token", "test-origin-token")
    app = create_app(testing=True)
    with TestClient(app, base_url="https://workspace.test") as origin:
        assert origin.get("/livez").status_code == 204
        assert origin.get("/login").status_code == 404
        headers = {"x-proof-first-origin-token": "test-origin-token"}
        response = origin.post("/login", headers=headers, data={"password": "correct horse battery"},
                               follow_redirects=False)
        assert response.status_code == 303
        assert "proof_first_session=" in response.headers["set-cookie"]
        assert "secure" in response.headers["set-cookie"].lower()
        assert origin.get("/leads", headers=headers, follow_redirects=False).status_code == 200
