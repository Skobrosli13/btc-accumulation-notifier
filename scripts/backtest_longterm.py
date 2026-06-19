"""Composite-level backtest of the long-term accumulation signal (multi-cycle).

The existing track record (scripts/calibrate) only scores the price+macro BACKBONE.
This adds the multi-cycle ON-CHAIN indicators that now have deep free history via
the BGeometrics static files — realized-price ratio (2011+), Reserve Risk, LTH/STH
-SOPR, LTH-MVRV (2012+) — and measures whether including the on-chain layer (the
system's biggest lever) actually improves the historical accumulation signal.

Method (identical yardstick to calibrate._track_record, which this reuses):
walk the daily panel with EXPANDING-window percentiles (each day scored only vs
its own past — no look-ahead), build the SAME category-renormalized composite +
tier the live scorer uses, mark ACCUMULATE/DEEP_VALUE days as the signal, collapse
to independent episodes, and report the forward-return episode hit-rate vs the
unconditional base rate with a bootstrap CI — for the FULL composite AND the
backbone alone, so the on-chain layer's marginal contribution is visible.

Honest scope: EXCLUDES mvrv_z / nupl / sopr / puell (only ~1 free cycle — no static
file), and ssr / net_liq / hash_ribbon (recent-only / no multi-cycle static). One
asset, ~2-3 cycles, timing-neutral. A sanity check, not a promise of edge.

    python -m scripts.backtest_longterm
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config        # noqa: E402
from scripts import calibrate             # noqa: E402

BACKBONE = ["price_to_wma200", "mayer", "m2_yoy", "hy_spread", "real_yield", "nfci"]
ONCHAIN = ["realized_ratio", "reserve_risk", "lth_sopr", "sth_sopr", "lth_mvrv"]


def _panel(cfg) -> pd.DataFrame:
    """Daily panel: price structure + macro (FRED) + on-chain (static files),
    all aligned by date (as-of backward fill for the slower series)."""
    px = calibrate._price_history().sort_values("date").reset_index(drop=True)

    for name, df in calibrate._macro_history(cfg).items():
        px = pd.merge_asof(px, df.sort_values("date"), on="date", direction="backward")

    # On-chain multi-cycle statics.
    for slug in ("reserve_risk", "lth_sopr", "sth_sopr", "lth_mvrv"):
        df = calibrate._bg_static_df(slug)
        if not df.empty:
            px = pd.merge_asof(px, df.rename(columns={"v": slug}).sort_values("date"),
                               on="date", direction="backward")
    # Realized-price ratio = price / realized price (both daily, ~2011+).
    rp = calibrate._bg_static_df("realized_price")
    if not rp.empty:
        px = pd.merge_asof(px, rp.rename(columns={"v": "realized_price"}).sort_values("date"),
                           on="date", direction="backward")
        px["realized_ratio"] = px["close"] / px["realized_price"]
    return px


def _fmt(tr: dict, label: str) -> str:
    out = [f"=== {label} ===",
           f"span {tr['from']} .. {tr['to']}  ({tr['days']} days)  "
           f"signal_days={tr['signal_days']} episodes={tr['signal_episodes']}"]
    for h, c in tr["horizons"].items():
        ci = c.get("ci")
        ci_s = f"[{ci[0]:.2f},{ci[1]:.2f}]" if ci else "n/a"
        eh = c.get("episode_hit_rate")
        br = c.get("base_rate")
        edge = (eh is not None and br is not None and ci and ci[0] > br)
        out.append(f"  {h:>4}: episode_hit={eh if eh is not None else 'n/a'}  "
                   f"base={br if br is not None else 'n/a'}  CI{ci_s}  "
                   f"{'EDGE (CI>base)' if edge else '~base (no clear edge)'}")
    return "\n".join(out)


def main() -> int:
    cfg = load_config()
    print("Building multi-cycle panel (price + macro + on-chain statics)...")
    px = _panel(cfg)
    full_inds = [k for k in BACKBONE + ONCHAIN if k in px.columns]
    backbone_inds = [k for k in BACKBONE if k in px.columns]
    print(f"  rows={len(px)}  full indicators={full_inds}")
    print(f"  on-chain available: {[k for k in ONCHAIN if k in px.columns]}\n")

    full = calibrate._track_record(px, cfg, full_inds)
    backbone = calibrate._track_record(px, cfg, backbone_inds)

    print(_fmt(backbone, "BACKBONE only (price + macro)"))
    print()
    print(_fmt(full, "FULL composite (+ on-chain: realized/reserve-risk/LTH-STH-SOPR/LTH-MVRV)"))
    print("\nepisode_hit vs base with a 90% bootstrap CI. EDGE only if the CI lower "
          "bound clears the base rate. One asset, ~2-3 cycles, timing-neutral — "
          "a sanity check, not a forecast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
