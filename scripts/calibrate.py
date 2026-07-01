"""Offline calibration (run MANUALLY) — emits app/calibration.json + app/track_record.json.

Percentile-rank only makes sense where we have deep, multi-cycle history. So this
calibrates the indicators that have it:
  * price_to_wma200 — deep weekly closes (Kraken ~2013) -> 200-week MA.
  * m2_yoy / hy_spread / real_yield / nfci — FRED full history.
  * reserve_risk / lth_sopr / sth_sopr / lth_mvrv — BGeometrics static files
    (free, no rate limit, back to 2012).
It then backtests that price+macro backbone with EXPANDING-window percentiles
(no look-ahead) and reports a forward-return hit-rate vs the base rate.

On-chain from bitcoin-data.com (~2022+ = ONE cycle) and derivatives KEEP their
economic-logic thresholds — percentile on one cycle would mislead — so they are
deliberately NOT calibrated and NOT in the historical track record. Sentiment
(Fear & Greed) IS calibrated — percentile vs its full 2018+ history — but stays
out of the historical track record.

    python -m scripts.calibrate        # from the project root

The live path only reads the committed app/calibration.json; re-run this to refresh.
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scoring                      # noqa: E402
from app.config import load_config           # noqa: E402
from app.sources import exchange, macro, onchain  # noqa: E402

COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

PROBS = [0.0, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 1.0]
APP_DIR = Path(__file__).resolve().parents[1] / "app"
FORWARD_DAYS = [90, 180, 365]   # forward-return horizons (calendar days, date-joined)
MIN_HISTORY = 60                # don't calibrate an indicator with fewer points
_FWD_TOL_DAYS = 7               # max overshoot when date-joining a forward return
_MAX_GAP_DAYS = 7               # a bigger hole in the daily panel is a failed fetch, not noise
# Must reach back to here to percentile-calibrate (so the history spans the 2022
# bottom + its run-up — otherwise percentile-rank on one regime misleads).
SPAN_CUTOFF = pd.Timestamp("2021-06-01")


# --- data pulls --------------------------------------------------------------

def _coinbase_daily() -> pd.DataFrame:
    """Deep daily BTC-USD closes from Coinbase (since 2015-07, reachable from AWS),
    paginated 300/req. Returns [date, close]; empty when Coinbase is entirely
    unreachable (the caller then falls back to the weekly frame). A window that
    fails AFTER earlier windows succeeded RAISES instead: a silent ~290-day hole
    would stretch every forward horizon computed on the panel."""
    start = datetime(2015, 7, 20, tzinfo=timezone.utc)
    end_all = datetime.now(timezone.utc)
    rows: list[list] = []
    cur = start
    while cur < end_all:
        win_end = min(cur + timedelta(days=290), end_all)
        status: object = None
        try:
            r = requests.get(COINBASE, params={"granularity": 86400,
                             "start": cur.isoformat(), "end": win_end.isoformat()},
                             headers={"User-Agent": "btc-calibrate"}, timeout=30)
            status = f"HTTP {r.status_code}"
            if r.status_code == 200:
                rows += r.json()      # [time, low, high, open, close, volume]
                cur = win_end
                time.sleep(0.25)      # be polite to the public endpoint
                continue
        except Exception as exc:  # noqa: BLE001
            status = f"error: {exc}"
        if not rows:
            # Nothing fetched yet: treat as "Coinbase unreachable" -> weekly fallback.
            return pd.DataFrame(columns=["date", "close"])
        raise RuntimeError(f"Coinbase window {cur.date()}..{win_end.date()} failed "
                           f"({status}); refusing to build a panel with a silent hole")
    if not rows:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df["date"] = (pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_localize(None)
                  .dt.normalize().astype("datetime64[ns]"))
    df["close"] = df["close"].astype(float)
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)[["date", "close"]]


def _price_history() -> pd.DataFrame:
    """Deep daily price -> date, close, mayer (price/200DMA), price_to_wma200
    (price/200-week MA from weekly resample). Coinbase deep daily; falls back to the
    shallower OKX/Kraken WEEKLY frame if Coinbase is unreachable (forward returns
    are date-joined via ``_fwd_idx``, so calendar horizons stay honest on the
    coarser weekly rows). Raises when the daily frame has a hole bigger than
    ``_MAX_GAP_DAYS`` — better no artifact than one with silently stretched horizons."""
    daily = _coinbase_daily()
    if daily.empty:
        df = exchange.klines_history("1w", 800, "BTC-USDT")
        out = df[["open_time", "close"]].copy()
        out["date"] = (pd.to_datetime(out["open_time"]).dt.tz_localize(None)
                       .dt.normalize().astype("datetime64[ns]"))
        out = out.sort_values("date").reset_index(drop=True)
        out["mayer"] = np.nan
        out["price_to_wma200"] = out["close"] / out["close"].rolling(200, min_periods=104).mean()
        return out[["date", "close", "mayer", "price_to_wma200"]]

    daily = daily.set_index("date")
    gaps = daily.index.to_series().diff().dt.days.dropna()
    if not gaps.empty and gaps.max() > _MAX_GAP_DAYS:
        raise RuntimeError(f"daily price panel has a {int(gaps.max())}-day hole ending "
                           f"{gaps.idxmax().date()} — refusing to compute forward returns over it")
    daily["dma200"] = daily["close"].rolling(200, min_periods=200).mean()
    daily["mayer"] = daily["close"] / daily["dma200"]
    weekly = daily["close"].resample("1W").last()
    wma200 = weekly.rolling(200, min_periods=150).mean()
    p2w = (weekly / wma200).reindex(daily.index, method="ffill")
    daily["price_to_wma200"] = p2w
    return daily.reset_index()[["date", "close", "mayer", "price_to_wma200"]]


def _fred_series(sid: str, key: str) -> pd.DataFrame:
    rows = macro._series(sid, key, limit=0)   # oldest->newest full history
    if not rows:
        return pd.DataFrame(columns=["date", sid])
    df = pd.DataFrame(rows, columns=["date", sid])
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.sort_values("date").reset_index(drop=True)


def _fng_history() -> pd.DataFrame:
    """Full Fear & Greed history (alternative.me, free, back to 2018) -> [date, v]."""
    try:
        r = requests.get("https://api.alternative.me/fng/", params={"limit": 0},
                         headers={"User-Agent": "btc-calibrate"}, timeout=30)
        rows = r.json().get("data", []) if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        rows = []
    out = []
    for x in rows:
        try:
            out.append((int(x["timestamp"]), float(x["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        return pd.DataFrame(columns=["date", "v"])
    df = pd.DataFrame(out, columns=["ts", "v"])
    df["date"] = (pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_localize(None)
                  .dt.normalize().astype("datetime64[ns]"))
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)[["date", "v"]]


def _bg_static_df(slug: str) -> pd.DataFrame:
    """Full history from a BGeometrics static file (free, no rate limit, back to
    2012) -> [date, v]. Multi-cycle, so these are calibratable."""
    rows = onchain.bg_history(slug)
    if not rows:
        return pd.DataFrame(columns=["date", "v"])
    df = pd.DataFrame(rows, columns=["ts", "v"])
    df["date"] = (pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
                  .dt.normalize().astype("datetime64[ns]"))
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)[["date", "v"]]


def _macro_history(cfg) -> dict[str, pd.DataFrame]:
    """Per-indicator [date, value] frames (empty dict if no FRED key). Merged onto
    the weekly spine separately so daily spikes (e.g. HY blowouts) aren't resampled away."""
    if not cfg.fred_api_key:
        print("  (no FRED_API_KEY — macro will not be calibrated)")
        return {}
    key = cfg.fred_api_key
    out: dict[str, pd.DataFrame] = {}
    m2 = _fred_series("M2SL", key)
    if not m2.empty:
        m2["m2_yoy"] = m2["M2SL"].pct_change(12) * 100.0
        out["m2_yoy"] = m2[["date", "m2_yoy"]].dropna().reset_index(drop=True)
    hy = _fred_series("BAMLH0A0HYM2", key)
    if not hy.empty:
        out["hy_spread"] = hy.rename(columns={"BAMLH0A0HYM2": "hy_spread"})
    ry = _fred_series("DFII10", key)
    if not ry.empty:
        out["real_yield"] = ry.rename(columns={"DFII10": "real_yield"})
    nfci = _fred_series("NFCI", key)
    if not nfci.empty:
        out["nfci"] = nfci.rename(columns={"NFCI": "nfci"})
    return out


