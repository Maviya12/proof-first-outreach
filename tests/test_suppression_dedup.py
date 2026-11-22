from sqlmodel import select

from app.models import AuditLog, Message, MessageStatus, Suppression
from app.services.dedup import check_duplicate, fuzzy_org_matches
from app.services.suppression import (add_suppression, address_suppressed, hash_value, is_suppressed,
                                      lead_suppression_reasons, suppress_lead)


def test_suppression_is_hashed_and_normalised(session):
    assert add_suppression(session, "email", "  Owner@GlowSkin.in ", "manual")
    session.commit()
    row = session.exec(select(Suppression)).one()
    assert "glowskin" not in row.value_hash  # no plaintext stored
    assert row.value_hash == hash_value("email", "owner@glowskin.in")
    assert is_suppressed(session, "email", "OWNER@glowskin.IN")
    assert not is_suppressed(session, "email", "other@glowskin.in")


def test_duplicate_suppression_is_idempotent(session):
    assert add_suppression(session, "domain", "https://www.glowskin.in", "x")
    assert not add_suppression(session, "domain", "glowskin.in", "x")


def test_handle_suppression_across_url_forms(session):
    add_suppression(session, "instagram", "https://instagram.com/GlowSkin/", "stop")
    assert is_suppressed(session, "instagram", "@glowskin")
    assert not is_suppressed(session, "x", "@glowskin")  # platform-scoped


def test_domain_suppression_blocks_any_address_at_domain(session):
    add_suppression(session, "domain", "glowskin.in", "org opted out")
    assert address_suppressed(session, "email", "newperson@glowskin.in")
    assert address_suppressed(session, "email", "garbage")  # fail closed


def test_suppress_lead_covers_contacts_org_and_pending_messages(session, make_lead):
    lead = make_lead(instagram="@glowskin")
    m = Message(lead_id=lead.id, channel="email", status=MessageStatus.APPROVAL_PENDING)
    session.add(m)
    session.commit()
    suppress_lead(session, lead, "replied stop")
    session.commit()
    assert is_suppressed(session, "email", "hello@glowskin.in")
    assert is_suppressed(session, "instagram", "glowskin")
    assert is_suppressed(session, "domain", "glowskin.in")
    assert session.get(Message, m.id).status == MessageStatus.CANCELLED
    assert lead.status == "DO_NOT_CONTACT"
    assert session.exec(select(AuditLog).where(AuditLog.action == "lead.status")).first()


def test_new_lead_at_suppressed_org_is_blocked(session, make_lead):
    a = make_lead()
    suppress_lead(session, a, "stop")
    session.commit()
    b = make_lead(name="Glow Skin Studio Baner", domain="glowskin.in", email="baner@glowskin.in")
    reasons = lead_suppression_reasons(session, b)
    assert any("domain" in r for r in reasons)


def test_personal_email_domain_not_suppressed_wholesale(session, make_lead):
    lead = make_lead(name="Solo Creator", domain="gmail.com", email="creator@gmail.com")
    suppress_lead(session, lead, "stop")
    session.commit()
    assert is_suppressed(session, "email", "creator@gmail.com")
    assert not is_suppressed(session, "domain", "gmail.com")


# ---------- dedup ----------

def test_exact_dedup_by_domain_place_and_contact(session, make_lead):
    lead = make_lead()
    org = session.get(type(lead).__mro__[0], lead.id)  # noqa: F841
    from app.models import Organization
    o = session.get(Organization, lead.organization_id)
    o.google_place_id = "ChIJ123"
    session.add(o)
    session.commit()

    assert check_duplicate(session, name="x", domain="https://www.glowskin.in/?utm_source=a").matched_on == "domain"
    assert check_duplicate(session, name="x", place_id="ChIJ123").matched_on == "google_place_id"
    r = check_duplicate(session, name="x", contacts=[("email", "HELLO@glowskin.in")])
    assert r.existing_lead_by_contact.id == lead.id


def test_fuzzy_dedup_flags_same_city_only(session, make_lead):
    make_lead(name="Glow Skin Studio", domain="glowskin.in", city="Pune")
    assert fuzzy_org_matches(session, "The Glow Skin Studio Pvt Ltd", "Pune")
    assert not fuzzy_org_matches(session, "The Glow Skin Studio", "Mumbai")
    assert not fuzzy_org_matches(session, "Urban Realty", "Pune")
    r = check_duplicate(session, name="Glow Skin Studio", city="Pune", domain="glowskinpune.com")
    assert not r.is_exact_duplicate and r.fuzzy_candidates  # flagged, not merged
