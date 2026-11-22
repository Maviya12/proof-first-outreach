import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
import respx
from sqlmodel import select

from app.config import get_settings
from app.models import ContactPoint, Evidence, Interaction, Lead, Message, MessageStatus
from app.services.dispatcher import approve_message
from app.services.meta_inbound import process_webhook, verify_signature, within_window
from app.services.youtube import YouTubeClient, add_youtube_evidence, iso_duration_seconds


def _payload(text, psid="1234567890", mid="m1", ts=None):
    ts = ts or int(datetime.utcnow().timestamp() * 1000)
    return {"object": "instagram", "entry": [{"id": "page", "messaging": [
        {"sender": {"id": psid}, "recipient": {"id": "page"}, "timestamp": ts, "message": {"mid": mid, "text": text}}]}]}


def test_signature_check():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, "secret")
    assert not verify_signature(body, sig, "other") and not verify_signature(body, None, "secret")
    assert not verify_signature(body, sig, "")  # no secret configured: fail closed


def test_webhook_endpoint_gating(client):
    s = get_settings()
    s.meta_inbound_enabled, s.meta_app_secret, s.meta_verify_token = True, "secret", "vt"
    try:
        r = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
        assert r.text == "42"
        body = json.dumps(_payload("hi")).encode()
        assert client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403
        sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        s.anthropic_api_key = ""
        r = client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sig,
                                                                 "Content-Type": "application/json"})
        assert r.status_code == 200 and r.json()["handled"] == 1
    finally:
        s.meta_inbound_enabled, s.anthropic_api_key = False, "test-key"


def test_inbound_creates_lead_and_optout_suppresses_psid(session, cfg):
    process_webhook(session, _payload("Hi, how much for 10 reels?"), llm=None, cfg=cfg)
    session.commit()
    cp = session.exec(select(ContactPoint).where(ContactPoint.type == "ig_psid")).one()
    lead = session.get(Lead, cp.lead_id)
    assert lead.source == "meta_inbound" and lead.status == "REPLIED"
    # duplicate delivery is ignored
    process_webhook(session, _payload("Hi, how much for 10 reels?"), llm=None, cfg=cfg)
    assert len(session.exec(select(Interaction)).all()) == 1
    process_webhook(session, _payload("stop messaging", mid="m2"), llm=None, cfg=cfg)
    assert lead.status == "DO_NOT_CONTACT"
    from app.services.suppression import is_suppressed
    assert is_suppressed(session, "ig_psid", "1234567890")


def test_echo_and_non_messages_ignored(session, cfg):
    p = _payload("hello")
    p["entry"][0]["messaging"][0]["message"]["is_echo"] = True
    assert process_webhook(session, p, llm=None, cfg=cfg)["handled"] == 0
    assert process_webhook(session, {"object": "whatsapp_business_account"}, cfg=cfg) == {"ignored": "whatsapp_business_account"}


def _reply_for(session, lead, psid):
    m = Message(lead_id=lead.id, channel="instagram", kind="reply", to_address=psid,
                body="Thanks for reaching out! My Starter package is 8 reels a month. Want me to share samples?",
                status=MessageStatus.APPROVAL_PENDING, validation_json=json.dumps({"final": {"ok": True}}))
    session.add(m)
    session.commit()
    return m


def test_meta_reply_inside_24h_uses_send_api(session, cfg):
    cfg.meta_inbound_enabled, cfg.meta_page_access_token, cfg.send_enabled = True, "tok", True
    now = datetime.utcnow()
    process_webhook(session, _payload("Hi there", ts=int((now - timedelta(hours=2)).timestamp() * 1000)), cfg=cfg)
    lead = session.exec(select(Lead)).one()
    m = _reply_for(session, lead, "1234567890")
    with respx.mock() as router:
        route = router.post(url__regex=r"https://graph\.facebook\.com/v21\.0/me/messages.*").respond(200, json={"message_id": "mid.1"})
        from app.services import meta_inbound
        orig = meta_inbound.send_reply
        meta_inbound.send_reply = lambda msg, c, client=None: orig(msg, c, client=httpx.Client())
        try:
            res = approve_message(session, m, now=now, cfg=cfg)
        finally:
            meta_inbound.send_reply = orig
        assert route.called
        sent = json.loads(route.calls[0].request.content)
        assert sent["recipient"]["id"] == "1234567890" and sent["messaging_type"] == "RESPONSE"
    assert res.status == "SENT" and m.status == MessageStatus.SENT


def test_meta_reply_outside_24h_falls_back_to_handoff(session, cfg):
    cfg.meta_inbound_enabled, cfg.meta_page_access_token, cfg.send_enabled = True, "tok", True
    now = datetime.utcnow()
    process_webhook(session, _payload("Hi", ts=int((now - timedelta(hours=30)).timestamp() * 1000)), cfg=cfg)
    lead = session.exec(select(Lead)).one()
    assert not within_window(session, lead, now)
    m = _reply_for(session, lead, "1234567890")
    assert approve_message(session, m, now=now, cfg=cfg).status == "MANUAL_PENDING"


def test_youtube_metadata_evidence(session, make_lead, cfg):
    lead = make_lead()
    with respx.mock() as router:
        router.get(url__regex=r".*/channels.*").respond(200, json={"items": [{
            "id": "UC1", "snippet": {"title": "Glow Skin"}, "statistics": {"videoCount": "84"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        router.get(url__regex=r".*/playlistItems.*").respond(200, json={"items": [
            {"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]})
        router.get(url__regex=r".*/videos.*").respond(200, json={"items": [
            {"id": "v1", "snippet": {"title": "Skincare tip", "publishedAt": "2026-11-01T10:00:00Z"}, "contentDetails": {"duration": "PT45S"}},
            {"id": "v2", "snippet": {"title": "Studio tour", "publishedAt": "2026-10-20T10:00:00Z"}, "contentDetails": {"duration": "PT4M10S"}}]})
        res = add_youtube_evidence(session, lead, "https://www.youtube.com/@GlowSkin",
                                   yt=YouTubeClient("k", client=httpx.Client()), cfg=cfg)
    assert res["evidence_added"] == 4
    snippets = [e.snippet for e in session.exec(select(Evidence).where(Evidence.kind == "youtube_meta"))]
    assert "YouTube channel has 84 public videos" in snippets
    assert any("1 are Shorts" in s and "2026-11-01" in s for s in snippets)


def test_iso_duration():
    assert iso_duration_seconds("PT1H2M3S") == 3723 and iso_duration_seconds("PT45S") == 45


def test_extra_niches_seeded(session):
    from app.models import Niche
    from app.services.niches import seed_niches
    seed_niches(session)
    slugs = {n.slug for n in session.exec(select(Niche))}
    assert {"restaurant", "fitness", "coaching"} <= slugs
