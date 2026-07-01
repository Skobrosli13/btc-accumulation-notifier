"""Shared FastAPI dependencies for the read-only dashboard API.

The BTC API (``api.py``) and the stock routers (``stock_api.py``,
``stock_lt_api.py``) all share ONE DB, ONE config and ONE bearer-token gate.
These helpers are the single source of truth so the three routers can't drift
apart (the auth/conn boilerplate used to be copy-pasted three times).
"""
from __future__ import annotations

import secrets
import sqlite3
from functools import lru_cache

from fastapi import Depends, Header, HTTPException

from . import store
from .config import Config, load_config


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide config cache.

    NOTE: this is why an ``.env`` change requires ``systemctl restart btc-api`` —
    the config is read once per process and cached here.
    """
    return load_config()


def require_token(authorization: str | None = Header(None),
                  cfg: Config = Depends(get_config)) -> None:
    """Enforce the internal bearer token when one is configured (open otherwise:
    dev / localhost-only). Use as ``Depends(require_token)``.

    ``cfg`` is injected via ``Depends`` (not called directly) so FastAPI test
    ``dependency_overrides`` on ``get_config`` reach the token gate too.
    """
    if not cfg.api_token:
        return
    expected = f"Bearer {cfg.api_token}"
    # Constant-time compare so the token can't be recovered byte-by-byte via
    # timing. Compare bytes so a non-ASCII header is a clean 401, not a 500
    # (``compare_digest`` rejects mixed/non-ASCII str).
    ok = bool(authorization) and secrets.compare_digest(
        (authorization or "").encode("utf-8", "ignore"), expected.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


def conn_ro(cfg: Config) -> sqlite3.Connection:
    """Read-only DB connection; 503 if the DB file is unavailable."""
    try:
        return store.connect_readonly(cfg.db_path)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


def conn_rw(cfg: Config) -> sqlite3.Connection:
    """Short-lived read-WRITE connection — the one narrow exception to the
    read-only API (subscribe/unsubscribe writes). ``init_db`` is idempotent and
    guarantees the subscribers table exists even before the first collector run.
    """
    try:
        conn = store.connect(cfg.db_path)
        store.init_db(conn)
        return conn
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")
