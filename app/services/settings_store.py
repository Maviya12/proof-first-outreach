"""User-editable settings stored in the `settings` table as JSON values."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, select

from app.models import SettingRow

DEFAULTS: dict[str, Any] = {
    "tone": "friendly",                 # friendly | formal
    "language": "English",              # English | Hinglish
    "sample_offer_enabled": False,
    "packages": [],                     # [{"name": "...", "description": "...", "price": "..."}]
    "rates": {},                        # {"per_reel": "INR 1500", ...}
    "niche_focus": "",
    "country_focus": "IN",
    "booking_link": "",
    "signature": "",
    "quality_threshold": 55,
    "approval_required_for_thread_replies": True,
    "followup_gaps_days": [4, 9],
    "max_followups": 2,
}


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(SettingRow, key)
    if row is None:
        return DEFAULTS.get(key, default) if default is None else default
    return json.loads(row.value)


def set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(SettingRow, key)
    if row is None:
        row = SettingRow(key=key, value=json.dumps(value))
    else:
        row.value = json.dumps(value)
    session.add(row)


def all_settings(session: Session) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for row in session.exec(select(SettingRow)):
        out[row.key] = json.loads(row.value)
    return out
