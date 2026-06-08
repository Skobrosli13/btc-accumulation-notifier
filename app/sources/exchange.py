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

import pandas as pd

from ._http import get_json

log = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
KRAKEN_BASE = "https://api.kraken.com"

# timeframe -> venue-specific interval codes
_OKX_BAR = {"4h": "4H", "1d": "1Dutc", "1w": "1Wutc"}
_KRAKEN_MIN = {"4h": 240, "1d": 1440, "1w": 10080}
_TF_MS = {"4h": 4 * 3600_000, "1d": 86_400_000, "1w": 7 * 86_400_000}

_COLS = ["open_time", "open", "high", "low", "close", "volume", "confirmed"]


def tf_to_ms(timeframe: str) -> int:
    return _TF_MS[timeframe]


def _swap_inst(symbol: str) -> str:
    """Spot symbol (BTC-USDT) -> OKX perpetual swap instId (BTC-USDT-SWAP)."""
    return symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"


def _kraken_pair(symbol: str) -> str:
    # Only BTC/USD is in scope; Kraken uses XBT for BTC.
    return "XBTUSD"


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

def _kraken_klines(timeframe: str, symbol: str) -> pd.DataFrame | None:
    interval = _KRAKEN_MIN.get(timeframe)
    if interval is None:
        return None
    data = get_json(f"{KRAKEN_BASE}/0/public/OHLC",
                    params={"pair": _kraken_pair(symbol), "interval": interval})
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
    return _df_from_rows(rows)


# --- Public interface ---------------------------------------------------------

def klines(timeframe: str, limit: int = 300, symbol: str = "BTC-USDT",
           prefer: str = "okx") -> pd.DataFrame:
    """Normalized OHLCV for a timeframe, oldest-first. Tries OKX then Kraken
    (order set by ``prefer``). Raises if BOTH fail (klines are mandatory; callers
    like price.py fall back to CoinGecko)."""
    order = ["kraken", "okx"] if prefer == "kraken" else ["okx", "kraken"]
    for venue in order:
        try:
            df = _okx_klines(timeframe, limit, symbol) if venue == "okx" \
                else _kraken_klines(timeframe, symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s klines(%s) failed: %s", venue, timeframe, exc)
            df = None
        if df is not None and not df.empty:
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
    kdf = _kraken_klines(timeframe, symbol)
    if kdf is not None:
        return kdf.tail(total)
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


def funding_latest(symbol: str = "BTC-USDT") -> float | None:
    """Latest 8h funding fraction for the OKX perp, or None."""
    data = get_json(f"{OKX_BASE}/api/v5/public/funding-rate",
                    params={"instId": _swap_inst(symbol)})
    if not data or data.get("code") != "0" or not data.get("data"):
        return None
    try:
        return float(data["data"][0]["fundingRate"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


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
                data = get_json(f"{KRAKEN_BASE}/0/public/Ticker",
                                params={"pair": _kraken_pair(symbol)})
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
