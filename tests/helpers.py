"""Shared builders for message-pipeline tests."""
from __future__ import annotations

from sqlmodel import select

from app.models import Niche, PortfolioAsset
from app.services.leads import add_evidence
from app.services.niches import seed_niches

PORTFOLIO_URL = "https://asha.video/medspa-reels"


def good_email_body(url: str = PORTFOLIO_URL) -> str:
    return ("Hi Meera,\n\nI saw that Glow Skin Studio shares weekly skincare reels on Instagram, and that your Baner "
            "branch opens Monday. I edit short-form reels for clinics and studios: tighter openings, animated keyword "
            "captions and a consistent look across every post, delivered within 48 hours. A few recent examples are "
            f"here: {url}\n\nIf it would help with the Baner launch content, would you be open to a quick look at "
            "how I'd approach one of your next reels?\n\nAsha")


def good_claims(ev_reels: int, ev_branch: int) -> list[dict]:
    return [{"text": "Glow Skin Studio shares weekly skincare reels on Instagram", "evidence_ids": [ev_reels]},
            {"text": "your Baner branch opens Monday", "evidence_ids": [ev_branch]}]


def setup_medspa_lead(session, make_lead, with_asset=True):
    seed_niches(session)
    session.commit()
    niche = session.exec(select(Niche).where(Niche.slug == "medspa")).one()
    lead = make_lead(niche_id=niche.id, status="QUALIFIED", instagram="@glowskin.pune")
    lead.contact_name = "Meera"
    e1 = add_evidence(session, lead, kind="website_text", source_type="web", source_url="https://glowskin.in",
                      snippet="We share weekly skincare reels on Instagram and run a monthly live Q&A.")
    e2 = add_evidence(session, lead, kind="caption", source_type="pasted_text", source_ref="instagram:caption:2",
                      snippet="Our Baner branch opens Monday!")
    if with_asset:
        session.add(PortfolioAsset(niche_id=niche.id, title="Medspa reels", url=PORTFOLIO_URL, type="reel",
                                   tags="reels,captions"))
        session.add(PortfolioAsset(niche_id=niche.id, title="B/A reel", url="https://asha.video/ba", type="before_after",
                                   tags="reels,captions,hooks"))
        session.add(PortfolioAsset(niche_id=None, title="Main reel", url="https://asha.video", type="main"))
    session.commit()
    return lead, e1.id, e2.id
