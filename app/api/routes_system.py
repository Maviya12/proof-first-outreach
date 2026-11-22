"""Auth, settings, suppressions, audit log, privacy (erasure, breach log)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.errors import APIError
from app.audit import audit
from app.config import get_settings
from app.db import get_session
from app.models import AuditLog, BreachLog, Lead
from app.security import require_user, verify_password
from app.services import privacy
from app.services.settings_store import all_settings, set_setting
from app.services.suppression import add_suppression
from app.ui.templating import templates

router = APIRouter()


# ---------- auth ----------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...), session: Session = Depends(get_session)):
    if not verify_password(password, get_settings().admin_password_hash):
        audit(session, "auth.login_failed", actor="anonymous")
        session.commit()
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password"}, status_code=401)
    request.session["user"] = "admin"
    audit(session, "auth.login", actor="admin")
    session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- settings ----------

@router.get("/api/v1/settings")
def get_settings_api(session: Session = Depends(get_session), _: str = Depends(require_user)):
    return all_settings(session)


@router.put("/api/v1/settings")
def put_settings(body: dict[str, Any], session: Session = Depends(get_session), user: str = Depends(require_user)):
    for k, v in body.items():
        set_setting(session, k, v)
    audit(session, "settings.update", actor=user, keys=list(body))
    session.commit()
    return all_settings(session)


# ---------- suppressions ----------

class SuppressionIn(BaseModel):
    type: str  # email, domain, phone, instagram, linkedin, facebook, x, youtube
    value: str
    reason: str = "manual"


@router.post("/api/v1/suppressions", status_code=201)
def add_suppression_api(body: SuppressionIn, session: Session = Depends(get_session),
                        user: str = Depends(require_user)):
    try:
        added = add_suppression(session, body.type, body.value, body.reason, actor=user)
    except ValueError as e:
        raise APIError(422, "INVALID_VALUE", str(e))
    session.commit()
    return {"added": added}


# ---------- audit ----------

@router.get("/api/v1/audit")
def audit_list(limit: int = 100, session: Session = Depends(get_session), _: str = Depends(require_user)):
    rows = session.exec(select(AuditLog).order_by(AuditLog.ts.desc()).limit(min(limit, 1000))).all()
    return [r.model_dump() for r in rows]


# ---------- privacy (DPDP) ----------

@router.post("/api/v1/leads/{lead_id}/erase")
def erase_lead(lead_id: int, session: Session = Depends(get_session), user: str = Depends(require_user)):
    lead = session.get(Lead, lead_id)
    if not lead:
        raise APIError(404, "NOT_FOUND", "lead not found")
    summary = privacy.erase_lead(session, lead, actor=user)
    session.commit()
    return summary


class BreachIn(BaseModel):
    description: str
    affected: str | None = None
    action_taken: str | None = None


@router.post("/api/v1/breaches", status_code=201)
def log_breach(body: BreachIn, session: Session = Depends(get_session), user: str = Depends(require_user)):
    row = BreachLog(**body.model_dump())
    session.add(row)
    audit(session, "breach.log", actor=user, detail=json.dumps(body.model_dump()))
    session.commit()
    return row.model_dump()


@router.get("/api/v1/breaches")
def list_breaches(session: Session = Depends(get_session), _: str = Depends(require_user)):
    return [r.model_dump() for r in session.exec(select(BreachLog).order_by(BreachLog.detected_at.desc()))]
