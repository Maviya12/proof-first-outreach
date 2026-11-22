"""Environment-driven configuration. Secrets come only from env / .env."""
from __future__ import annotations

from datetime import time
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    model_judge: str = "claude-opus-5-5"
    model_extract: str = "claude-sonnet-5-5"
    model_classify: str = "claude-haiku-5-5"
    monthly_llm_budget_inr: float = 5000.0
    usd_to_inr: float = 88.0

    # Google
    google_places_api_key: str = ""
    places_discovery_enabled: bool = False
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://127.0.0.1:8000/api/v1/integrations/google/callback"
    youtube_api_key: str = ""

    # Security
    fernet_key: str = ""
    session_secret: str = "dev-only-change-me"
    admin_password_hash: str = ""
    sites_origin_token: str = ""

    # Sending & compliance
    send_enabled: bool = False
    allow_eu_uk: bool = False
    daily_cap_email: int = 15
    hourly_cap_email: int = 5
    min_gap_seconds_email: int = 180
    daily_cap_handoff: int = 25
    contact_lock_days: int = 90
    raw_retention_days: int = 30
    send_window: str = "10:00-18:00"
    timezone: str = "Asia/Kolkata"
    sender_name: str = ""
    sender_email: str = ""
    sender_contact_address: str = ""
    default_phone_region: str = "IN"

    fetch_user_agent: str = "ProofFirstOutreachBot/1.0 (+mailto:you@example.com)"

    # Meta (Phase 6)
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    meta_page_access_token: str = ""
    meta_inbound_enabled: bool = False
    meta_graph_version: str = "v21.0"

    # Storage
    database_url: str = "sqlite:///./data/outreach.db"
    upload_dir: str = "./data/uploads"
    jobs_enabled: bool = True

    followup_gaps_days: list[int] = Field(default_factory=lambda: [4, 9])

    @field_validator("send_window")
    @classmethod
    def _check_window(cls, v: str) -> str:
        parse_window(v)
        return v

    @property
    def window(self) -> tuple[time, time]:
        return parse_window(self.send_window)


def parse_window(v: str) -> tuple[time, time]:
    start_s, end_s = v.split("-")
    sh, sm = (int(x) for x in start_s.strip().split(":"))
    eh, em = (int(x) for x in end_s.strip().split(":"))
    start, end = time(sh, sm), time(eh, em)
    if start >= end:
        raise ValueError("SEND_WINDOW start must be before end (same day)")
    return start, end


@lru_cache
def get_settings() -> Settings:
    return Settings()
