"""Gmail via official OAuth from the user's own mailbox (spec 6.6 step 5, 6.7, 10).

Scopes are minimal: gmail.send + gmail.readonly. The OAuth credentials JSON (incl. refresh
token) is Fernet-encrypted at rest in `integrations`; it is never logged.

Note: a Google Cloud OAuth app in "Testing" status issues refresh tokens that expire after 7 days.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import get_engine
from app.models import Integration, utcnow
from app.security import TokenCipher

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]


class Mailer(Protocol):
    account_email: str

    def send(self, raw_b64: str, thread_id: str | None = None) -> tuple[str, str]: ...
    def thread_messages(self, thread_id: str) -> list[dict]: ...


# ---------------- message building ----------------

def opt_out_footer(cfg: Settings) -> str:
    """DPDP/CAN-SPAM notice: who you are, why you're writing, how to opt out, postal/contact details."""
    lines = ["--", cfg.sender_name or "", cfg.sender_contact_address or "",
             "I'm writing because your business's contact details are listed publicly on your website. "
             "If you'd rather not hear from me, just reply \"stop\" and I won't contact you again."]
    return "\n".join(l for l in lines if l)


def build_mime(*, cfg: Settings, from_email: str, to: str, subject: str, body: str, in_reply_to: str | None = None,
               references: str | None = None, include_footer: bool = True) -> tuple[str, str]:
    """Return (base64url raw message, Message-ID)."""
    if not cfg.sender_name or not cfg.sender_contact_address:
        raise ValueError("SENDER_NAME and SENDER_CONTACT_ADDRESS must be set before sending email")
    m = EmailMessage()
    m["From"] = formataddr((cfg.sender_name, from_email))
    m["To"] = to
    m["Subject"] = subject
    msg_id = make_msgid(domain=from_email.split("@")[-1])
    m["Message-ID"] = msg_id
    m["List-Unsubscribe"] = f"<mailto:{from_email}?subject=unsubscribe>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = references or in_reply_to
    text = body.rstrip() + ("\n\n" + opt_out_footer(cfg) if include_footer else "")
    m.set_content(text)
    return base64.urlsafe_b64encode(m.as_bytes()).decode(), msg_id


def extract_plain_text(payload: dict) -> str:
    """Pull text/plain from a Gmail API message payload (falls back to stripped HTML)."""
    def walk(p: dict) -> list[tuple[str, str]]:
        out = []
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            out.append((mime, base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")))
        for part in p.get("parts", []) or []:
            out.extend(walk(part))
        return out
    parts = walk(payload)
    for mime, t in parts:
        if mime == "text/plain":
            return t
    for _, t in parts:
        return re.sub(r"<[^>]+>", " ", t)
    return ""


def header(msg: dict, name: str) -> str | None:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


# ---------------- OAuth ----------------

def _client_config(cfg: Settings) -> dict:
    if not cfg.google_oauth_client_id or not cfg.google_oauth_client_secret:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not set")
    return {"web": {"client_id": cfg.google_oauth_client_id, "client_secret": cfg.google_oauth_client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [cfg.google_oauth_redirect_uri]}}


def start_oauth(cfg: Settings | None = None) -> tuple[str, str, str | None]:
    """Returns (authorization_url, state, code_verifier)."""
    from google_auth_oauthlib.flow import Flow
    cfg = cfg or get_settings()
    flow = Flow.from_client_config(_client_config(cfg), scopes=SCOPES, redirect_uri=cfg.google_oauth_redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
    return url, state, getattr(flow, "code_verifier", None)


def finish_oauth(session: Session, *, code: str, state: str, code_verifier: str | None,
                 cfg: Settings | None = None) -> str:
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    cfg = cfg or get_settings()
    flow = Flow.from_client_config(_client_config(cfg), scopes=SCOPES, state=state,
                                   redirect_uri=cfg.google_oauth_redirect_uri)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    email = svc.users().getProfile(userId="me").execute()["emailAddress"]
    save_credentials(session, creds.to_json(), email)
    return email


def save_credentials(session: Session, creds_json: str, email: str | None) -> None:
    cipher = TokenCipher()
    row = session.exec(select(Integration).where(Integration.provider == "google")).first()
    if row is None:
        row = Integration(provider="google", token_encrypted=cipher.encrypt(creds_json), account_email=email)
    else:
        row.token_encrypted = cipher.encrypt(creds_json)
        row.account_email = email or row.account_email
        row.updated_at = utcnow()
    session.add(row)


class GmailClient:
    def __init__(self, service, account_email: str):  # noqa: ANN001
        self.svc = service
        self.account_email = account_email

    def send(self, raw_b64: str, thread_id: str | None = None) -> tuple[str, str]:
        body = {"raw": raw_b64}
        if thread_id:
            body["threadId"] = thread_id
        res = self.svc.users().messages().send(userId="me", body=body).execute()
        return res["id"], res["threadId"]

    def thread_messages(self, thread_id: str) -> list[dict]:
        res = self.svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return res.get("messages", [])


def load_gmail(session: Session | None = None) -> GmailClient | None:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    own = session is None
    session = session or Session(get_engine())
    try:
        row = session.exec(select(Integration).where(Integration.provider == "google")).first()
        if not row:
            return None
        info = json.loads(TokenCipher().decrypt(row.token_encrypted))
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(session, creds.to_json(), row.account_email)
            session.commit()
        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return GmailClient(svc, row.account_email or "")
    except Exception:  # noqa: BLE001 — surface as "not connected"; never log token material
        log.warning("Gmail credentials unusable; reconnect Google in Settings")
        return None
    finally:
        if own:
            session.close()