# --- calibration breakpoints -------------------------------------------------

def _breakpoints(values) -> list[float] | None:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if arr.size < MIN_HISTORY:
        return None
    bp = [float(x) for x in np.quantile(arr, PROBS)]
    for i in range(1, len(bp)):   # enforce monotonic non-decreasing
        bp[i] = max(bp[i], bp[i - 1])
    return bp


def _emit_calibration(raw: dict[str, pd.DataFrame]) -> dict:
    """raw: {indicator -> DataFrame[date, v]} at native frequency (deep as available).
    Calibrates only indicators with enough points AND history spanning the cutoff."""
    inds = {}
    for name, df in raw.items():
        df = df.dropna(subset=["v"])
        earliest = df["date"].min() if not df.empty else None
        bp = _breakpoints(df["v"].tolist())
        if bp is None or earliest is None or earliest > SPAN_CUTOFF:
            since = earliest.date() if earliest is not None and pd.notna(earliest) else None
            print(f"  skip {name}: shallow history (n={len(df)}, since={since}) -> economic threshold")
            continue
        inds[name] = {"direction": scoring.DIRECTION[name], "n": int(len(df)),
                      "since": str(earliest.date()), "breakpoints": bp}
        print(f"  calibrated {name}: n={len(df)} since={earliest.date()} "
              f"bp={[round(x, 3) for x in bp]}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "probs": PROBS, "indicators": inds}


# --- track record (expanding-window, no look-ahead) --------------------------

def _fwd_idx(dates, i: int, h: int, tol_days: int = _FWD_TOL_DAYS) -> int | None:
    """Row index of the first date >= dates[i] + ``h`` CALENDAR days (a date join).

    Row-offset indexing (``closes[i + h]``) silently stretches the horizon across
    any data hole — and turns "90d" into 90 WEEKS on the weekly fallback frame.
    None when the sample ends before the horizon, or when the nearest row
    overshoots the target by more than ``tol_days`` (a gap we refuse to paper over).
    ``dates`` must be a sorted datetime64 array."""
    target = dates[i] + np.timedelta64(h, "D")
    j = int(np.searchsorted(dates, target))
    if j >= len(dates) or (dates[j] - target) > np.timedelta64(tol_days, "D"):
        return None
    return j


def _spaced(episode_idx: list[int], dates, h: int) -> list[int]:
    """Greedy subset of episode-start indices whose dates are >= ``h`` days apart,
    so the forward windows of the CI sample do NOT overlap. Adjacent 365d windows
    of episodes ~47 days apart share ~87% of their span — i.i.d.-resampling them
    overstates the evidence; this small subset is the honest per-horizon sample."""
    kept: list[int] = []
    last = None
    for i in episode_idx:
        if last is None or (dates[i] - last) >= np.timedelta64(h, "D"):
            kept.append(i)
            last = dates[i]
    return kept


def _seed_history(panel: pd.DataFrame, calibrated: list[str],
                  native: dict[str, pd.DataFrame] | None = None) -> dict[str, list[float]]:
    """Pre-panel values per indicator, so the expanding ranks aren't cold-started.

    The panel's first scored row is where price_to_wma200 first exists (~2018-06).
    Unseeded, day one ranks every indicator against n=1 (instant saturation) and
    macro ranks against 2018+ data only, while LIVE scoring uses breakpoints from
    each indicator's full native history (m2_yoy back to 1960). ``native`` maps an
    indicator to its full native-frequency frame ([date, v] or [date, <name>]);
    indicators without one seed from the panel's own pre-start rows (native daily
    there — price structure, realized_ratio, the BG statics)."""
    scored = panel.dropna(subset=["price_to_wma200"])
    if scored.empty:
        return {k: [] for k in calibrated}
    start = scored["date"].iloc[0]
    seeds: dict[str, list[float]] = {}
    for name in calibrated:
        vals: list[float] = []
        src = (native or {}).get(name)
        if src is not None and not src.empty:
            col = "v" if "v" in src.columns else name
            sub = src[src["date"] < start].dropna(subset=[col])
            vals = [float(x) for x in sub[col] if np.isfinite(x)]
        elif name in panel.columns:
            sub = panel[panel["date"] < start].dropna(subset=[name])
            vals = [float(x) for x in sub[name] if np.isfinite(x)]
        seeds[name] = vals
    return seeds


def _track_record(panel: pd.DataFrame, cfg, calibrated: list[str],
                  seeds: dict[str, list[float]] | None = None) -> dict:
    rows = panel.dropna(subset=["price_to_wma200"]).reset_index(drop=True)
    hist: dict[str, list[float]] = {k: list((seeds or {}).get(k, [])) for k in calibrated}
    seeded = {k: len(v) for k, v in hist.items() if v}
    comps, tiers, closes = [], [], []
    for _, r in rows.iterrows():
        sub: dict[str, float | None] = {}
        for name in calibrated:
            v = r.get(name)
            if v is None or not np.isfinite(v):
                continue
            hist[name].append(float(v))
            sub[name] = scoring.rank_score(hist[name], float(v), scoring.DIRECTION[name])
        cats = scoring.category_scores(sub)
        comp, _ = scoring.composite(cats, cfg.weights, 1.0)  # timing-neutral for the test
        wma = r["close"] / r["price_to_wma200"] if r["price_to_wma200"] else None
        tiers.append(scoring.tier(comp, r["close"], wma,
                                  cfg.tier_watch, cfg.tier_accumulate, cfg.tier_deepvalue))
        comps.append(comp)
        closes.append(float(r["close"]))
    dates = rows["date"].to_numpy()

    def outcome(i: int, h: int) -> int | None:
        j = _fwd_idx(dates, i, h)
        return None if j is None else (1 if closes[j] > closes[i] else 0)

    def winrate(idxs, h):
        outs = [o for o in (outcome(i, h) for i in idxs) if o is not None]
        return round(sum(outs) / len(outs), 3) if outs else None

    signal_idx = [i for i, t in enumerate(tiers) if t in ("ACCUMULATE", "DEEP_VALUE")]

    # Independent EPISODES: consecutive signal days are autocorrelated, so collapse
    # each run of signal days to one (its start). Daily hit-rate overstates n;
    # episodes are the honest sample size for the confidence interval — and even
    # those overlap at the longer horizons, hence the ``_spaced`` subset below.
    episodes, in_ep = [], False
    for i, t in enumerate(tiers):
        sig = t in ("ACCUMULATE", "DEEP_VALUE")
        if sig and not in_ep:
            episodes.append(i)
        in_ep = sig

    def bootstrap_ci(outcomes, iters=2000):
        if len(outcomes) < 3:
            return None
        rnd = random.Random(42)
        n = len(outcomes)
        rates = sorted(sum(outcomes[rnd.randrange(n)] for _ in range(n)) / n for _ in range(iters))
        return [round(rates[int(0.05 * iters)], 3), round(rates[int(0.95 * iters)], 3)]

    horizons = {}
    for h in FORWARD_DAYS:
        ep_all = [o for o in (outcome(s, h) for s in episodes) if o is not None]
        eff_idx = _spaced(episodes, dates, h)
        ep_eff = [o for o in (outcome(s, h) for s in eff_idx) if o is not None]
        ci = bootstrap_ci(ep_eff)
        br = winrate(list(range(len(closes))), h)
        horizons[f"{h}d"] = {
            # Overlapping DAILY windows — descriptive only (see caveats).
            "signal_hit_rate": winrate(signal_idx, h),
            "base_rate": br,
            "episode_hit_rate": round(sum(ep_all) / len(ep_all), 3) if ep_all else None,
            # Non-overlapping sample: episode starts spaced >= the horizon.
            "episodes_effective": len(ep_eff),
            "episode_hit_rate_effective": (round(sum(ep_eff) / len(ep_eff), 3)
                                           if ep_eff else None),
            "ci": ci,   # 90% bootstrap CI over the NON-OVERLAPPING episode outcomes
            "edge": bool(ci is not None and br is not None and ci[0] > br),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": ("expanding-window percentile composite (price + macro + multi-cycle "
                   "on-chain), timing-neutral; indicator histories seeded with pre-panel "
                   "native data; CI over episode starts spaced >= the horizon"),
        "days": len(closes),
        "from": str(rows["date"].iloc[0].date()) if len(rows) else None,
        "to": str(rows["date"].iloc[-1].date()) if len(rows) else None,
        "signal_days": len(signal_idx),
        "signal_episodes": len(episodes),
        "seeded": seeded,   # pre-panel points each indicator's expanding rank started with
        "horizons": horizons,
        "caveats": [
            "Composite over the deep multi-cycle indicators: price-structure (200WMA/Mayer), macro (FRED), and on-chain (realized ratio, reserve risk, LTH/STH-SOPR, LTH-MVRV).",
            "Excludes one-free-cycle on-chain (MVRV-Z/NUPL/SOPR/Puell), sentiment, and derivatives.",
            "signal_hit_rate / base_rate use fully overlapping daily windows — descriptive only. The ci and 'edge' verdict use episode starts spaced >= the horizon (episodes_effective); at 365d that is a single-digit sample.",
            "Live scoring additionally applies the cycle multiplier (~±5%) and tier hysteresis, both EXCLUDED here; a composite near the 40/60/80 cutoffs can tier differently live.",
            "Category weights, tier cutoffs, redundancy groups and the indicator selection itself are in-sample choices made with full-sample knowledge.",
            "One asset, ~2-3 cycles. Past behavior is not a forecast.",
        ],
    }


def main() -> int:
    cfg = load_config()
    print("Calibrating (deep multi-cycle indicators only)...")
    px = _price_history()
    print(f"  daily closes: {len(px)} "
          f"({px['date'].min().date()} -> {px['date'].max().date()})")
    macro = _macro_history(cfg)

    # Breakpoints from each indicator's full native-frequency history (deep as available).
    raw = {"price_to_wma200": px[["date", "price_to_wma200"]].rename(columns={"price_to_wma200": "v"}),
           "mayer": px[["date", "mayer"]].rename(columns={"mayer": "v"})}
    for name, df in macro.items():
        raw[name] = df.rename(columns={name: "v"})
    fng = _fng_history()
    if not fng.empty:
        raw["fng"] = fng                      # Fear & Greed, percentile vs 2018+ history
    # BGeometrics static-file on-chain metrics (free, multi-cycle) -> percentile.
    for ind in ("reserve_risk", "lth_sopr", "sth_sopr", "lth_mvrv"):
        df = _bg_static_df(ind)
        if not df.empty:
            raw[ind] = df
    calib = _emit_calibration(raw)
    (APP_DIR / "calibration.json").write_text(json.dumps(calib, indent=2))
    print(f"  wrote app/calibration.json ({len(calib['indicators'])} indicators)")

    # Track record over the price/macro backbone PLUS the multi-cycle ON-CHAIN
    # layer (static files) — so the headline reflects the on-chain lever, not just
    # price+macro. (Still excludes mvrv_z/nupl/sopr/puell: one free cycle, no static.)
    px = px.sort_values("date").reset_index(drop=True)
    native: dict[str, pd.DataFrame] = dict(macro)   # full-history frames for seeding
    for name, df in macro.items():
        px = pd.merge_asof(px, df.sort_values("date"), on="date", direction="backward")
    for slug in ("reserve_risk", "lth_sopr", "sth_sopr", "lth_mvrv"):
        df = raw.get(slug)   # reuse the frames fetched above (BG statics are rate-friendly but why refetch)
        if df is not None and not df.empty:
            native[slug] = df
            px = pd.merge_asof(px, df.rename(columns={"v": slug}).sort_values("date"),
                               on="date", direction="backward")
    rp = _bg_static_df("realized_price")
    if not rp.empty:
        px = pd.merge_asof(px, rp.rename(columns={"v": "realized_price"}).sort_values("date"),
                           on="date", direction="backward")
        px["realized_ratio"] = px["close"] / px["realized_price"]
    track_inds = ["price_to_wma200", "mayer", "m2_yoy", "hy_spread", "real_yield",
                  "nfci", "realized_ratio", "reserve_risk", "lth_sopr", "sth_sopr", "lth_mvrv"]
    calibrated = [k for k in track_inds if k in px.columns]
    seeds = _seed_history(px, calibrated, native=native)
    track = _track_record(px, cfg, calibrated, seeds=seeds)
    (APP_DIR / "track_record.json").write_text(json.dumps(track, indent=2))
    print(f"  wrote app/track_record.json ({len(calibrated)} inds): {track['horizons']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
