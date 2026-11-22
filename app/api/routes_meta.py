"""Meta webhook (Phase 6). Not session-protected: authenticated by X-Hub-Signature-256 instead.
Must be served over public HTTPS (e.g. a tunnel) for Meta to reach it."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from app.api.errors import APIError
from app.config import get_settings
from app.db import get_session
from app.llm import LLM

router = APIRouter(prefix="/webhooks/meta", include_in_schema=False)


@router.get("")
def verify(request: Request):
    cfg = get_settings()
    q = request.query_params
    if not cfg.meta_inbound_enabled or not cfg.meta_verify_token:
        raise APIError(404, "DISABLED", "Meta inbound is disabled")
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == cfg.meta_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise APIError(403, "FORBIDDEN", "verification failed")


@router.post("")
async def receive(request: Request, session: Session = Depends(get_session)):
    from app.services.meta_inbound import process_webhook, verify_signature
    cfg = get_settings()
    if not cfg.meta_inbound_enabled:
        raise APIError(404, "DISABLED", "Meta inbound is disabled")
    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), cfg.meta_app_secret):
        raise APIError(403, "BAD_SIGNATURE", "signature check failed")
    llm = LLM(session) if cfg.anthropic_api_key else None
    res = process_webhook(session, json.loads(body), llm=llm, cfg=cfg)
    session.commit()
    return res
