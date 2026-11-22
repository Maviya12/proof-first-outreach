"""Deterministic draft validator (spec 6.4 step 2). No LLM here."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.models import EMAIL_CHANNEL, Evidence

LIMITS = {"email": (70, 120), "dm": (40, 80)}
BANNED_PHRASES = [
    "i hope this finds you well", "hope this finds you well", "hope you are doing well", "hope you're doing well",
    "game-changer", "game changer", "guaranteed", "guarantee", "skyrocket", "10x", "go viral", "explode your",
    "synergy", "touch base", "circle back", "quick question", "just checking in", "dear sir/madam", "dear sir or madam",
    "to whom it may concern", "revolutionize", "revolutionise", "unlock your", "take it to the next level",
    "i noticed your content is bad", "your content is bad", "your videos are bad", "poor quality",
]
PRESSURE = ["urgent", "limited time", "act now", "today only", "last chance", "hurry", "only a few spots",
            "don't miss out", "dont miss out", "expires", "before it's too late", "asap"]
FAKE_FAMILIARITY = ["as we discussed", "as discussed", "great to reconnect", "following up on our call",
                    "as promised", "per our conversation", "nice chatting", "good to see you again", "long time"]
FLATTERY = ["amazing", "incredible", "awesome", "stunning", "huge fan", "big fan", "love your", "loved your",
            "obsessed", "fantastic", "brilliant", "world-class", "best in"]
MEDICAL_TERMS = re.compile(
    r"\b(results?|before[- /]and[- /]after|before/after|transformations?|patients?|cure[sd]?|heal(s|ing)?|"
    r"treat(s|ed|ing)?|painless|safe(ly|st)?|anti-?aging|younger|flawless|clinically|outcomes?|recover(y|ies)?|"
    r"side[- ]effects?|diagnos\w*|wrinkles?|acne|weight loss|fda|risk[- ]free)\b", re.I)
SAMPLE_OFFER = re.compile(r"\b(free (sample|re-?edit|edit)|sample (re-?edit|edit)|re-?edit (one|a) of your)\b", re.I)
SAMPLE_PRIVACY = re.compile(r"\b(privately|won'?t (post|share|reuse)|not (post|share|reuse)|delete)\b", re.I)
URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)
PERSONAL_MARKERS = re.compile(r"\b(your|you've|you're|you have|you post|you run|you share|i saw|i noticed|i came across|"
                              r"i watched|i see|i liked|i enjoyed|caught my eye)\b", re.I)
OFFER_MARKERS = re.compile(r"\b(i edit|i'm a|i am a|i make|i help|i cut|i'd|i would|i can|happy to|here'?s|my work|"
                           r"portfolio|samples?|would you|are you open|open to|worth a|could i|shall i|if useful|"
                           r"reply|opt out|stop)\b", re.I)


@dataclass
class Claim:
    text: str
    evidence_ids: list[int]


@dataclass
class ValidationResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    word_count: int = 0
    unsupported_sentences: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "word_count": self.word_count,
                "unsupported_sentences": self.unsupported_sentences}


CITABLE_KINDS_EXCLUDED = {"opportunity"}


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9ऀ-ॿ'’]+", URL_RE.sub("", text)))


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", URL_RE.sub(" ", text))
    return [p.strip() for p in parts if len(p.strip()) > 3]


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", s))


def _supported(claim: Claim, ev: dict[int, Evidence]) -> bool:
    """Light grounding: the claim shares content with at least one cited snippet."""
    for eid in claim.evidence_ids:
        e = ev.get(eid)
        if e and (fuzz.token_set_ratio(claim.text.lower(), e.snippet.lower()) >= 45
                  or fuzz.partial_ratio(claim.text.lower(), e.snippet.lower()) >= 60):
            return True
    return False


def validate_draft(*, body: str, subject: str | None, channel: str, claims: list[Claim], evidence: dict[int, Evidence],
                   portfolio_url: str | None, is_medical: bool, niche_banned: list[str] | None = None,
                   sample_offer_enabled: bool = False, kind: str = "initial", human_edit: bool = False,
                   extra_allowed_urls: list[str] | None = None) -> ValidationResult:
    r = ValidationResult()
    text = body or ""
    low = text.lower().replace("’", "'")
    r.word_count = word_count(text)

    # --- length ---
    lo, hi = LIMITS["email" if channel == EMAIL_CHANNEL else "dm"]
    if kind == "reply":
        lo = 15
    if not lo <= r.word_count <= hi:
        r.error(f"length {r.word_count} words; must be {lo}-{hi} for {channel}")

    # --- subject ---
    if channel == EMAIL_CHANNEL and kind != "reply":
        if not subject or not subject.strip():
            r.error("email needs a subject")
        elif len(subject) > 70:
            r.error("subject over 70 characters")
        elif re.match(r"^\s*(re|fwd?):", subject, re.I) and kind == "initial":
            r.error("deceptive 'Re:/Fwd:' subject on a first email")
    elif channel != EMAIL_CHANNEL and subject:
        r.warnings.append("subject ignored for DM channels")

    # --- portfolio link ---
    urls = [u.rstrip(".,;!?") for u in URL_RE.findall(text)]
    allowed = {u for u in [portfolio_url, *(extra_allowed_urls or [])] if u}
    if kind != "reply":
        if not portfolio_url:
            r.error("no portfolio asset selected")
        elif portfolio_url not in urls:
            r.error("portfolio URL missing or altered")
    for u in urls:
        if u not in allowed:
            r.error(f"unexpected URL {u}")

    # --- banned / pressure / familiarity ---
    for p in BANNED_PHRASES + [b.lower() for b in (niche_banned or []) if not is_medical]:
        if p in low:
            r.error(f"banned phrase: '{p}'")
    for p in PRESSURE:
        if p in low:
            r.error(f"pressure tactic: '{p}'")
    if kind == "initial":
        for p in FAKE_FAMILIARITY:
            if p in low:
                r.error(f"fake familiarity: '{p}'")

    # --- medical niche ---
    if is_medical:
        hits = sorted({m.group(0).lower() for m in MEDICAL_TERMS.finditer(text)})
        if hits:
            r.error("medical niche: health/outcome wording not allowed: " + ", ".join(hits))
        for b in niche_banned or []:
            if b.lower() in low and b.lower() not in hits:
                r.error(f"medical niche banned claim: '{b}'")

    # --- sample offer ---
    if SAMPLE_OFFER.search(text):
        if not sample_offer_enabled:
            r.error("sample re-edit offered but sample offers are disabled in settings")
        elif not SAMPLE_PRIVACY.search(text):
            r.error("sample offer must say it's sent privately and never posted/reused")

    # --- call to action ---
    q = text.count("?")
    if kind != "reply":
        if q == 0:
            r.error("no call to action (one low-pressure question expected)")
        elif q > 1:
            r.error(f"{q} questions; keep exactly one call to action")

    # --- evidence grounding (rule 4) ---
    if kind != "reply":
        valid_claims = []
        if not claims and not human_edit:
            r.error("no evidence-backed claim")
        for c in claims:
            bad = [i for i in c.evidence_ids if i not in evidence or evidence[i].kind in CITABLE_KINDS_EXCLUDED]
            if not c.evidence_ids or bad:
                r.error(f"claim cites missing/non-factual evidence {bad or '[]'}: '{c.text[:60]}'")
                continue
            if not _supported(c, evidence):
                r.error(f"claim not supported by its evidence: '{c.text[:60]}'")
                continue
            valid_claims.append(c)
        cited_text = " ".join(evidence[i].snippet for c in valid_claims for i in c.evidence_ids if i in evidence)
        for s in sentences(text):
            if not PERSONAL_MARKERS.search(s):
                continue
            covered = any(fuzz.partial_ratio(c.text.lower(), s.lower()) >= 70 or
                          fuzz.partial_ratio(s.lower(), c.text.lower()) >= 70 for c in valid_claims)
            if covered:
                stray = _numbers(s) - _numbers(cited_text)
                if stray:
                    r.error(f"number(s) {sorted(stray)} not found in cited evidence: '{s[:60]}'")
                continue
            if OFFER_MARKERS.search(s) and not re.search(r"\b(i saw|i noticed|i watched|caught my eye)\b", s, re.I):
                continue  # offer/CTA sentence, not a claim about them
            r.unsupported_sentences.append(s)
        if r.unsupported_sentences:
            msg = "personalised sentence(s) without evidence: " + " | ".join(x[:60] for x in r.unsupported_sentences)
            if human_edit:
                r.warnings.append(msg)
            else:
                r.error(msg)
        # flattery only allowed inside an evidence-backed claim
        for f in FLATTERY:
            if f in low and not any(f in c.text.lower() for c in valid_claims):
                (r.warnings if human_edit else r.errors).append(f"unsupported flattery: '{f}'")
                if not human_edit:
                    r.ok = False
    return r
