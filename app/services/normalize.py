"""Deterministic normalisation for dedup and suppression lookups."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers

TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "igsh", "ref", "ref_src", "mc_cid", "mc_eid",
                   "_hsenc", "_hsmi", "si", "s", "msclkid", "yclid"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

SOCIAL_HOSTS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com"),
    "x": ("x.com", "twitter.com"),
    "linkedin": ("linkedin.com",),
    "youtube": ("youtube.com", "youtu.be"),
}
# Path segments that are never a profile handle
_RESERVED = {"p", "reel", "reels", "stories", "explore", "share", "sharer", "watch", "intent",
             "home", "hashtag", "search", "tv", "shorts", "embed", "login", "pages"}


def _ensure_scheme(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url
    return url


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop www, tracking params, fragment and trailing slash."""
    if not url or not url.strip():
        return ""
    parts = urlsplit(_ensure_scheme(url))
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(query), ""))


def normalize_domain(url_or_domain: str) -> str:
    if not url_or_domain or not url_or_domain.strip():
        return ""
    value = url_or_domain.strip()
    if "@" in value and "://" not in value:
        value = value.split("@", 1)[1]
    host = (urlsplit(_ensure_scheme(value)).hostname or "").lower()
    return host.removeprefix("www.").rstrip(".")


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    e = e.removeprefix("mailto:").split("?", 1)[0]
    if not EMAIL_RE.match(e):
        raise ValueError(f"invalid email: {email!r}")
    return e


def email_domain(email: str) -> str:
    return normalize_email(email).split("@", 1)[1]


def normalize_phone(phone: str, region: str = "IN") -> str:
    try:
        num = phonenumbers.parse(phone, region)
    except phonenumbers.NumberParseException as e:
        raise ValueError(f"invalid phone: {phone!r}") from e
    if not phonenumbers.is_possible_number(num):
        raise ValueError(f"invalid phone: {phone!r}")
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def normalize_handle(platform: str, value: str) -> str:
    """Return canonical handle (lowercase, no @) for a social profile URL or bare handle.

    LinkedIn keeps its namespace ("company/acme", "in/jane-doe"); YouTube keeps "@handle"
    or "channel/UC...".
    """
    platform = platform.lower()
    if platform == "twitter":
        platform = "x"
    v = (value or "").strip()
    if not v:
        raise ValueError("empty handle")
    looks_like_url = "/" in v or any(h in v.lower() for h in SOCIAL_HOSTS.get(platform, ()))
    if not looks_like_url:
        h = v.lstrip("@").lower()
        if platform == "youtube":
            return "@" + h
        if not re.match(r"^[a-z0-9._\-]+$", h):
            raise ValueError(f"invalid handle: {value!r}")
        return h

    parts = urlsplit(_ensure_scheme(v))
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    allowed = SOCIAL_HOSTS.get(platform, ())
    if allowed and not any(host == a or host.endswith("." + a) for a in allowed):
        raise ValueError(f"{value!r} is not a {platform} URL")
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        raise ValueError(f"no handle in {value!r}")

    if platform == "linkedin":
        if len(segs) >= 2 and segs[0] in {"company", "in", "school", "showcase"}:
            return f"{segs[0]}/{segs[1].lower()}"
        raise ValueError(f"unrecognised LinkedIn URL {value!r}")
    if platform == "youtube":
        if segs[0].startswith("@"):
            return segs[0].lower()
        if segs[0] in {"channel", "c", "user"} and len(segs) >= 2:
            return f"{segs[0]}/{segs[1]}" if segs[0] == "channel" else "@" + segs[1].lower()
        raise ValueError(f"unrecognised YouTube URL {value!r}")
    if platform == "facebook" and segs[0] == "profile.php":
        qs = dict(parse_qsl(parts.query))
        if "id" in qs:
            return f"id:{qs['id']}"
    handle = segs[0].lstrip("@").lower()
    if handle in _RESERVED:
        raise ValueError(f"{value!r} is a post/page link, not a profile")
    return handle


def normalize_contact(type_: str, value: str, region: str = "IN") -> str:
    t = type_.lower()
    if t == "email":
        return normalize_email(value)
    if t in {"phone", "whatsapp"}:
        return normalize_phone(value, region)
    if t == "website":
        return normalize_url(value)
    if t in {"instagram", "facebook", "x", "twitter", "linkedin", "youtube"}:
        return normalize_handle(t, value)
    if t in {"ig_psid", "fb_psid"}:  # Meta page-scoped ids from inbound webhooks (Phase 6)
        v = value.strip()
        if not v.isdigit():
            raise ValueError(f"invalid {t}: {value!r}")
        return v
    raise ValueError(f"unknown contact type {type_!r}")


def profile_url(type_: str, handle: str) -> str:
    """Public profile URL for the handoff 'Open profile' button."""
    t = type_.lower()
    if t == "instagram":
        return f"https://www.instagram.com/{handle}/"
    if t == "facebook":
        return f"https://www.facebook.com/profile.php?id={handle[3:]}" if handle.startswith("id:") \
            else f"https://www.facebook.com/{handle}"
    if t == "x":
        return f"https://x.com/{handle}"
    if t == "linkedin":
        return f"https://www.linkedin.com/{handle}/"
    if t == "youtube":
        return f"https://www.youtube.com/{handle}"
    if t == "whatsapp":
        return f"https://wa.me/{handle.lstrip('+')}"
    return handle
