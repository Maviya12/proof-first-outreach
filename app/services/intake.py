"""Public Profile Intake (spec 5): human-fed social analysis, no scraping.

The user supplies a public profile URL, pasted bio/captions, optional dates and notes, and up to
8 screenshots. Opus (vision) returns checkable observations; plain code validates every
reference, drops unsafe observations, and stores the rest as evidence rows.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlmodel import Session

from app.audit import audit
from app.config import Settings, get_settings
from app.llm import LLM, image_block, load_prompt
from app.models import Lead, RawFile, utcnow
from app.services.leads import add_contact_point, add_evidence
from app.services.normalize import normalize_handle

MAX_SCREENSHOTS = 8
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_CAPTIONS = 15
PLATFORMS = {"instagram", "linkedin", "facebook", "x", "youtube"}
# observations mentioning these are dropped (health/outcome speculation or describing people)
UNSAFE_OBSERVATION = re.compile(
    r"\b(patient|patients|diagnos\w*|condition|disease|acne|wrinkle\w*|weight|scar\w*|before[- /]and[- /]after|"
    r"before/after|result(s)? (look|appear)|treatment (worked|results)|looks? (younger|older|thin|fat)|"
    r"revenue|profit|income|followers? (grew|growth)|struggl\w*|desperate|minor|child(ren)?|kid(s)?)\b", re.I)


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    return None


class Observation(BaseModel):
    text: str
    kind: Literal["style", "cadence", "format", "captioning", "hook", "branding", "audio", "other"] = "other"
    evidence_ref: str = Field(description="screenshot:N | caption:N | bio | note")
    confidence: float = Field(ge=0, le=1, default=0.7)


class Opportunity(BaseModel):
    idea: str
    supports: list[int] = []


class AnalysisOut(BaseModel):
    observations: list[Observation] = []
    opportunities: list[Opportunity] = []
    unknown: list[str] = []
    content_types: list[str] = []
    posting_cadence: str | None = None
    visual_style: str | None = None
    brand_consistency: str | None = None
    sufficient: bool = True


@dataclass
class IntakeInput:
    platform: str
    profile_url: str
    bio: str = ""
    captions: list[str] = field(default_factory=list)
    dates_note: str = ""
    user_note: str = ""
    video_urls: list[str] = field(default_factory=list)
    screenshots: list[tuple[str, bytes]] = field(default_factory=list)  # (filename, bytes)


def split_captions(text: str) -> list[str]:
    """Captions are separated by a line with --- or by blank lines."""
    if not text.strip():
        return []
    parts = re.split(r"\n\s*-{3,}\s*\n|\n\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()][:MAX_CAPTIONS]


def run_intake(session: Session, lead: Lead, data: IntakeInput, *, llm: LLM, cfg: Settings | None = None,
               actor: str = "admin") -> dict:
    cfg = cfg or get_settings()
    platform = data.platform.lower()
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {sorted(PLATFORMS)}")
    normalize_handle(platform, data.profile_url)  # raises on non-profile URLs
    if len(data.screenshots) > MAX_SCREENSHOTS:
        raise ValueError(f"at most {MAX_SCREENSHOTS} screenshots")
    captions = data.captions[:MAX_CAPTIONS]
    if not (data.bio.strip() or captions or data.screenshots):
        raise ValueError("supply a bio, captions or screenshots")

    add_contact_point(session, lead, platform, data.profile_url, source_url=data.profile_url)

    # ---- store raw inputs ----
    shot_dir = Path(cfg.upload_dir) / "screens" / str(lead.id)
    shot_dir.mkdir(parents=True, exist_ok=True)
    shots: list[tuple[int, str, Path]] = []  # (n, original name, path)
    for n, (name, blob) in enumerate(data.screenshots, start=1):
        if len(blob) > MAX_SCREENSHOT_BYTES:
            raise ValueError(f"{name}: larger than 5 MB")
        ext = _sniff_image(blob)
        if not ext:
            raise ValueError(f"{name}: only PNG or JPG allowed")
        p = shot_dir / f"{uuid.uuid4().hex}{ext}"
        p.write_bytes(blob)
        session.add(RawFile(lead_id=lead.id, kind="screenshot", path=str(p)))
        shots.append((n, name, p))

    ref_to_evidence: dict[str, int] = {}
    if data.bio.strip():
        ev = add_evidence(session, lead, kind="bio", source_type="pasted_text", source_url=data.profile_url,
                          source_ref=f"{platform}:bio", snippet=data.bio.strip())
        ref_to_evidence["bio"] = ev.id
    for i, cap in enumerate(captions, start=1):
        ev = add_evidence(session, lead, kind="caption", source_type="pasted_text", source_url=data.profile_url,
                          source_ref=f"{platform}:caption:{i}", snippet=cap)
        ref_to_evidence[f"caption:{i}"] = ev.id
    if data.user_note.strip():
        ev = add_evidence(session, lead, kind="note", source_type="user_note", source_url=data.profile_url,
                          source_ref=f"{platform}:note", snippet=data.user_note.strip())
        ref_to_evidence["note"] = ev.id
    for url in data.video_urls:
        if url.strip():
            add_evidence(session, lead, kind="note", source_type="user_note", source_url=url.strip(),
                         source_ref=f"{platform}:video_link", snippet=f"Public video link supplied: {url.strip()}")

    # ---- analysis (Opus, vision) ----
    content: list[dict] = [{"type": "text", "text": f"Platform: {platform}\nProfile: {data.profile_url}"}]
    if data.bio.strip():
        content.append({"type": "text", "text": f"[bio]\n{data.bio.strip()}"})
    for i, cap in enumerate(captions, start=1):
        content.append({"type": "text", "text": f"[caption:{i}]\n{cap}"})
    if data.dates_note.strip():
        content.append({"type": "text", "text": f"[posting dates supplied]\n{data.dates_note.strip()}"})
    if data.user_note.strip():
        content.append({"type": "text", "text": f"[note]\n{data.user_note.strip()}"})
    for n, _, p in shots:
        content.append({"type": "text", "text": f"[screenshot:{n}]"})
        content.append(image_block(p))

    out = llm.json(task="profile_analysis", model=cfg.model_judge, system=load_prompt("analysis_system.md"),
                   content=content, schema=AnalysisOut, max_tokens=2500)

    valid_refs = set(ref_to_evidence) | {f"screenshot:{n}" for n, _, _ in shots}
    have_dates = bool(data.dates_note.strip())
    kept: dict[int, int] = {}  # observation index -> evidence id
    dropped: list[dict] = []
    for idx, ob in enumerate(out.observations):
        ref = ob.evidence_ref.strip().lower().replace(" ", "")
        if ref not in valid_refs:
            dropped.append({"text": ob.text, "why": f"unknown reference '{ob.evidence_ref}'"})
            continue
        if ob.kind == "cadence" and not have_dates:
            dropped.append({"text": ob.text, "why": "cadence without supplied dates"})
            continue
        if UNSAFE_OBSERVATION.search(ob.text):
            dropped.append({"text": ob.text, "why": "speculative/sensitive content"})
            continue
        if ref.startswith("screenshot:"):
            n = int(ref.split(":")[1])
            fname = next(name for k, name, _ in shots if k == n)
            ev = add_evidence(session, lead, kind="screenshot_observation", source_type="screenshot",
                              source_url=data.profile_url, source_ref=f"{platform}:screenshot:{n}:{fname}",
                              snippet=ob.text, confidence=ob.confidence)
        else:
            ev = add_evidence(session, lead, kind="profile_observation", source_type="pasted_text",
                              source_url=data.profile_url, source_ref=f"{platform}:{ref}", snippet=ob.text,
                              confidence=ob.confidence)
        kept[idx] = ev.id

    opportunities = []
    for op in out.opportunities[:4]:
        ev_ids = [kept[i] for i in op.supports if i in kept]
        if not ev_ids or UNSAFE_OBSERVATION.search(op.idea):
            continue
        add_evidence(session, lead, kind="opportunity", source_type="analysis", source_url=data.profile_url,
                     source_ref="supports:" + ",".join(f"E{e}" for e in ev_ids), snippet=op.idea, confidence=0.6)
        opportunities.append({"idea": op.idea, "evidence_ids": ev_ids})

    sufficient = out.sufficient and len(kept) >= 1
    lead.intake_status = "SUFFICIENT" if sufficient else "INSUFFICIENT"
    if not sufficient:
        lead.needs_review = "Intake insufficient — add more captions/screenshots: " + "; ".join(out.unknown[:3])
    lead.updated_at = utcnow()
    session.add(lead)
    audit(session, "intake.analyse", actor=actor, entity="lead", entity_id=lead.id, platform=platform,
          observations=len(kept), dropped=len(dropped), sufficient=sufficient)
    return {
        "sufficient": sufficient,
        "observations": [{"evidence_id": kept[i], **out.observations[i].model_dump()} for i in sorted(kept)],
        "opportunities": opportunities,
        "dropped": dropped,
        "unknown": out.unknown,
        "content_types": out.content_types,
        "posting_cadence": out.posting_cadence if have_dates else None,
        "visual_style": out.visual_style,
        "brand_consistency": out.brand_consistency,
    }
