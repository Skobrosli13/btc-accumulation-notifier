"""End-to-end Sharadar-native screen (§4 M1 acceptance: screener on Sharadar+SRW-SUE).

Proves the whole equities data path runs off the lake instead of Yahoo/massive/
Finnhub:
  universe  <- data.equities.universe.build_from_lake (TICKERS+DAILY+SEP, PIT tiers)
  prices    <- data.equities.prices.sep_bars          (split/div-adjusted SEP)
  features  <- stock_scoring.features                 (the existing pure scorer)
  PEAD      <- data.equities.pead.sue_events          (SRW-SUE surprise x 8-K timing)

Ranks the liquid universe by 3-month momentum and prints the top names, then
shows the most recent SUE-PEAD event for each (free EDGAR, needs a CIK). This is
a read-only demonstration screen; the calibrated live collector cutover (and the
deletion of the Finnhub/massive adapters) is a separate, deliberate step because
it changes the PEAD scoring unit (%-surprise -> SUE sigma) and needs re-calibration.

    python -m scripts.screen_sharadar [--as-of YYYY-MM-DD] [--top 20] [--pead]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import stock_scoring                          # noqa: E402
from app.config import load_config                     # noqa: E402
from app.data.equities import prices, universe         # noqa: E402
from app.data.equities import pead                     # noqa: E402
from app.data_lake import Lake                          # noqa: E402
from app.sources.stocks import universe as sec         # noqa: E402  (CIK map for PEAD)


def screen(as_of: str | None = None, top: int = 20, want_pead: bool = False) -> list[dict]:
    cfg = load_config()
    lake = Lake(cfg.data_lake_path)
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    uni = [r for r in universe.build_from_lake(lake, as_of) if r["included"]]
    if not uni:
        print("universe empty — ingest TICKERS/DAILY/SEP first "
              "(python -m scripts.ingest SEP --bulk, etc.)")
        return []
    scored = []
    for r in uni:
        bars = prices.sep_bars(lake, r["ticker"], limit=300)
        feat = stock_scoring.features(bars) if bars else None
        if not feat or feat.get("ret_63") is None:
            continue
        scored.append({**r, "ret_63": feat["ret_63"], "rsi": feat.get("rsi"),
                       "from_52w_high": feat.get("from_52w_high")})
    scored.sort(key=lambda x: x["ret_63"], reverse=True)
    out = scored[:top]

    print(f"\nSharadar universe as of {as_of}: {len(uni)} liquid names, "
          f"{len(scored)} priced. Top {len(out)} by 3-month momentum:\n")
    print(f"  {'ticker':<8}{'tier':<7}{'ret_63':>9}{'rsi':>7}{'52wH':>8}  sector")
    for r in out:
        print(f"  {r['ticker']:<8}{r['tier']:<7}{r['ret_63']*100:>8.1f}%"
              f"{(r['rsi'] or 0):>7.0f}{(r['from_52w_high'] or 0)*100:>7.1f}%  {r['sector']}")

    if want_pead:
        cikmap = sec.sec_ticker_map(cfg.sec_user_agent)
        print("\nMost recent SUE-PEAD event (free EDGAR):")
        for r in out[:min(5, len(out))]:
            meta = cikmap.get(r["ticker"].upper()) or {}
            cik = meta.get("cik")
            evs = pead.sue_events(cik, cfg.sec_user_agent) if cik else []
            if evs:
                e = evs[0]
                print(f"  {r['ticker']:<8} SUE {e['sue']:+.2f}  FY{e['fy']}Q{e['quarter']}"
                      f"  {e['hour'] or 'intraday'}  period_end {e['period_end']}")
            else:
                print(f"  {r['ticker']:<8} (no EDGAR SUE event)")
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Sharadar-native equity screen")
    p.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--pead", action="store_true", help="also show SUE-PEAD events (slower; EDGAR)")
    args = p.parse_args(argv)
    screen(as_of=args.as_of, top=args.top, want_pead=args.pead)


if __name__ == "__main__":
    main()
