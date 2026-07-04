"""FastAPI assembly for the read-only dashboard API (§0.5 router split).

Creates the app, wires the lifespan (schema init on STARTUP — no import-time side
effects), CORS, and includes every router. Bound to localhost in production; the
human gate is nginx HTTP basic auth in front of the dashboard (Let's Encrypt
TLS), and an optional internal bearer token (cfg.api_token) gates the JSON
endpoints. The DB is opened READ-ONLY per request so the API can never corrupt
collector writes (WAL allows concurrent reads).

Run:  uvicorn app.api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import stock_api, stock_lt_api, stock_lt_store, stock_store, store
from ..api_deps import get_config
from ..config import load_config
from ..harness import schema as harness_schema
from . import btc, health, policies, studies, subscribe


def _ensure_schema() -> None:
    """Best-effort: create both schemas so the READ-ONLY endpoints never hit a
    missing table on a fresh box (before the first collector cron has run). The
    API already writes for subscribe (_conn_rw/init_db), so this needs no new
    privilege; the stock tables are additive and idempotent."""
    try:
        conn = store.connect(load_config().db_path)
        store.init_db(conn)
        stock_store.init_stock_db(conn)
        stock_lt_store.init_stock_lt_db(conn)
        harness_schema.init_harness_db(conn)
        conn.close()
    except Exception:  # noqa: BLE001 - never block startup on schema init
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup work, run by the ASGI server — NOT at import (§0.5: no import-time
    side effects). Merely importing ``app.api`` (tests, tooling) touches no
    database; the schema is ensured when the server actually starts."""
    _ensure_schema()
    yield


app = FastAPI(title="BTC Signal API", version="1.0.0", lifespan=lifespan)

# CORS only matters if the dashboard is served cross-origin (it usually isn't).
# Read config here (cheap, cached, no I/O) rather than holding a module global.
if get_config().api_cors_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[get_config().api_cors_origin],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

# BTC signal endpoints, health, email subscriptions, the research lab, and the
# PROMOTED-policy live state.
app.include_router(health.router)
app.include_router(btc.router)
app.include_router(subscribe.router)
app.include_router(studies.router)
app.include_router(policies.router)
# Second asset: the stock swing tracker (/api/stock/*) + long-term engine
# (/api/stock/longterm/*) — already self-contained routers.
app.include_router(stock_api.router)
app.include_router(stock_lt_api.router)
