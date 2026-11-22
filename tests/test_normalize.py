import pytest

from app.services.normalize import (normalize_domain, normalize_email, normalize_handle, normalize_phone,
                                    normalize_url, profile_url)


@pytest.mark.parametrize("raw,expected", [
    ("https://www.GlowSkin.in/?utm_source=ig&utm_medium=bio", "https://glowskin.in"),
    ("glowskin.in/about/", "https://glowskin.in/about"),
    ("http://glowskin.in/services?id=3&fbclid=abc#top", "https://glowskin.in/services?id=3"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("https://www.glowskin.in/contact", "glowskin.in"),
    ("WWW.GLOWSKIN.IN", "glowskin.in"),
    ("hello@glowskin.in", "glowskin.in"),
    ("clinic.example.co.uk", "clinic.example.co.uk"),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_normalize_email():
    assert normalize_email("  Hello@GlowSkin.IN ") == "hello@glowskin.in"
    assert normalize_email("mailto:info@x.com?subject=hi") == "info@x.com"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")


@pytest.mark.parametrize("raw", ["+91 98765 43210", "098765 43210", "9876543210", "+91-98765-43210"])
def test_normalize_phone_india(raw):
    assert normalize_phone(raw, "IN") == "+919876543210"


def test_normalize_phone_invalid():
    with pytest.raises(ValueError):
        normalize_phone("12", "IN")


@pytest.mark.parametrize("platform,raw,expected", [
    ("instagram", "@GlowSkin.Pune", "glowskin.pune"),
    ("instagram", "https://www.instagram.com/glowskin.pune/?igsh=abc", "glowskin.pune"),
    ("instagram", "instagram.com/GlowSkin.Pune", "glowskin.pune"),
    ("x", "https://twitter.com/GlowSkin", "glowskin"),
    ("x", "https://x.com/glowskin/status/123", "glowskin"),
    ("linkedin", "https://www.linkedin.com/company/Glow-Skin/about/", "company/glow-skin"),
    ("linkedin", "https://in.linkedin.com/in/Jane-Doe", "in/jane-doe"),
    ("facebook", "https://m.facebook.com/GlowSkinPune/", "glowskinpune"),
    ("facebook", "https://www.facebook.com/profile.php?id=10001", "id:10001"),
    ("youtube", "https://www.youtube.com/@GlowSkin", "@glowskin"),
    ("youtube", "https://www.youtube.com/channel/UCabc123", "channel/UCabc123"),
])
def test_normalize_handle(platform, raw, expected):
    assert normalize_handle(platform, raw) == expected


def test_handle_rejects_post_links_and_wrong_host():
    with pytest.raises(ValueError):
        normalize_handle("instagram", "https://www.instagram.com/p/Cxyz/")
    with pytest.raises(ValueError):
        normalize_handle("instagram", "https://facebook.com/glowskin")


def test_profile_url():
    assert profile_url("instagram", "glowskin") == "https://www.instagram.com/glowskin/"
    assert profile_url("linkedin", "company/glow") == "https://www.linkedin.com/company/glow/"
