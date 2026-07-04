"""Read-only dashboard API package (§0.5 router split).

``app.api:app`` remains the ASGI entry point (deploy: ``uvicorn app.api:app``).
The former 703-line api.py monolith is split into :mod:`app.api.main` (app +
lifespan + CORS + router includes) and the :mod:`~app.api.health`,
:mod:`~app.api.btc` and :mod:`~app.api.subscribe` routers.

Re-exports the small surface that tests/tooling import as ``app.api.*``:
``app``, ``get_config`` (for FastAPI dependency_overrides), and the pure
helper ``_lt_breakdown`` exercised directly by tests.
"""
from ..api_deps import get_config
from .btc import _lt_breakdown
from .main import app

__all__ = ["app", "get_config", "_lt_breakdown"]
