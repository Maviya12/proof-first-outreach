"""Injectable external clients. Tests override these with fakes."""
from __future__ import annotations

from fastapi import Depends
from sqlmodel import Session

from app.db import get_session
from app.llm import LLM


def get_llm(session: Session = Depends(get_session)) -> LLM:
    return LLM(session)


def get_fetcher():
    from app.services.enrichment import PoliteFetcher
    return PoliteFetcher()


def get_places():
    from app.config import get_settings
    from app.services.places import PlacesClient
    return PlacesClient(get_settings().google_places_api_key)


def get_mailer():
    """Gmail client, or None when not connected."""
    from app.services.gmail import load_gmail
    return load_gmail()
