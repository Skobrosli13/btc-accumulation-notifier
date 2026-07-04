"""Lake-backed daily bars from Sharadar SEP — the equity price source (§4.7).

Replaces the Yahoo/Stooq/Alpaca/massive per-ticker fetch. The nightly ingest
lands SEP in the Parquet lake; the screener reads recent bars per name straight
out of it (DuckDB filter-pushdown, so even the multi-GB SEP table stays cheap).

Bars are returned in the scorer's shape — ``[{ts, open, high, low, close,
volume}]`` oldest→newest — with **split+dividend-adjusted** prices: ``close`` is
``closeadj`` and O/H/L are scaled by the same ``closeadj/close`` factor, so
returns / DMAs / RSI are never distorted by a split. Raw prices (``closeunadj``)
are for paper-order fills only, not the signal.

Vendor-restatement caveat (verified live on the AAPL/TSLA 2020 splits): Sharadar
retroactively restates SEP ``open/high/low/close`` for splits — ``closeadj`` adds
only the dividend adjustment on top, and the TRUE as-traded series is
``closeunadj``. Split-safety therefore depends on the sep table carrying the
restated history: after a split the vendor bumps ``lastupdated`` on the restated
rows, so a ``lastupdated.gte`` incremental refresh picks them up and the
(ticker, date) upsert replaces the stale bars; a bulk pull is a full snapshot and
is always consistent. Never append post-split rows without merging restated
history. (QA note: feed ``detect_price_spikes`` the ``closeunadj`` series —
``close`` is already split-adjusted and will never show the cliff.)
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


def sep_bars_bulk(lake, tickers: list[str], limit: int = 400,
                  start_date: str | None = None,
                  end_date: str | None = None) -> dict[str, list[dict]]:
    """Adjusted daily bars for MANY tickers in one DuckDB pass.

    One window-function query beats thousands of per-ticker parquet scans
    (the full-universe screen dropped from tens of minutes to seconds). Returns
    ``{ticker: bars}`` (same shape as :func:`sep_bars`); names with no rows are
    simply absent. ``start_date``/``end_date`` (ISO) bound the scan — chunked
    evaluations use this to keep memory flat instead of loading whole histories.
    """
    if not lake.exists("sep") or not tickers:
        return {}
    uniq = sorted({t.upper() for t in tickers})
    placeholders = ",".join("?" for _ in uniq)
    bounds, params = "", []
    if start_date:
        bounds += " AND CAST(date AS DATE) >= CAST(? AS DATE)"
        params.append(start_date)
    if end_date:
        bounds += " AND CAST(date AS DATE) <= CAST(? AS DATE)"
        params.append(end_date)
    df = lake.query(
        f"SELECT ticker, date, open, high, low, close, closeadj, volume FROM ("
        f"  SELECT ticker, date, open, high, low, close, closeadj, volume,"
        f"         row_number() OVER (PARTITION BY ticker ORDER BY date DESC) rn"
        f"  FROM {lake.sql_table('sep')} WHERE ticker IN ({placeholders}){bounds}"
        f") WHERE rn <= ? ORDER BY ticker, date ASC",
        [*uniq, *params, int(limit)])
    out: dict[str, list[dict]] = {}
    for tk, grp in df.groupby("ticker", sort=False):
        out[str(tk)] = bars_from_sep_rows(grp.to_dict("records"))
    return out
