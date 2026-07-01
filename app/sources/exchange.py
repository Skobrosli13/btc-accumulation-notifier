"""Exchange market-data adapter — OKX primary, Kraken fallback.

Why not Binance: it returns HTTP 451 from US/AWS IPs. OKX's public market-data
API is reachable and gives klines + funding + open interest; Kraken is a
globally-reachable fallback for klines (its weekly history reaches back to 2013,
which is plenty for the 200-week MA). CoinGecko remains the last-resort price
fallback inside `price.py`.

All adapters return a normalized OHLCV DataFrame with the SAME column contract
the rest of the app expects: columns ``open_time`` (UTC datetime), ``open``,
``high``, ``low``, ``close``, ``volume`` (floats), plus ``confirmed`` (bool;
the last row may be a still-forming candle), oldest-first.

Funding/OI use the OKX perpetual swap and degrade to None on any failure.
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from ._http import get_json

log = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
KRAKEN_BASE = "https://api.kraken.com"
COINBASE_BASE = "https://api.exchange.coinbase.com"

# timeframe -> venue-specific interval codes
_OKX_BAR = {"4h": "4H", "1d": "1Dutc", "1w": "1Wutc"}
# NOTE on weekly anchors: OKX "1Wutc" weeks are Monday-anchored UTC; Kraken's
# interval=10080 weeks are epoch-aligned (Thursday 00:00 UTC boundary); and the
# Coinbase/CoinGecko fallbacks resample daily closes with pandas "1W" (Sunday-
# ending) in price.py. A venue fallback can therefore shift the weekly close
# boundary by several days — a ~0.1%-of-value nudge to the 200-week MA and the
# derived cycle ATH date, well inside the tier thresholds' tolerance. Any future
# indicator with tighter weekly-boundary sensitivity must resample all venues'
# DAILY closes to one shared anchor instead of trusting native weekly bars.
_KRAKEN_MIN = {"4h": 240, "1d": 1440, "1w": 10080}
# Coinbase only offers a fixed granularity set (seconds); 4h and 1w are NOT in it,
# so Coinbase serves as a daily/6h fallback only (still fixes the long-term ATH /
# 200-MA problem that CoinGecko's 365-day cap creates).
_COINBASE_GRAN = {"1d": 86400, "6h": 21600, "1h": 3600}
_TF_MS = {"4h": 4 * 3600_000, "1d": 86_400_000, "1w": 7 * 86_400_000}

_COLS = ["open_time", "open", "high", "low", "close", "volume", "confirmed"]


def tf_to_ms(timeframe: str) -> int:
    return _TF_MS[timeframe]


def _swap_inst(symbol: str) -> str:
    """Spot symbol (BTC-USDT) -> OKX perpetual swap instId (BTC-USDT-SWAP)."""
    return symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"


def _is_btc(symbol: str) -> bool:
    return symbol.upper().startswith(("BTC", "XBT"))


def _kraken_pair(symbol: str) -> str | None:
    # Only BTC/USD is wired for the Kraken fallback; Kraken uses XBT for BTC.
    # Return None for a non-BTC symbol so we never silently mix another asset's
    # BTC data into the series.
    return "XBTUSD" if _is_btc(symbol) else None


def _coinbase_product(symbol: str) -> str | None:
    return "BTC-USD" if _is_btc(symbol) else None


def _df_from_rows(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=_COLS)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df["confirmed"] = df["confirmed"].astype(bool)
    return df


# --- OKX ----------------------------------------------------------------------

def _okx_klines(timeframe: str, limit: int, symbol: str) -> pd.DataFrame | None:
    bar = _OKX_BAR.get(timeframe)
    if bar is None:
        return None
    data = get_json(f"{OKX_BASE}/api/v5/market/candles",
                    params={"instId": symbol, "bar": bar, "limit": min(limit, 300)})
    if not data or data.get("code") != "0" or not data.get("data"):
        return None
    rows = []
    for c in data["data"]:  # newest-first: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
        rows.append([int(c[0]), c[1], c[2], c[3], c[4], c[5], c[8] == "1"])
    rows.reverse()  # -> oldest-first
    return _df_from_rows(rows)


# --- Kraken -------------------------------------------------------------------

def _kraken_klines(timeframe: str, symbol: str,
                   limit: int | None = None) -> pd.DataFrame | None:
    interval = _KRAKEN_MIN.get(timeframe)
    pair = _kraken_pair(symbol)
    if interval is None or pair is None:
        return None
    data = get_json(f"{KRAKEN_BASE}/0/public/OHLC",
                    params={"pair": pair, "interval": interval})
    if not data or data.get("error") or not data.get("result"):
        return None
    result = data["result"]
    key = next((k for k in result if k != "last"), None)
    if key is None:
        return None
    raw = result[key]  # ascending: [time(s),o,h,l,c,vwap,vol,count]; last row is forming
    rows = []
    n = len(raw)
    for i, c in enumerate(raw):
        confirmed = i < n - 1  # Kraken's last candle is the in-progress one
        rows.append([int(c[0]) * 1000, c[1], c[2], c[3], c[4], c[6], confirmed])
    # Kraken ignores any count param and returns ~720 rows; cap to the newest
    # ``limit`` so a fallback batch never writes a WIDER window than the OKX
    # primary would have (persisted candles stay comparable across venues).
    if limit is not None and limit > 0:
        rows = rows[-limit:]
    return _df_from_rows(rows)


# --- Coinbase (last-resort klines; daily/6h only) -----------------------------

def _coinbase_klines(timeframe: str, symbol: str) -> pd.DataFrame | None:
    """Coinbase Exchange candles (free, no key, US-reachable). Daily/6h only.

    Row format: [time(s), low, high, open, close, volume], newest-first, max 300.
    Coinbase doesn't flag the forming candle, so any bucket whose period hasn't
    closed yet (open_time + granularity > now) is marked unconfirmed — keeping
    ``closed_only`` correct on this double-fallback path too (OKX flags it via
    ``confirm``, Kraken via the last-row rule).
    """
    gran = _COINBASE_GRAN.get(timeframe)
    product = _coinbase_product(symbol)
    if gran is None or product is None:
        return None
    data = get_json(f"{COINBASE_BASE}/products/{product}/candles",
                    params={"granularity": gran})
    if not data or not isinstance(data, list):
        return None
    now = int(time.time())
    rows = []
    for c in data:  # [time, low, high, open, close, volume]
        try:
            ts = int(c[0])
            rows.append([ts * 1000, c[3], c[2], c[1], c[4], c[5], ts + gran <= now])
        except (IndexError, TypeError, ValueError):
            continue
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])  # -> oldest-first
    return _df_from_rows(rows)


def coinbase_daily_history(total: int, symbol: str = "BTC-USDT",
                           include_forming: bool = True) -> pd.DataFrame | None:
    """Paginate Coinbase daily candles backward to assemble ~``total`` days.

    Used as the price-structure fallback ahead of CoinGecko: CoinGecko's free tier
    caps at 365 days, which yields a bogus 1-year "ATH" and no real 200-week MA.
    Coinbase reaches back years (300 candles/request via start/end).

    The still-open UTC day is marked unconfirmed. The long-term price-structure
    consumer wants that live snapshot (``include_forming=True``, the default —
    its headline "price" is the latest close, forming or not); pass False to
    drop unclosed buckets for closed-candle consumers.
    """
    product = _coinbase_product(symbol)
    if product is None:
        return None
    gran = 86400
    frames: list[list] = []
    end = None  # ISO8601; None = now
    pages = max(1, (total // 300) + 1)
    for _ in range(pages + 1):
        params = {"granularity": gran}
        if end is not None:
            params["end"] = end
            params["start"] = _iso(end_epoch=_parse_iso(end) - 300 * gran)
        data = get_json(f"{COINBASE_BASE}/products/{product}/candles", params=params)
        if not data or not isinstance(data, list):
            break
        chunk = sorted(data, key=lambda c: int(c[0]))
        frames.extend(chunk)
        oldest = int(chunk[0][0])
        end = _iso(end_epoch=oldest - gran)
        if len(chunk) < 300:
            break
    if not frames:
        return None
    now = int(time.time())
    seen = {}
    for c in frames:  # dedup by ts; [time, low, high, open, close, volume]
        try:
            ts = int(c[0])
            seen[ts] = [ts * 1000, c[3], c[2], c[1], c[4], c[5], ts + gran <= now]
        except (IndexError, TypeError, ValueError):
            continue
    rows = [seen[k] for k in sorted(seen)]
    if not include_forming:
        rows = [r for r in rows if r[6]]
    if not rows:
        return None
    return _df_from_rows(rows).tail(total).reset_index(drop=True)


def _parse_iso(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _iso(end_epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(end_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Public interface ---------------------------------------------------------

_KLINE_VENUES = {
    "okx": _okx_klines,
    "kraken": lambda tf, limit, sym: _kraken_klines(tf, sym, limit=limit),
    "coinbase": lambda tf, limit, sym: _coinbase_klines(tf, sym),
}


def klines(timeframe: str, limit: int = 300, symbol: str = "BTC-USDT",
           prefer: str = "okx") -> pd.DataFrame:
    """Normalized OHLCV for a timeframe, oldest-first. Tries OKX -> Kraken ->
    Coinbase (order set by ``prefer``). Raises if ALL fail (klines are mandatory;
    callers like price.py then fall back to CoinGecko).

    The returned frame carries ``df.attrs['source']`` = the venue that served it,
    so persisted candles can be venue-tagged (a fallback batch has a different
    quote currency + volume scale and must not be mixed into indicator recomputes).
    """
    order = (["kraken", "okx", "coinbase"] if prefer == "kraken"
             else ["okx", "kraken", "coinbase"])
    for venue in order:
        try:
            df = _KLINE_VENUES[venue](timeframe, limit, symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s klines(%s) failed: %s", venue, timeframe, exc)
            df = None
        if df is not None and not df.empty:
            df.attrs["source"] = venue
            return df
    raise RuntimeError(f"all exchanges failed for klines({timeframe}, {symbol})")


def klines_history(timeframe: str, total: int, symbol: str = "BTC-USDT") -> pd.DataFrame:
    """Paginate OKX candles backward via ``after`` to assemble ``total`` candles
    (for the short-term backtest). Falls back to a single Kraken pull (~720 max)."""
    frames: list[pd.DataFrame] = []
    bar = _OKX_BAR.get(timeframe)
    after: str | None = None
    if bar:
        fetched = 0
        while fetched < total:
            params = {"instId": symbol, "bar": bar, "limit": 300}
            if after:
                params["after"] = after
            data = get_json(f"{OKX_BASE}/api/v5/market/candles", params=params)
            if not data or data.get("code") != "0" or not data.get("data"):
                break
            chunk = data["data"]  # newest-first
            rows = [[int(c[0]), c[1], c[2], c[3], c[4], c[5], c[8] == "1"] for c in chunk]
            rows.reverse()
            frames.append(_df_from_rows(rows))
            fetched += len(chunk)
            after = chunk[-1][0]  # oldest ts in this chunk -> next page is older
            if len(chunk) < 300:
                break
    if frames:
        df = pd.concat(frames, ignore_index=True).drop_duplicates("open_time")
        return df.sort_values("open_time").reset_index(drop=True).tail(total)
    # fallback: whatever Kraken can give in one shot
    kdf = _kraken_klines(timeframe, symbol, limit=total)
    if kdf is not None:
        return kdf
    raise RuntimeError(f"klines_history failed for {timeframe}")


def funding_history(limit: int = 100, symbol: str = "BTC-USDT") -> list[tuple[int, float]]:
    """OKX perpetual funding history as [(ts_ms, rate), ...] oldest-first. [] on failure."""
    data = get_json(f"{OKX_BASE}/api/v5/public/funding-rate-history",
                    params={"instId": _swap_inst(symbol), "limit": min(limit, 100)})
    if not data or data.get("code") != "0" or not data.get("data"):
        return []
    out: list[tuple[int, float]] = []
    for r in data["data"]:  # newest-first
        try:
            out.append((int(r["fundingTime"]), float(r["fundingRate"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.reverse()
    return out


_EIGHT_HOURS_MS = 8 * 3600_000
# Plausible funding-interval bounds (OKX runs 8h normally, 4h in volatile
# regimes); a spacing outside this range is a garbage payload — don't scale.
_MIN_FUNDING_GAP_MS = 3600_000           # 1h
_MAX_FUNDING_GAP_MS = 24 * 3600_000      # 24h


def funding_latest(symbol: str = "BTC-USDT") -> float | None:
    """Latest OKX perp funding, normalized to a per-8h fraction, or None.

    OKX switches some instruments to 4h funding in volatile regimes; the raw
    per-settlement rate would then read ~half its 8h-equivalent exactly when the
    spike triggers and the acute-capitulation flash care most. Mirrors
    ``funding.funding_7d_avg``'s normalization: scale the rate by
    8h / (nextFundingTime - fundingTime). Falls back to the raw rate when the
    spacing fields are missing or implausible.
    """
    data = get_json(f"{OKX_BASE}/api/v5/public/funding-rate",
                    params={"instId": _swap_inst(symbol)})
    if not data or data.get("code") != "0" or not data.get("data"):
        return None
    try:
        row = data["data"][0]
        rate = float(row["fundingRate"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    try:
        gap = int(row["nextFundingTime"]) - int(row["fundingTime"])
    except (KeyError, TypeError, ValueError):
        return rate
    if _MIN_FUNDING_GAP_MS <= gap <= _MAX_FUNDING_GAP_MS:
        rate *= _EIGHT_HOURS_MS / gap
    return rate


def open_interest(symbol: str = "BTC-USDT") -> float | None:
    """Current open interest (contracts) for the OKX perp, or None."""
    data = get_json(f"{OKX_BASE}/api/v5/public/open-interest",
                    params={"instId": _swap_inst(symbol)})
    if not data or data.get("code") != "0" or not data.get("data"):
        return None
    try:
        return float(data["data"][0]["oi"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def spot_price(symbol: str = "BTC-USDT", prefer: str = "okx") -> float | None:
    """Latest spot price (last trade) from a lightweight ticker — OKX then Kraken.

    Cheap enough to call on every dashboard request (one ticker row, not 300
    klines). Returns None if both venues fail; the caller falls back to the
    stored long-term price so the headline never blanks.
    """
    order = ["kraken", "okx"] if prefer == "kraken" else ["okx", "kraken"]
    for venue in order:
        try:
            if venue == "okx":
                data = get_json(f"{OKX_BASE}/api/v5/market/ticker",
                                params={"instId": symbol})
                if data and data.get("code") == "0" and data.get("data"):
                    return float(data["data"][0]["last"])
            else:
                pair = _kraken_pair(symbol)
                if pair is None:
                    continue
                data = get_json(f"{KRAKEN_BASE}/0/public/Ticker",
                                params={"pair": pair})
                if data and not data.get("error") and data.get("result"):
                    row = next(iter(data["result"].values()), None)
                    if row:
                        return float(row["c"][0])  # c = [last_price, lot_volume]
        except Exception as exc:  # noqa: BLE001
            log.warning("%s spot_price failed: %s", venue, exc)
    return None


def closed_only(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the trailing still-forming candle so signals evaluate closed bars only."""
    if df.empty:
        return df
    return df[df["confirmed"]].reset_index(drop=True)
