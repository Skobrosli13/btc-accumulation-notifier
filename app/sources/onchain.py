"""On-chain valuation.

The highest-signal layer for cycle bottoms. Provider precedence:
  1. Glassnode (paid, ``GLASSNODE_API_KEY``) — a strict AUGMENT, not a swap:
     the keyed path still merges the free BGeometrics static-file cohort
     metrics (reserve_risk / lth_sopr / sth_sopr / lth_mvrv — the calibrated
     multi-cycle indicators), and falls back to the free provider entirely if
     the Glassnode fetch yields nothing, so keying up can never score FEWER
     indicators than the free tier the track record certifies.
  2. bitcoin-data.com / BGeometrics (FREE, no key) — the default; lights the
     layer up end-to-end on the free tier. Disable with ``ONCHAIN_FREE=false``.
Each metric fails soft to None and the whole category renormalizes away if the
chosen provider is unreachable. Dated payloads are checked against the config
freshness budget — a frozen upstream reads as missing, not as current.

Glassnode metrics used (header ``X-Api-Key``, params ``a=BTC&i=24h``):
  market/mvrv_z_score                     -> mvrv_z
  market/price_realized_usd               -> realized price (-> realized_ratio = price/realized)
  indicators/net_unrealized_profit_loss   -> nupl
  indicators/sopr                         -> sopr (latest daily — the same raw-daily
                                             definition as the free provider; the
                                             committed thresholds were tuned on it)
  indicators/puell_multiple               -> puell

bitcoin-data.com endpoints (GET ``/v1/<metric>/last`` -> ``{"d":..,"<field>":num}``):
  mvrv-zscore -> mvrvZscore | nupl -> nupl | sopr -> sopr |
  puell-multiple -> puellMultiple | realized-price -> realizedPrice
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ._http import get_json, is_stale

log = logging.getLogger(__name__)

GLASSNODE_BASE = "https://api.glassnode.com/v1/metrics"
BITCOIN_DATA_BASE = "https://bitcoin-data.com/v1"
# BGeometrics static JSON files: the SAME series as the /v1 REST API but with NO
# rate limit and full 2012+ history (the REST /last is hard-capped at ~10 req/hr).
# Used to SCORE metrics without spending the REST budget — and as the offline
# calibration history source. Format: [[unixMs, value], ...] oldest->newest.
# NOTE: only some metrics are published as files; the core valuation set
# (mvrv-zscore/nupl/sopr/puell) is NOT, so those stay on the REST API below.
BG_FILES_BASE = "https://charts.bgeometrics.com/files"

_NONE_READINGS = {"mvrv_z": None, "realized_ratio": None, "nupl": None,
                  "sopr": None, "puell": None}

# Free provider: scorer key -> (endpoint slug, JSON field) for the point metrics.
_BD_METRICS = {
    "mvrv_z": ("mvrv-zscore", "mvrvZscore"),
    "nupl":   ("nupl", "nupl"),
    "sopr":   ("sopr", "sopr"),
    "puell":  ("puell-multiple", "puellMultiple"),
}

# Context-only metrics (shown, NOT scored — only ~1 cycle of free history to
# threshold against, so they inform rather than move the composite).
_BD_CONTEXT = {
    "rhodl":        ("rhodl-ratio", "rhodlRatio"),
}

# Scored metrics sourced from the rate-cap-free BGeometrics static files
# (scorer key -> file basename). reserve_risk now has full 2012+ history this way,
# so it graduates from context-only to a scored holder-conviction indicator.
_BG_FILE_METRICS = {
    "reserve_risk": "reserve_risk",
    "lth_sopr": "lth_sopr",      # long-term-holder SOPR (<1 = LTH realizing losses)
    "sth_sopr": "sth_sopr",      # short-term-holder SOPR (<1 = recent-buyer washout)
    "lth_mvrv": "lth_mvrv",      # long-term-holder MVRV (low = LTH cost basis near price)
}


def _gn_series(path: str, api_key: str) -> list[float]:
    """Fetch a Glassnode metric as a list of float values (oldest->newest)."""
    data = get_json(f"{GLASSNODE_BASE}/{path}",
                    params={"a": "BTC", "i": "24h"},
                    headers={"X-Api-Key": api_key})
    if not data:
        return []
    out: list[float] = []
    for row in data:
        v = row.get("v")
        if v is not None:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
    return out


def _gn_latest(path: str, api_key: str) -> float | None:
    vals = _gn_series(path, api_key)
    return vals[-1] if vals else None


def _from_glassnode(api_key: str, price: float | None) -> dict:
    realized = _gn_latest("market/price_realized_usd", api_key)
    realized_ratio = (price / realized) if (price and realized) else None

    return {
        "mvrv_z": _gn_latest("market/mvrv_z_score", api_key),
        "realized_ratio": realized_ratio,
        "nupl": _gn_latest("indicators/net_unrealized_profit_loss", api_key),
        # RAW latest daily, deliberately NOT 7d-smoothed: the free provider is
        # raw daily and the committed sopr/froth thresholds were tuned on raw
        # daily — smoothing on one provider only is a train/serve skew.
        "sopr": _gn_latest("indicators/sopr", api_key),
        "puell": _gn_latest("indicators/puell_multiple", api_key),
    }


def _bd_ts_seconds(data: dict) -> float | None:
    """Epoch seconds of a bitcoin-data.com payload (``unixTs`` or the ``d`` ISO
    date), or None when no date field parses (then no freshness check applies)."""
    ts = data.get("unixTs")
    if ts is not None:
        try:
            ts_f = float(ts)
            return ts_f / 1000.0 if ts_f > 1e12 else ts_f
        except (TypeError, ValueError):
            pass
    d = data.get("d")
    if isinstance(d, str):
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def _bd_last(slug: str, field: str) -> float | None:
    """Latest value of one bitcoin-data.com metric, or None on any failure —
    including a STALE latest point (older than the freshness budget): a frozen
    upstream must renormalize away, not keep scoring its last value as current."""
    data = get_json(f"{BITCOIN_DATA_BASE}/{slug}/last")
    if not isinstance(data, dict):
        # An error page / list / bare string must darken THIS metric only — an
        # escaping AttributeError would take the whole layer down with it.
        return None
    ts = _bd_ts_seconds(data)
    if ts is not None and is_stale(ts):
        log.warning("bitcoin-data.com %s latest point is stale (%s); treated as missing",
                    slug, data.get("d"))
        return None
    try:
        v = data.get(field)
        return float(v) if v is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _bg_last(name: str) -> float | None:
    """Latest value of a BGeometrics static-file metric, or None on any failure.

    Reads ``{BG_FILES_BASE}/<name>.json`` ([[unixMs, value], ...] oldest->newest);
    no rate limit, full history. Walks back from the end to the most recent NON-null
    value — these files carry a trailing null for the current, not-yet-computed day.
    A most-recent value older than the freshness budget means the file generator
    has frozen: the metric reads as missing rather than stale-scored-as-current.
    """
    data = get_json(f"{BG_FILES_BASE}/{name}.json")
    if not isinstance(data, list):
        return None
    for row in reversed(data):
        try:
            v = row[1]
        except (TypeError, IndexError):
            continue
        if v is None:
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        try:
            ts_s = float(row[0]) / 1000.0
        except (TypeError, ValueError, IndexError):
            ts_s = None  # undated row: no freshness check possible
        if ts_s is not None and is_stale(ts_s):
            log.warning("BGeometrics %s latest value is stale; treated as missing", name)
            return None
        return val
    return None


def bg_history(name: str) -> list[tuple[int, float]]:
    """Full daily series for a BGeometrics static-file metric (OFFLINE calibration).

    Unlike ``history()`` (the rate-limited /v1 REST path) this static file has no
    rate limit and full 2012+ history, so it's the preferred calibration source for
    file-backed metrics. Returns [(unixMs, value), ...] oldest->newest, or [].
    """
    data = get_json(f"{BG_FILES_BASE}/{name}.json")
    if not isinstance(data, list):
        return []
    out: list[tuple[int, float]] = []
    for row in data:
        try:
            if row and row[1] is not None:
                out.append((int(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _from_bitcoin_data(price: float | None) -> dict:
    """Free on-chain readings (bitcoin-data.com / BGeometrics) keyed for the scorer.

    No API key. Each metric fails soft to None independently; ``realized_ratio``
    needs the current spot price (passed in) and is None if either is missing.
    """
    out = {key: _bd_last(slug, field) for key, (slug, field) in _BD_METRICS.items()}
    realized_price = _bd_last("realized-price", "realizedPrice")
    out["realized_ratio"] = (price / realized_price) if (price and realized_price) else None
    # Scored, from the rate-cap-free static files (full history).
    for key, name in _BG_FILE_METRICS.items():
        out[key] = _bg_last(name)
    # Context-only metrics (not scored).
    for key, (slug, field) in _BD_CONTEXT.items():
        out[key] = _bd_last(slug, field)
    return out


def history(slug: str) -> list[dict]:
    """Full daily series for a bitcoin-data.com metric (OFFLINE calibration only).

    GET /v1/<slug> (no /last) -> [{"d","unixTs","<field>":v}, ...]. Returns [] on
    failure. Rate-limited (~10 req/hr) — NEVER call from the live run/collect/api
    paths; only scripts/calibrate.py uses this.
    """
    data = get_json(f"{BITCOIN_DATA_BASE}/{slug}")
    return data if isinstance(data, list) else []


def onchain(price: float | None = None) -> dict:
    """On-chain readings keyed for the scorer.

    Free by default (bitcoin-data.com); Glassnode augments when a key is set.
    All-None only when the free feed is disabled and no usable paid data exists,
    or the chosen provider is unreachable. ``price`` (current spot) is needed
    for the realized ratio.
    """
    from ..config import load_config

    cfg = load_config()
    if cfg.glassnode_api_key:
        readings = None
        try:
            readings = _from_glassnode(cfg.glassnode_api_key, price)
        except Exception as exc:  # noqa: BLE001
            log.warning("Glassnode fetch failed (%s); falling back to the free provider", exc)
        if readings is not None and any(v is not None for v in readings.values()):
            # A paid key must strictly AUGMENT the free tier. The four calibrated
            # multi-cycle cohort metrics (reserve_risk / lth_sopr / sth_sopr /
            # lth_mvrv) live in the keyless, rate-cap-free BGeometrics static
            # files, so merge them in — dropping them would silently remove
            # scored indicators and step-change the composite away from the
            # inputs the committed calibration/track record certify.
            for key, name in _BG_FILE_METRICS.items():
                readings[key] = _bg_last(name) if cfg.onchain_free_enabled else None
            # Context-only REST metrics stay off the keyed path (display-only,
            # and the free REST budget is ~10 req/hr); keys kept for a stable
            # reading shape across providers.
            readings.update({key: None for key in _BD_CONTEXT})
            return readings
        if readings is not None:
            log.warning("Glassnode returned no usable values; "
                        "falling back to the free provider")
        # fall through: the keyed configuration must never be LESS resilient
        # than the free one.

    if cfg.onchain_free_enabled:
        try:
            readings = _from_bitcoin_data(price)
        except Exception as exc:  # noqa: BLE001
            log.warning("bitcoin-data.com fetch failed (%s); on-chain layer skipped", exc)
            return dict(_NONE_READINGS)
        if all(v is None for v in readings.values()):
            log.warning("bitcoin-data.com returned no on-chain values; layer dark this run")
        return readings

    log.info("On-chain layer dark (ONCHAIN_FREE=false and no usable paid source); skipped")
    return dict(_NONE_READINGS)
