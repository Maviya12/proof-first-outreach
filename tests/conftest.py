from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

# Test env BEFORE app imports read settings
os.environ.update({
    "DATABASE_URL": "sqlite://",
    "JOBS_ENABLED": "false",
    "SEND_ENABLED": "false",
    "ANTHROPIC_API_KEY": "test-key",
    "FERNET_KEY": "x6dh6kJr1W8n0j1gNtYQ3rjO7T0N0U2wA1zqgR3m6dE=",
    "SESSION_SECRET": "test-secret",
    "SENDER_NAME": "Asha Editor",
    "SENDER_EMAIL": "asha@example.com",
    "SENDER_CONTACT_ADDRESS": "12 MG Road, Pune 411001, India",
    "UPLOAD_DIR": "/tmp/outreach-test-uploads",
})

from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    import app.models  # noqa: F401
    SQLModel.metadata.create_all(eng)
    db.set_engine(eng)
    yield eng
    db.set_engine(None)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def cfg():
    return get_settings().model_copy()


@pytest.fixture
def client(engine):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.security import hash_password
    s = get_settings()
    s.admin_password_hash = hash_password("correct horse battery")
    app = create_app(testing=True)
    with TestClient(app) as c:
        r = c.post("/login", data={"password": "correct horse battery"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


class FakeAnthropic:
    """Queue of canned responses; records every request."""

    def __init__(self, responses: list | None = None):
        self.responses = list(responses or [])
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def queue(self, *items):
        self.responses.extend(items)
        return self

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeAnthropic: no response queued")
        item = self.responses.pop(0)
        if callable(item):
            item = item(kwargs)
        text = item if isinstance(item, str) else json.dumps(item)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


@pytest.fixture
def fake_llm():
    return FakeAnthropic()


# ---------- factories ----------

@pytest.fixture
def make_lead(session):
    from app.models import ContactPoint, Lead, Organization
    from app.services.normalize import normalize_contact

    def _make(name="Glow Skin Studio", domain="glowskin.in", city="Pune", country="IN", email="hello@glowskin.in",
              niche_id=None, status="NEW", instagram=None):
        org = Organization(name=name, domain_norm=domain, city=city, country=country)
        session.add(org)
        session.flush()
        lead = Lead(organization_id=org.id, niche_id=niche_id, website=f"https://{domain}" if domain else None,
                    status=status, source="test")
        session.add(lead)
        session.flush()
        if email:
            session.add(ContactPoint(lead_id=lead.id, type="email", value_raw=email,
                                     value_norm=normalize_contact("email", email)))
        if instagram:
            session.add(ContactPoint(lead_id=lead.id, type="instagram", value_raw=instagram,
                                     value_norm=normalize_contact("instagram", instagram)))
        session.commit()
        return lead
    return _make
