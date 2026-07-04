"""Lake-backed daily bars from Sharadar SEP — the equity price source (§4.7).

Replaces the Yahoo/Stooq/Alpaca/massive per-ticker fetch. The nightly ingest
lands SEP in the Parquet lake; the screener reads recent bars per name straight
out of it (DuckDB filter-pushdown, so even the multi-GB SEP table stays cheap).

Bars are returned in the scorer's shape — ``[{ts, open, high, low, close,
volume}]`` oldest→newest — with **split+dividend-adjusted** prices: ``close`` is
``closeadj`` and O/H/L are scaled by the same ``closeadj/close`` factor, so
returns / DMAs / RSI are never distorted by a split. Raw prices (``closeunadj``)
are for paper-order fills only, not the signal.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _epoch_ms(d: str) -> int:
    """'YYYY-MM-DD' -> epoch ms at UTC midnight (the bars' ts convention)."""
    dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def bars_from_sep_rows(rows: list[dict]) -> list[dict]:
    """Transform SEP rows (oldest→newest) into adjusted scorer bars (pure).

    Each SEP row carries date/open/high/low/close/closeadj/volume. The
    adjustment factor closeadj/close back-adjusts O/H/L so the whole bar is on
    the total-return scale.
    """
    out: list[dict] = []
    for r in rows:
        close, closeadj = r.get("close"), r.get("closeadj")
        if close in (None, 0) or closeadj is None:
            continue
        factor = closeadj / close
        out.append({
            "ts": _epoch_ms(r["date"]),
            "open": (r.get("open") or close) * factor,
            "high": (r.get("high") or close) * factor,
            "low": (r.get("low") or close) * factor,
            "close": float(closeadj),
            "volume": float(r.get("volume") or 0.0),
        })
    return out


def sep_bars(lake, ticker: str, limit: int = 400) -> list[dict]:
    """Recent adjusted daily bars for ``ticker`` from the SEP lake table.

    Returns [] if SEP isn't ingested yet or the name has no rows. ``limit`` is
    the number of most-recent sessions; the result is oldest→newest.
    """
    if not lake.exists("sep"):
        return []
    df = lake.query(
        f"SELECT date, open, high, low, close, closeadj, volume "
        f"FROM {lake.sql_table('sep')} WHERE ticker = ? "
        f"ORDER BY date DESC LIMIT ?",
        [ticker.upper(), int(limit)])
    if df.empty:
        return []
    rows = df.iloc[::-1].to_dict("records")     # DESC -> oldest→newest
    return bars_from_sep_rows(rows)
