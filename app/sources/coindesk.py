"""CoinDesk / CryptoCompare adapter — network-activity + social context reads.

A FORWARD-TEST layer: these reads are stored and displayed, and evaluated for
edge offline (``scripts/eval_coindesk.py``), but they are deliberately **NOT** in
``scoring.CATEGORY_INDICATORS`` / ``THRESHOLDS`` — they never move the live
composite or fire an alert until an edge is proven. (Same posture as ``flow.py``.)

Why the legacy host. The new ``data-api.coindesk.com`` retired free access in
mid-2025; the **legacy** ``min-api.cryptocompare.com`` endpoints still serve the
Blockchain (on-chain activity) and Social series on a free key (a 250k *lifetime*
call budget — at a 6h cadence over a handful of endpoints that lasts years). The
raw per-block endpoint is intentionally NOT used: raw blocks are not a signal and
live on the paywalled host.

Activated by ``COINDESK_API_KEY`` presence; every function degrades to an
all-``None`` dict so a dark layer never breaks a run. Auth is the CryptoCompare
convention: header ``authorization: Apikey <key>``.

Each reading is computed **baseline-relative** — the latest *closed* day's z-score
vs its own trailing ~90d distribution (the system already scores regime/relative,
not absolute: see ``miner.py`` and the OI-delta path). The still-forming current
day is dropped, mirroring ``exchange.closed_only``. Raw latest values ride along
for display.

Endpoints (base ``https://min-api.cryptocompare.com``) ::

    GET /data/blockchain/histo/day?fsym=BTC&limit=N
        -> {"Data":{"Data":[{time, active_addresses, transaction_count,
                             large_transaction_count, new_addresses, ...}]}}
    GET /data/social/coin/histo/day?coinId=1182&limit=N
        -> {"Data":[{time, reddit_active_users, reddit_posts_per_day,
                     reddit_comments_per_day, ...}]}   (coinId 1182 = BTC)
"""
from __future__ import annotations

import logging
from statistics import fmean, pstdev

from ._http import get_json

log = logging.getLogger(__name__)

BASE = "https://min-api.cryptocompare.com"
BTC_COIN_ID = 1182          # CryptoCompare's numeric id for BTC (the social endpoint keys off it)
_HISTO_LIMIT = 200          # daily points to pull: ~90d baseline + buffer
_BASELINE_DAYS = 90         # trailing window for the z-score
_MIN_BASELINE = 20          # need at least this many baseline points or z = None

# Reading keys, prefixed ``cd_`` so they can never collide with a scored key.
_ONCHAIN_KEYS = ("cd_active_addr", "cd_active_addr_z", "cd_large_tx", "cd_large_tx_z",
                 "cd_new_addr", "cd_new_addr_z", "cd_tx_count", "cd_tx_count_z")
_SOCIAL_KEYS = ("cd_social_z", "cd_reddit_active")
_NONE = {k: None for k in _ONCHAIN_KEYS + _SOCIAL_KEYS}


def _hdr(api_key: str) -> dict:
    return {"authorization": f"Apikey {api_key}"}


def _rows(path: str, params: dict, api_key: str) -> list[dict]:
    """Daily rows for an endpoint, oldest-first, with the still-forming current
    day dropped (closed-day-only). [] on any failure.

    Handles both response shapes: blockchain nests under ``Data.Data`` while the
    social series puts the list directly under ``Data``.
    """
    data = get_json(f"{BASE}{path}", params=params, headers=_hdr(api_key))
    body = data.get("Data") if isinstance(data, dict) else None
    rows = body.get("Data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return []
    rows = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: r.get("time", 0))
    # Drop the trailing still-forming day so a partial daily total can't skew the
    # latest read (mirrors exchange.closed_only).
    return rows[:-1] if len(rows) > 1 else rows


def _series(rows: list[dict], field: str) -> list[float]:
    """Numeric series for ``field`` across ``rows`` (already time-ordered)."""
    out: list[float] = []
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _latest_and_z(series: list[float]) -> tuple[float | None, float | None]:
    """(latest value, z-score of latest vs trailing baseline). z is None when
    there isn't enough history or the baseline is flat (std 0)."""
    if not series:
        return None, None
    latest = series[-1]
    window = series[-(_BASELINE_DAYS + 1):-1]   # trailing closed days before latest
    if len(window) < _MIN_BASELINE:
        return latest, None
    sd = pstdev(window)
    if sd == 0:
        return latest, None
    return latest, (latest - fmean(window)) / sd


def coindesk_onchain(api_key: str, *, limit: int = _HISTO_LIMIT) -> dict:
    """Network-activity reads (active/new addresses, large-txn + total txn count)
    as latest value + baseline z-score. All-``None`` on failure."""
    rows = _rows("/data/blockchain/histo/day", {"fsym": "BTC", "limit": limit}, api_key)
    if not rows:
        return {k: None for k in _ONCHAIN_KEYS}
    fields = {
        "active_addresses": ("cd_active_addr", "cd_active_addr_z"),
        "large_transaction_count": ("cd_large_tx", "cd_large_tx_z"),
        "new_addresses": ("cd_new_addr", "cd_new_addr_z"),
        "transaction_count": ("cd_tx_count", "cd_tx_count_z"),
    }
    out: dict[str, float | None] = {}
    for field, (raw_k, z_k) in fields.items():
        latest, z = _latest_and_z(_series(rows, field))
        out[raw_k], out[z_k] = latest, z
    return out


def coindesk_social(api_key: str, *, limit: int = _HISTO_LIMIT,
                    coin_id: int = BTC_COIN_ID) -> dict:
    """Social-momentum read: the mean baseline z-score across the reliable Reddit
    activity fields (active users / posts / comments per day), plus raw active
    users. All-``None`` on failure. (Twitter fields in the legacy feed are stale,
    so they are deliberately excluded.)"""
    rows = _rows("/data/social/coin/histo/day", {"coinId": coin_id, "limit": limit}, api_key)
    if not rows:
        return {k: None for k in _SOCIAL_KEYS}
    reddit_active, _ = _latest_and_z(_series(rows, "reddit_active_users"))
    zs = []
    for field in ("reddit_active_users", "reddit_posts_per_day", "reddit_comments_per_day"):
        _, z = _latest_and_z(_series(rows, field))
        if z is not None:
            zs.append(z)
    return {"cd_social_z": (fmean(zs) if zs else None), "cd_reddit_active": reddit_active}


def coindesk() -> dict:
    """All CoinDesk context reads keyed for storage/display. Reads the key from
    config; returns the all-``None`` dict when the layer is dark or anything fails.
    These keys are NOT scored — forward-test/context only."""
    from ..config import load_config
    cfg = load_config()
    if not cfg.coindesk_api_key:
        return dict(_NONE)
    out = dict(_NONE)
    try:
        out.update(coindesk_onchain(cfg.coindesk_api_key))
        out.update(coindesk_social(cfg.coindesk_api_key))
    except Exception as exc:  # noqa: BLE001 - fail soft; never break the long-term run
        log.warning("coindesk() failed (%s); context layer skipped", exc)
        return dict(_NONE)
    return out
