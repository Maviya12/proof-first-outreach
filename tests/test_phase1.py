import httpx
import pytest
import respx
from sqlmodel import select

from app.llm import LLM
from app.models import ContactPoint, DiscoveryJob, Evidence, Lead, Organization, RawFile
from app.services.csv_import import import_csv
from app.services.enrichment import PoliteFetcher, choose_pages, parse_page, quote_is_verbatim
from app.services.niches import seed_niches
from app.services.places import PlacesClient, PlacesDisabled, run_discovery
from app.services.qualification import qualify_lead

CSV = """name,website,city,country,niche,email,instagram,intent_signal,notes
Glow Skin Studio,https://www.glowskin.in,Pune,IN,medspa,hello@glowskin.in,@glowskin.pune,Running Meta ads,Ads show 3 reels
Glow Skin Studio Pvt Ltd,https://glowskinpune.com,Pune,IN,medspa,,,,
Dup by domain,glowskin.in/?utm_source=x,Pune,IN,medspa,,,,
London Aesthetics,https://londonaes.co.uk,London,GB,medspa,info@londonaes.co.uk,,,
No niche,https://x.com,Pune,IN,unknownniche,,,,
,https://noname.in,Pune,IN,,,,,
"""

HOME = """<html><head><title>Glow Skin Studio | Pune</title>
<script type="application/ld+json">{"@type":"MedicalBusiness","name":"Glow Skin Studio","telephone":"+91 98765 43210",
"address":{"addressLocality":"Pune","addressCountry":"IN"}}</script></head>
<body><h1>Glow Skin Studio</h1><p>We share weekly skincare reels on Instagram and run a monthly live Q&A.</p>
<a href="/about">About</a><a href="/contact">Contact</a><a href="/blog/post-1">Blog</a>
<a href="https://www.instagram.com/glowskin.pune/">IG</a><a href="https://www.instagram.com/p/abc/">post</a>
<iframe src="https://www.youtube.com/embed/xyz"></iframe></body></html>"""
ABOUT = "<html><body><h2>Meet the team</h2><p>Dr. Meera Kulkarni, Founder and lead consultant.</p></body></html>"
CONTACT = "<html><body><a href='mailto:Hello@GlowSkin.in'>Email us</a> <img src='logo@2x.png'></body></html>"


@pytest.fixture
def niches(session):
    seed_niches(session)
    session.commit()


def test_csv_import_dedup_eu_and_report(session, niches):
    rep = import_csv(session, CSV)
    d = rep.as_dict()
    assert d["created"] == 2
    assert d["duplicates"][0]["matched_on"] == "domain"
    reasons = {s.get("name", ""): s["reason"] for s in d["skipped"]}
    assert "EU/UK" in reasons["London Aesthetics"]
    assert "unknown niche" in reasons["No niche"]
    assert any(s["reason"] == "missing name" for s in d["skipped"])
    assert d["flagged"] and "possible duplicate" in d["flagged"][0]["warnings"][0]
    lead = session.get(Lead, d["created_ids"][0])
    assert lead.intent_signal == "Running Meta ads"
    assert {c.type for c in session.exec(select(ContactPoint).where(ContactPoint.lead_id == lead.id))} == {"email", "instagram"}
    assert session.exec(select(Evidence).where(Evidence.lead_id == lead.id, Evidence.kind == "note")).all()


def test_parse_page_and_choose_pages():
    pd = parse_page("https://glowskin.in", HOME)
    assert "glowskin.pune" in str(pd.socials["instagram"]) and pd.video_embeds == 1
    assert pd.jsonld[0]["name"] == "Glow Skin Studio"
    pages = choose_pages("https://glowskin.in", pd.links)
    assert pages == ["https://glowskin.in/about", "https://glowskin.in/contact"]
    assert not parse_page("https://glowskin.in/contact", CONTACT).emails - {"hello@glowskin.in"}


