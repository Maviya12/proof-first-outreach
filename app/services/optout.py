"""Keyword opt-out detection. Runs BEFORE any LLM classifier (rule 9).

Deliberately biased to fail safe: a false positive suppresses a prospect (cheap); a false
negative contacts someone who said stop (unacceptable).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

UNSUBSCRIBE = "UNSUBSCRIBE"
NOT_INTERESTED = "NOT_INTERESTED"

# (pattern, kind). Patterns run on lowercased, apostrophe-normalised text.
_EN_UNSUB = [
    r"\bunsubscribe\b", r"\bstop\b", r"\bremove me\b", r"\bremove us\b", r"\btake me off\b",
    r"\btake us off\b", r"\bdo ?n[o']?t (?:contact|email|message|text|dm|reach out|write)\b",
    r"\bdo not (?:contact|email|message|text|dm|reach out|write)\b", r"\bopt[\s-]?out\b",
    r"\bleave (?:me|us) alone\b", r"\bno more (?:emails|messages|dms)\b", r"\bstop (?:emailing|messaging|texting)\b",
    r"\bspam\b", r"\breport(?:ed|ing)? (?:you|this)\b",
]
_EN_NOT_INTERESTED = [
    r"\bnot interested\b", r"\bno thanks?\b", r"\bno,? thank you\b", r"\bnot looking\b",
    r"\bnot (?:at this time|right now|needed)\b", r"\bpass on this\b", r"\bnot for us\b",
]
# Hinglish (romanised Hindi) — spelling varies a lot, so allow common variants
_HINGLISH_UNSUB = [
    r"\b(?:msg|message|mail|email|dm|contact|call) mat (?:karo|kro|karna|krna|bhejo|bhejna)\b",
    r"\bmat bhej(?:o|na|iye)\b", r"\bband kar(?:o|do|iye)\b", r"\bbandh kar(?:o|do)\b",
    r"\bdobara mat\b", r"\bphir se mat\b", r"\bmujhe mat\b", r"\bpareshan mat\b", r"\btang mat\b",
]
_HINGLISH_NOT_INTERESTED = [
    r"\b(?:interest|interested|intrest|intrested) nahi\b", r"\bnahi chahiye\b", r"\bnahi chaiye\b",
    r"\bnahi chahie\b", r"\bnhi chahiye\b", r"\bnai chahiye\b", r"\bzarurat nahi\b", r"\bzaroorat nahi\b",
    r"\bjarurat nahi\b", r"\bkoi zarurat nahi\b", r"\brehne do\b", r"\brehne dijiye\b",
    r"\bnahi karna\b", r"\bnahi lena\b",
]
# Marathi romanised
_MARATHI = [r"\bnako\b", r"\bpathvu naka\b", r"\bgaraj nahi\b"]
# Devanagari (Hindi / Marathi) — no \b for Devanagari, use substring
_DEVANAGARI_UNSUB = ["मत भेजो", "मत भेजिए", "बंद करो", "बंद करें", "संपर्क मत", "मैसेज मत", "पाठवू नका"]
_DEVANAGARI_NOT_INTERESTED = ["रुचि नहीं", "दिलचस्पी नहीं", "नहीं चाहिए", "ज़रूरत नहीं", "जरूरत नहीं",
                              "नको", "गरज नाही", "इंटरेस्ट नहीं"]
# A few other common languages
_OTHER_UNSUB = [r"\bdarse de baja\b", r"\bno me contactes\b", r"\bdésabonner\b", r"\bdesabonner\b",
                r"\babmelden\b", r"\bpare de\b"]
_OTHER_NOT_INTERESTED = [r"\bno me interesa\b", r"\bpas intéressé\b", r"\bkein interesse\b",
                         r"\bnão tenho interesse\b"]
_ARABIC_URDU = ["لا تتصل", "غير مهتم", "دلچسپی نہیں", "نہیں چاہیے"]


@dataclass(frozen=True)
class OptOut:
    kind: str  # UNSUBSCRIBE | NOT_INTERESTED
    matched: str


def _prep(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    # drop quoted history in email replies so our own text ("reply stop to opt out") never matches
    lines = []
    for line in t.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^on .+wrote:$", s) or s.startswith("-----original message"):
            break
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def detect_optout(text: str) -> OptOut | None:
    t = _prep(text)
    if not t:
        return None
    for pats, kind in ((_EN_UNSUB, UNSUBSCRIBE), (_HINGLISH_UNSUB, UNSUBSCRIBE), (_OTHER_UNSUB, UNSUBSCRIBE),
                       (_EN_NOT_INTERESTED, NOT_INTERESTED), (_HINGLISH_NOT_INTERESTED, NOT_INTERESTED),
                       (_MARATHI, NOT_INTERESTED), (_OTHER_NOT_INTERESTED, NOT_INTERESTED)):
        for p in pats:
            m = re.search(p, t)
            if m:
                return OptOut(kind, m.group(0))
    for phrase in _DEVANAGARI_UNSUB:
        if phrase in t:
            return OptOut(UNSUBSCRIBE, phrase)
    for phrase in _DEVANAGARI_NOT_INTERESTED + _ARABIC_URDU:
        if phrase in t:
            return OptOut(NOT_INTERESTED, phrase)
    return None
