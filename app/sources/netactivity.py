"""Free on-chain network-activity context — FORWARD-TEST, never scored.

Active addresses, transaction count, transfer count, and addresses-with-balance.
No API key: Coin Metrics' community API (primary, richer) with a blockchain.com
charts fallback for the two core reads — both free, both already trusted by this
codebase (blockchain.com also backs miner.py). Each read is the latest *closed*
day's z-score vs its own trailing-90d baseline; raw latest values ride along for
display. All-``None`` on total failure; deliberately absent from
``scoring.CATEGORY_INDICATORS`` so the live composite is provably unaffected.

(Originally planned on CoinDesk/CryptoCompare, whose free tier was retired —
these free no-key sources cover the same network-activity signal with no key, no
lifetime cap, and the layer is always-on like the other free levers.)

Sources ::

    GET https://community-api.coinmetrics.io/v4/timeseries/asset-metrics
        ?assets=btc&metrics=AdrActCnt,TxCnt,TxTfrCnt,AdrBalCnt&frequency=1d
        -> {"data":[{"time","AdrActCnt","TxCnt","TxTfrCnt","AdrBalCnt"}, ...]}
    GET https://api.blockchain.info/charts/<n-unique-addresses|n-transactions>
        ?timespan=1year&format=json   -> {"values":[{"x":unixSec,"y":num}, ...]}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

from ._http import get_json

log = logging.getLogger(__name__)

CM_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
BC_URL = "https://api.blockchain.info/charts"

_BASELINE_DAYS = 90        # trailing window for the z-score
_MIN_BASELINE = 20         # need at least this many baseline points or z = None
_HISTORY_DAYS = 200        # recent window pulled for the baseline (Coin Metrics community serves ~this)

# Coin Metrics community metric -> (raw reading key, z-score key).
_CM_METRICS = {
    "AdrActCnt": ("na_active_addr", "na_active_addr_z"),    # active addresses — network demand
    "TxCnt": ("na_tx_count", "na_tx_count_z"),              # transactions — throughput
    "TxTfrCnt": ("na_transfers", "na_transfers_z"),         # transfers — economic activity
    "AdrBalCnt": ("na_addr_balance", "na_addr_balance_z"),  # addresses w/ balance — adoption
}
_KEYS = tuple(k for pair in _CM_METRICS.values() for k in pair)
_NONE = {k: None for k in _KEYS}


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _latest_and_z(series: list[float]) -> tuple[float | None, float | None]:
    """(latest value, z-score of latest vs trailing baseline). z is None when there
    isn't enough history or the baseline is flat (std 0)."""
    if not series:
        return None, None
    latest = series[-1]
    window = series[-(_BASELINE_DAYS + 1):-1]
    if len(window) < _MIN_BASELINE:
        return latest, None
    sd = pstdev(window)
    if sd == 0:
        return latest, None
    return latest, (latest - fmean(window)) / sd


def _cm_series() -> dict[str, list[float]]:
    """{metric: [values oldest->newest]} from Coin Metrics community, with the
    still-forming current UTC day dropped. {} on any failure."""
    start = (datetime.now(timezone.utc) - timedelta(days=_HISTORY_DAYS)).strftime("%Y-%m-%d")
    data = get_json(CM_URL, params={"assets": "btc", "metrics": ",".join(_CM_METRICS),
                                    "frequency": "1d", "page_size": 1000, "start_time": start})
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return {}
    rows = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: r.get("time", ""))
    if str(rows[-1].get("time", ""))[:10] == _today_iso():
        rows = rows[:-1]
    out: dict[str, list[float]] = {m: [] for m in _CM_METRICS}
    for r in rows:
        for m in _CM_METRICS:
            v = r.get(m)
            if v is None:
                continue
            try:
                out[m].append(float(v))
            except (TypeError, ValueError):
                continue
    return out


def _bc_series(chart: str) -> list[float]:
    """Daily values from a blockchain.com chart, oldest-first, forming-day dropped.
    [] on failure."""
    data = get_json(f"{BC_URL}/{chart}", params={"timespan": "1year", "format": "json"})
    vals = data.get("values") if isinstance(data, dict) else None
    if not isinstance(vals, list) or not vals:
        return []
    rows = [r for r in vals if isinstance(r, dict) and r.get("y") is not None and r.get("x")]
    if rows and datetime.fromtimestamp(rows[-1]["x"], tz=timezone.utc).date().isoformat() == _today_iso():
        rows = rows[:-1]
    out: list[float] = []
    for r in rows:
        try:
            out.append(float(r["y"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def netactivity() -> dict:
    """All free network-activity context reads keyed for storage/display. All-``None``
    on total failure. These keys are NOT scored — forward-test/context only."""
    out = dict(_NONE)
    try:
        cm = _cm_series()
    except Exception as exc:  # noqa: BLE001 - fail soft; never break the long-term run
        log.warning("netactivity Coin Metrics failed (%s)", exc)
        cm = {}
    for m, (raw_k, z_k) in _CM_METRICS.items():
        series = cm.get(m) or []
        if series:
            out[raw_k], out[z_k] = _latest_and_z(series)
    # blockchain.com fallback for the two core reads if Coin Metrics didn't supply them.
    try:
        if out["na_active_addr"] is None:
            out["na_active_addr"], out["na_active_addr_z"] = _latest_and_z(_bc_series("n-unique-addresses"))
        if out["na_tx_count"] is None:
            out["na_tx_count"], out["na_tx_count_z"] = _latest_and_z(_bc_series("n-transactions"))
    except Exception as exc:  # noqa: BLE001 - fallback is best-effort
        log.warning("netactivity blockchain.com fallback failed (%s)", exc)
    return out
