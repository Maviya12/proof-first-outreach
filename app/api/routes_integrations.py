"""Gmail OAuth connect/callback/status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.api.errors import APIError
from app.audit import audit
from app.db import get_session
from app.models import Integration
from app.security import require_user

router = APIRouter(prefix="/api/v1/integrations/google", dependencies=[Depends(require_user)])


@router.post("/connect")
def connect(request: Request):
    from app.services.gmail import start_oauth
    try:
        url, state, verifier = start_oauth()
    except RuntimeError as e:
        raise APIError(409, "OAUTH_NOT_CONFIGURED", str(e))
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = verifier
    return {"authorization_url": url}


@router.get("/connect")
def connect_redirect(request: Request):
    return RedirectResponse(connect(request)["authorization_url"], status_code=303)


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             session: Session = Depends(get_session), user: str = Depends(require_user)):
    from app.services.gmail import finish_oauth
    if error:
        raise APIError(400, "OAUTH_DENIED", error)
    if not state or state != request.session.pop("oauth_state", None):
        raise APIError(400, "OAUTH_STATE_MISMATCH", "state mismatch; start again")
    email = finish_oauth(session, code=code, state=state, code_verifier=request.session.pop("oauth_verifier", None))
    audit(session, "integration.google_connected", actor=user, account=email)
    session.commit()
    return RedirectResponse("/settings?connected=1", status_code=303)


@router.get("/status")
def status(session: Session = Depends(get_session)):
    row = session.exec(select(Integration).where(Integration.provider == "google")).first()
    return {"connected": bool(row), "account_email": row.account_email if row else None,
            "updated_at": row.updated_at if row else None}


@router.post("/disconnect")
def disconnect(session: Session = Depends(get_session), user: str = Depends(require_user)):
    row = session.exec(select(Integration).where(Integration.provider == "google")).first()
    if row:
        session.delete(row)
        audit(session, "integration.google_disconnected", actor=user)
        session.commit()
    return {"connected": False}
