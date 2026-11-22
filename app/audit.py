"""Append-only audit log for approvals, sends, status changes, suppression edits."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session

from app.models import AuditLog


def audit(session: Session, action: str, *, actor: str = "system", entity: str | None = None,
          entity_id: int | None = None, **detail: Any) -> AuditLog:
    row = AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id,
                   detail_json=json.dumps(detail, default=str) if detail else None)
    session.add(row)
    return row
