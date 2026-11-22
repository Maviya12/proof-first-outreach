"""Niche configs: search queries, rubric, playbook, portfolio tags, banned claims."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, select

from app.models import Niche

MEDICAL_BANNED = ["results", "before and after", "before/after", "transformation", "cure", "treat",
                  "heal", "safe", "painless", "guaranteed results", "anti-aging", "younger", "flawless",
                  "patients love", "clinically", "fda", "risk-free", "permanent"]

DEFAULT_RUBRIC = {"niche_match": 25, "location": 15, "contactability": 20, "video_activity": 25, "intent_signal": 15}

SEED: list[dict[str, Any]] = [
    {"slug": "medspa", "name": "Medspas & aesthetic clinics", "config": {
        "is_medical": True,
        "search_queries": ["medspa", "aesthetic clinic", "skin clinic", "laser clinic"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Talk only about editing craft: hooks, pacing, captions, brand consistency, turnaround. "
                    "Never mention treatments, outcomes, patients or before/after.",
        "portfolio_tags": ["reels", "brand", "talking_head", "captions"],
        "banned_claims": MEDICAL_BANNED}},
    {"slug": "clinic", "name": "Clinics (dental, physio, wellness)", "config": {
        "is_medical": True,
        "search_queries": ["dental clinic", "physiotherapy clinic", "dermatologist"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Editing craft only: educational reels, doctor talking-head clips, captions, consistency.",
        "portfolio_tags": ["talking_head", "educational", "reels", "captions"],
        "banned_claims": MEDICAL_BANNED}},
    {"slug": "realtor", "name": "Realtors & property developers", "config": {
        "is_medical": False,
        "search_queries": ["real estate agent", "property dealer", "real estate developer"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Property walkthroughs, listing reels, agent intro videos, drone/cinematic cuts.",
        "portfolio_tags": ["walkthrough", "cinematic", "reels", "listing"],
        "banned_claims": ["guaranteed sale", "sell faster guaranteed", "double your"]}},
    {"slug": "creator", "name": "Creators & coaches", "config": {
        "is_medical": False,
        "search_queries": [],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Short-form repurposing, hooks, captions, retention editing, long-to-short clipping.",
        "portfolio_tags": ["reels", "youtube", "podcast_clips", "captions", "hooks"],
        "banned_claims": ["go viral", "guaranteed views", "million views"]}},
    {"slug": "agency", "name": "Marketing agencies", "config": {
        "is_medical": False,
        "search_queries": ["digital marketing agency", "social media agency"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "White-label overflow editing capacity, turnaround, consistency across client brands.",
        "portfolio_tags": ["reels", "ads", "brand", "variety"],
        "banned_claims": ["guaranteed roi", "guaranteed views"]}},
    {"slug": "restaurant", "name": "Restaurants & cafes", "config": {
        "is_medical": False,
        "search_queries": ["restaurant", "cafe", "cloud kitchen"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Menu reels, dish close-ups, behind-the-counter clips, event/launch promos.",
        "portfolio_tags": ["reels", "cinematic", "food"],
        "banned_claims": ["guaranteed footfall", "double your sales"]}},
    {"slug": "fitness", "name": "Gyms & fitness studios", "config": {
        "is_medical": False,
        "search_queries": ["gym", "fitness studio", "yoga studio"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Class promos, trainer intros, workout tips. No body-transformation or weight-loss claims.",
        "portfolio_tags": ["reels", "talking_head", "captions"],
        "banned_claims": ["transformation", "lose weight", "weight loss", "before and after", "guaranteed results"]}},
    {"slug": "coaching", "name": "Coaching institutes & educators", "config": {
        "is_medical": False,
        "search_queries": ["coaching institute", "tuition classes", "training institute"],
        "rubric": DEFAULT_RUBRIC,
        "playbook": "Faculty explainers, topper interviews (with consent), course promos, short concept clips.",
        "portfolio_tags": ["educational", "talking_head", "captions", "reels"],
        "banned_claims": ["guaranteed selection", "100% results", "guaranteed rank"]}},
]


def seed_niches(session: Session) -> int:
    n = 0
    for item in SEED:
        if session.exec(select(Niche).where(Niche.slug == item["slug"])).first():
            continue
        session.add(Niche(slug=item["slug"], name=item["name"], config_json=json.dumps(item["config"])))
        n += 1
    return n


def niche_config(niche: Niche | None) -> dict[str, Any]:
    if niche is None:
        return {"is_medical": False, "rubric": DEFAULT_RUBRIC, "portfolio_tags": [], "banned_claims": [],
                "playbook": "", "search_queries": []}
    cfg = json.loads(niche.config_json or "{}")
    cfg.setdefault("rubric", DEFAULT_RUBRIC)
    cfg.setdefault("banned_claims", [])
    cfg.setdefault("portfolio_tags", [])
    cfg.setdefault("is_medical", False)
    return cfg
