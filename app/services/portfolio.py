"""Deterministic portfolio matching (spec 6.5)."""
from __future__ import annotations

import re

from sqlmodel import Session, select

from app.models import Lead, Niche, PortfolioAsset
from app.services.leads import evidence_of
from app.services.niches import niche_config

TAG_KEYWORDS = {
    "reels": ["reel", "reels", "short-form", "shorts", "tiktok"],
    "walkthrough": ["walkthrough", "property tour", "home tour", "listing video"],
    "talking_head": ["talking head", "talking-head", "to camera", "doctor explains", "founder video"],
    "captions": ["caption", "subtitles", "on-screen text"],
    "hooks": ["hook", "first frame", "first 2 seconds", "opening"],
    "cinematic": ["cinematic", "drone", "b-roll"],
    "podcast_clips": ["podcast", "long-form", "episode"],
    "youtube": ["youtube", "vlog"],
    "ads": ["ad library", "ads running", "running ads", "meta ads", "sponsored"],
    "educational": ["educational", "explainer", "tips", "how to", "q&a"],
    "brand": ["brand", "logo", "colour", "color", "font"],
}


def asset_tags(asset: PortfolioAsset) -> set[str]:
    return {t.strip().lower() for t in (asset.tags or "").split(",") if t.strip()} | {asset.type}


def lead_content_tags(session: Session, lead: Lead) -> set[str]:
    text = " ".join(e.snippet.lower() for e in evidence_of(session, lead.id) if e.kind != "opportunity")
    text += " " + (lead.description or "").lower() + " " + (lead.intent_signal or "").lower()
    tags = {tag for tag, words in TAG_KEYWORDS.items() if any(re.search(r"\b" + re.escape(w), text) for w in words)}
    return tags


def eligible_assets(session: Session, lead: Lead) -> list[tuple[PortfolioAsset, int]]:
    """Niche assets ordered by tag overlap (desc). Medical niches never get before/after samples."""
    niche = session.get(Niche, lead.niche_id) if lead.niche_id else None
    ncfg = niche_config(niche)
    if not niche:
        return []
    want = lead_content_tags(session, lead)
    assets = session.exec(select(PortfolioAsset).where(PortfolioAsset.niche_id == niche.id,
                                                       PortfolioAsset.active == True)).all()  # noqa: E712
    out = []
    for a in assets:
        if ncfg.get("is_medical") and (a.type == "before_after" or "before_after" in asset_tags(a)):
            continue
        out.append((a, len(asset_tags(a) & want)))
    return sorted(out, key=lambda x: (-x[1], x[0].id))


def main_asset(session: Session) -> PortfolioAsset | None:
    return session.exec(select(PortfolioAsset).where(PortfolioAsset.type == "main", PortfolioAsset.active == True)  # noqa: E712
                        .order_by(PortfolioAsset.id)).first()


def select_asset(session: Session, lead: Lead, tie_breaker=None) -> PortfolioAsset | None:  # noqa: ANN001
    """tie_breaker(lead, [assets]) -> asset; only called among equally-ranked eligible assets."""
    ranked = eligible_assets(session, lead)
    if not ranked:
        return main_asset(session)
    top = ranked[0][1]
    tied = [a for a, s in ranked if s == top]
    if len(tied) > 1 and tie_breaker is not None:
        choice = tie_breaker(lead, tied)
        if choice in tied:  # never accept something outside the eligible set
            return choice
    return tied[0]
