"""Password hashing (argon2), token encryption (Fernet), session auth dependency."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status

from app.config import get_settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


class TokenCipher:
    """Encrypts OAuth tokens at rest. Refuses to work without a FERNET_KEY."""

    def __init__(self, key: str | None = None):
        key = key if key is not None else get_settings().fernet_key
        if not key:
            raise RuntimeError("FERNET_KEY is not set; refusing to store tokens")
        self._f = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._f.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise RuntimeError("Token decryption failed (wrong FERNET_KEY?)") from e


def require_user(request: Request) -> str:
    """FastAPI dependency: single-user session check."""
    if request.session.get("user") != "admin":
        if request.url.path.startswith("/api/"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return "admin"
