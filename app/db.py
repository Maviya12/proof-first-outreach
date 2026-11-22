"""Engine + session. SQLite runs in WAL mode with foreign keys on."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_engine: Engine | None = None


def _make_engine(url: str) -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return eng


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
    return _engine


def set_engine(engine: Engine) -> None:
    """Used by tests to inject an in-memory engine."""
    global _engine
    _engine = engine


def create_all() -> None:
    import app.models  # noqa: F401  (register tables)
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Iterator[Session]:
    # expire_on_commit=False so objects can be serialised after commit inside a request
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