def test_verbatim_check():
    text = "We share weekly skincare reels on Instagram and run a monthly live Q&A."
    assert quote_is_verbatim("weekly skincare reels on Instagram", text)
    assert not quote_is_verbatim("They post daily TikToks with 1M views", text)


def _mock_site(router, robots="User-agent: *\nAllow: /"):
    router.get("https://glowskin.in/robots.txt").respond(200, text=robots)
    router.get("https://glowskin.in/about").respond(200, html=ABOUT)
    router.get("https://glowskin.in/contact").respond(200, html=CONTACT)
    router.get(url__regex=r"^https://glowskin\.in/?$").respond(200, html=HOME)


def test_enrichment_end_to_end(session, make_lead, fake_llm, cfg, tmp_path):
    cfg.upload_dir = str(tmp_path)
    lead = make_lead(email=None, domain="glowskin.in")
    lead.website = "https://glowskin.in"
    session.add(lead)
    session.commit()
    fake_llm.queue({"business_name": "Glow Skin Studio", "category": "aesthetic clinic", "services": ["skin care"],
                    "audience": "young professionals", "tone": "warm", "country": "IN",
                    "team": [{"name": "Dr. Meera Kulkarni", "role": "Founder"}, {"name": "Invented Person", "role": "CEO"}],
                    "snippets": [
                        {"quote": "We share weekly skincare reels on Instagram", "source_url": "https://glowskin.in", "about": "video"},
                        {"quote": "Voted best clinic in India 2025", "source_url": "https://glowskin.in", "about": "award"}]})
    sleeps = []
    with respx.mock(assert_all_called=False) as router:
        _mock_site(router)
        fetcher = PoliteFetcher(client=httpx.Client(), cfg=cfg, sleep=sleeps.append)
        res = enrich_and_commit(session, lead, fetcher, LLM(session, client=fake_llm, cfg=cfg), cfg)
    assert res["ok"] and res["pages"] == 3 and res["rejected_snippets"] == 1
    assert sleeps, "per-domain throttle must sleep between requests"
    session.refresh(lead)
    assert lead.status == "RESEARCHED" and lead.contact_name == "Dr. Meera Kulkarni"
    types = {c.type: c for c in session.exec(select(ContactPoint).where(ContactPoint.lead_id == lead.id))}
    assert types["email"].value_norm == "hello@glowskin.in" and types["email"].source_url == "https://glowskin.in/contact"
    assert types["phone"].value_norm == "+919876543210" and "instagram" in types
    snippets = [e.snippet for e in session.exec(select(Evidence).where(Evidence.lead_id == lead.id))]
    assert any("weekly skincare reels" in s for s in snippets)
    assert not any("Voted best" in s for s in snippets)  # hallucinated quote rejected
    assert len(session.exec(select(RawFile)).all()) == 3
    # the extraction request used the cheaper model
    assert fake_llm.requests[0]["model"] == cfg.model_extract


def enrich_and_commit(session, lead, fetcher, llm, cfg):
    from app.services.enrichment import enrich_lead
    res = enrich_lead(session, lead, fetcher=fetcher, llm=llm, cfg=cfg)
    session.commit()
    return res


def test_robots_disallow_is_respected(session, make_lead, cfg, tmp_path):
    cfg.upload_dir = str(tmp_path)
    lead = make_lead(domain="glowskin.in")
    lead.website = "https://glowskin.in"
    with respx.mock(assert_all_called=False) as router:
        _mock_site(router, robots="User-agent: *\nDisallow: /")
        fetcher = PoliteFetcher(client=httpx.Client(), cfg=cfg, sleep=lambda s: None)
        res = enrich_and_commit(session, lead, fetcher, None, cfg)
        assert not router.routes[3].called  # homepage never requested
    assert not res["ok"] and "robots" in res["reason"]


