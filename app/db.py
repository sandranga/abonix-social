"""SQLite storage, fully separate from the Abonix database."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine = None
_Session: sessionmaker | None = None


def init_db(url: str | None = None) -> None:
    global _engine, _Session
    url = url or get_settings().db_url
    _engine = create_engine(url, future=True)

    @event.listens_for(_engine, "connect")
    def _pragmas(conn, _):  # pragma: no cover - sqlite tuning
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)


@contextmanager
def session() -> Iterator[Session]:
    if _Session is None:
        init_db()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
