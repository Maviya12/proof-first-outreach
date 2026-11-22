from __future__ import annotations

from fastapi import HTTPException


class APIError(HTTPException):
    """Raises the spec's error shape: {"error": CODE, "detail": "...", ...extra}."""

    def __init__(self, status: int, code: str, detail: str, **extra):
        super().__init__(status_code=status, detail={"error": code, "detail": detail, **extra})