def test_places_discovery_persists_only_place_id(session, niches, cfg, tmp_path):
    cfg.places_discovery_enabled = True
    from app.models import Niche
    niche = session.exec(select(Niche).where(Niche.slug == "medspa")).one()
    job = DiscoveryJob(niche_id=niche.id, city="Pune", max_results=5)
    session.add(job)
    session.commit()
    with respx.mock(assert_all_called=False) as router:
        search = router.post("https://places.googleapis.com/v1/places:searchText").respond(200, json={"places": [
            {"id": "P1", "displayName": {"text": "Glow Skin (Places name)"}, "primaryType": "beauty_salon", "types": ["spa"]},
            {"id": "P2", "displayName": {"text": "Hardware Store"}, "primaryType": "hardware_store", "types": []},
            {"id": "P3", "displayName": {"text": "No Site Spa"}, "primaryType": "spa", "types": []}]})
        router.get("https://places.googleapis.com/v1/places/P1").respond(200, json={"id": "P1", "websiteUri": "https://glowskin.in"})
        router.get("https://places.googleapis.com/v1/places/P3").respond(200, json={"id": "P3"})
        _mock_site(router)
        run_discovery(session, job, places=PlacesClient("k", client=httpx.Client()),
                      fetcher=PoliteFetcher(client=httpx.Client(), cfg=cfg, sleep=lambda s: None), cfg=cfg)
        assert search.calls[0].request.headers["X-Goog-FieldMask"].startswith("places.id,")
    assert job.status == "DONE" and job.created == 1 and job.skipped == 2
    org = session.exec(select(Organization)).one()
    assert org.google_place_id == "P1"
    assert org.name == "Glow Skin Studio"  # from the business website, not Places
    assert org.name != "Glow Skin (Places name)"


def test_places_disabled_by_default(session, cfg):
    job = DiscoveryJob(city="Pune")
    with pytest.raises(PlacesDisabled):
        run_discovery(session, job, cfg=cfg)


def test_qualification_scores_and_threshold(session, niches, make_lead, fake_llm, cfg):
    from app.models import Niche
    from app.services.leads import add_evidence
    niche = session.exec(select(Niche).where(Niche.slug == "medspa")).one()
    good = make_lead(niche_id=niche.id, instagram="@glowskin")
    good.description = "aesthetic clinic; skin care"
    good.intent_signal = "Running Meta ads"
    for s in ["We share weekly skincare reels on Instagram", "Page embeds 1 video player(s)"]:
        add_evidence(session, good, kind="website_text", source_type="web", snippet=s, source_url="https://glowskin.in")
    session.commit()
    fake_llm.queue({"go": True, "adjustment": 5, "fit_reason": "Posts weekly reels [E1] and runs ads.", "size_band": "small"})
    res = qualify_lead(session, good, llm=LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert res["go"] and good.status == "QUALIFIED" and good.quality_score >= 55 and "[E1]" in good.fit_reason
    assert fake_llm.requests[0]["model"] == cfg.model_judge

    weak = make_lead(name="Nowhere", domain="nowhere.in", email=None, niche_id=niche.id)
    res2 = qualify_lead(session, weak, llm=None, cfg=cfg)
    assert not res2["go"] and weak.status == "NOT_A_FIT" and "No usable contact" in weak.fit_reason


def test_api_import_and_list(client, session):
    seed_niches(session)
    session.commit()
    r = client.post("/api/v1/leads/import", files={"file": ("leads.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["created"] == 2
    leads = client.get("/api/v1/leads", params={"niche": "medspa", "platform": "instagram"}).json()
    assert len(leads) == 1 and leads[0]["organization"]["name"] == "Glow Skin Studio"
    detail = client.get(f"/api/v1/leads/{leads[0]['id']}").json()
    assert detail["contact_points"] and detail["evidence"]
    assert client.post("/api/v1/discovery/jobs", json={"niche": "medspa", "city": "Pune"}).json()["detail"]["error"] == "PLACES_DISABLED"
