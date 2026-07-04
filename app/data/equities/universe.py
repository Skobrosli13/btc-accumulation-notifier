"""Point-in-time universe builder — §4.4.

A nightly + historical snapshot of the tradeable universe as it stood on each
date, INCLUDING since-delisted names (their SEP/DAILY history exists up to the
delisting, so a historical date naturally re-includes them — that is the whole
point of building membership from as-of data rather than today's listing).

Snapshot row:
  date, permaticker, ticker, mcap, dollar_vol_20d, price, tier, sector,
  days_since_earnings, excluded, included

Cap tiers (USD market cap) and liquidity floors are pure and table-driven. Join
research data on ``permaticker`` (a ticker is reused across companies); ``ticker``
rides along for display only.
"""
from __future__ import annotations

# Cap tiers in USD market cap. Below the micro floor ($50M) a name is out.
TIER_BOUNDS = (
    ("micro", 50e6, 300e6),
    ("small", 300e6, 2e9),
    ("mid", 2e9, 10e9),
    ("large", 10e9, float("inf")),
)
MIN_PRICE = 3.0
# 20-day average dollar volume floor by tier (micro/small looser, mid/large tighter).
DOLLAR_VOL_FLOOR = {"micro": 1e6, "small": 1e6, "mid": 5e6, "large": 5e6}


def classify_tier(mcap_usd: float | None) -> str | None:
    """Cap tier from USD market cap, or None if below the $50M micro floor."""
    if mcap_usd is None or mcap_usd < TIER_BOUNDS[0][1]:
        return None
    for name, lo, hi in TIER_BOUNDS:
        if lo <= mcap_usd < hi:
            return name
    return None


def dollar_vol_floor(tier: str | None) -> float:
    return DOLLAR_VOL_FLOOR.get(tier, 5e6)


def is_liquid(price: float | None, dollar_vol_20d: float | None, tier: str | None) -> bool:
    """Price >= $3 and 20d average dollar volume >= the tier floor."""
    if tier is None:
        return False
    if price is None or price < MIN_PRICE:
        return False
    if dollar_vol_20d is None or dollar_vol_20d < dollar_vol_floor(tier):
        return False
    return True


def classify(mcap_usd: float | None, price: float | None,
             dollar_vol_20d: float | None, *, excluded: bool = False) -> dict:
    """Tier + membership for one name. ``excluded`` (owner list / MNPI guard)
    forces out even a liquid name."""
    tier = classify_tier(mcap_usd)
    included = (not excluded) and is_liquid(price, dollar_vol_20d, tier)
    return {"tier": tier, "excluded": bool(excluded), "included": included}


def dollar_volume_20d(closes: list[float], volumes: list[float]) -> float | None:
    """Mean close*volume over the trailing (up to) 20 sessions; None if empty."""
    pairs = [(c, v) for c, v in zip(closes, volumes)
             if c is not None and v is not None][-20:]
    if not pairs:
        return None
    return sum(c * v for c, v in pairs) / len(pairs)


def build_from_lake(lake, as_of: str, *, excluded_tickers=frozenset(),
                    common_only: bool = True) -> list[dict]:
    """PIT universe snapshot as of ``as_of`` from the lake (TICKERS + DAILY + SEP).

    Joins on ticker (within SEP-covered securities), pulls the latest DAILY
    market cap (Sharadar reports it in $millions -> ×1e6) and the trailing-20d
    price + dollar volume from SEP, then classifies each name. Needs TICKERS +
    DAILY + SEP ingested; returns [] if any is missing.
    """
    for t in ("tickers", "daily", "sep"):
        if not lake.exists(t):
            return []
    cat = "AND category LIKE '%Common Stock%'" if common_only else ""
    sql = f"""
        WITH secs AS (
            SELECT DISTINCT permaticker, ticker, sector, category
            FROM {lake.sql_table('tickers')}
            WHERE "table" = 'SEP' {cat}
        ),
        mcap AS (
            SELECT ticker, marketcap FROM (
                SELECT ticker, marketcap,
                       row_number() OVER (PARTITION BY ticker ORDER BY date DESC) rn
                FROM {lake.sql_table('daily')} WHERE date <= ?
            ) WHERE rn = 1
        ),
        px AS (
            SELECT ticker, max_by(close, date) AS price,
                   avg(close * volume) AS dollar_vol_20d
            FROM (
                SELECT ticker, date, close, volume,
                       row_number() OVER (PARTITION BY ticker ORDER BY date DESC) rn
                FROM {lake.sql_table('sep')} WHERE date <= ?
            ) WHERE rn <= 20
            GROUP BY ticker
        )
        SELECT s.permaticker, s.ticker, s.sector,
               m.marketcap * 1e6 AS mcap_usd, p.price, p.dollar_vol_20d
        FROM secs s JOIN mcap m USING(ticker) JOIN px p USING(ticker)
    """
    df = lake.query(sql, [as_of, as_of])
    return build_snapshot(as_of, df.to_dict("records"), excluded_tickers)


def build_snapshot(as_of: str, rows: list[dict],
                   excluded_tickers=frozenset()) -> list[dict]:
    """Assemble the PIT universe snapshot for ``as_of`` from per-name rows already
    joined on permaticker: each carries permaticker, ticker, sector, mcap_usd,
    price, dollar_vol_20d, and optionally days_since_earnings.

    A name whose data exists only up to a past date (since delisted) still lands
    in that date's snapshot — membership is built from as-of data, not today's
    listing status.
    """
    excluded_tickers = set(excluded_tickers)
    out: list[dict] = []
    for r in rows:
        excluded = r.get("ticker") in excluded_tickers
        c = classify(r.get("mcap_usd"), r.get("price"), r.get("dollar_vol_20d"),
                     excluded=excluded)
        out.append({
            "date": as_of,
            "permaticker": r.get("permaticker"),
            "ticker": r.get("ticker"),
            "mcap": r.get("mcap_usd"),
            "dollar_vol_20d": r.get("dollar_vol_20d"),
            "price": r.get("price"),
            "tier": c["tier"],
            "sector": r.get("sector"),
            "days_since_earnings": r.get("days_since_earnings"),
            "excluded": c["excluded"],
            "included": c["included"],
        })
    return out
