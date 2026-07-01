"""Regenerate the stock calibration artifacts from the LIVE forward-test.

Reads closed ``stock_positions`` (the out-of-sample record the collector accrues)
and writes:
- ``app/stock_track_record.json`` — the dashboard's live win-rate/expectancy card.
- ``app/stock_st_winrates.json`` — the confidence base rates, but ONLY once enough
  live trades have closed to beat the backtest seed (else the seed is kept, so a
  handful of live trades can't swing confidence around).

Positions voided by a venue rebase (``exit_reason='rebased'``) and entries that
never filled (non-CLOSED status, e.g. expired ``pending`` rows) are excluded from
every aggregate. Promoted cells reuse the backtest's cell shape (``n_months``,
month-clustered dispersion) and carry the ``alignment: announcement_date`` marker
on PEAD — live entries key off the earnings *calendar* (announcement dates), so
live cells are valid by construction.

Run after trades have accumulated (e.g. weekly cron), then commit the JSON — the
API reads these verbatim (a scoring change does NOT update them).

    python -m scripts.stock_calibrate            # print
    python -m scripts.stock_calibrate --write    # write app/*.json
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app import stock_positions, stock_store, store
from app.config import load_config
from scripts.stock_backtest import build_cells, month_key

log = logging.getLogger("stock-calibrate")
_MIN_LIVE_TO_OVERRIDE = 40   # closed trades before live win-rates replace the backtest seed
_APP = Path(__file__).resolve().parents[1] / "app"


def eligible_positions(rows: list[dict]) -> list[dict]:
    """Closed positions that count toward the record.

    Excludes venue-rebase voids (``exit_reason='rebased'`` — the entry bar could
    not be re-verified on the pinned venue) and anything not actually CLOSED
    (e.g. ``pending`` entries that expired unfilled)."""
    out = []
    for r in rows:
        if (r.get("exit_reason") or "") == "rebased":
            continue
        if (r.get("status") or "CLOSED").upper() != "CLOSED":
            continue
        out.append(r)
    return out


def live_cells(closed: list[dict]) -> dict:
    """Per-archetype cells from live closed positions, in the backtest cell shape
    (n / win_rate / expectancy_r / n_months / month dispersion / PEAD alignment)."""
    trades = [{**r, "month": (month_key(r["opened_ts"]) if r.get("opened_ts") else None)}
              for r in closed]
    return build_cells(trades)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config()

    conn = store.connect(cfg.db_path)
    store.init_db(conn)
    stock_store.init_stock_db(conn)
    closed = eligible_positions(stock_store.closed_positions(conn))
    conn.close()

    summary = stock_positions.summarize(closed)
    now = datetime.now(timezone.utc).isoformat()
    n = summary["overall"]["n"]

    track = {
        "note": "Out-of-sample forward-test of the tracker's own closed positions.",
        "available": n > 0, "generated_at": now, "method": "closed stock_positions (single-exit R)",
        "overall": summary["overall"], "archetypes": summary["archetypes"],
        "caveats": [
            "Out-of-sample and small until trades accumulate; not a forecast.",
            "Expectancy (avg R) is the target, not raw win-rate.",
            "Conservative single-exit R (stop / T2 / time-stop); intrabar ties go to the stop.",
            "Venue-rebased (voided) exits and unfilled pending entries are excluded.",
        ],
    }
    print(json.dumps(track, indent=2))

    if args.write:
        (_APP / "stock_track_record.json").write_text(json.dumps(track, indent=2))
        log.info("wrote stock_track_record.json (%d closed trades)", n)
        if n >= _MIN_LIVE_TO_OVERRIDE:
            winrates = {"generated_at": now, "source": "live",
                        "note": "Live out-of-sample win-rates (replaced the backtest seed).",
                        "method": ("closed stock_positions net of costs; rebased/pending "
                                   "excluded; effective-n = n_months (distinct entry months); "
                                   "PEAD cells are announcement-date aligned (live entries "
                                   "key off the earnings calendar)"),
                        "archetypes": live_cells(closed)}
            (_APP / "stock_st_winrates.json").write_text(json.dumps(winrates, indent=2))
            log.info("wrote stock_st_winrates.json from LIVE data")
        else:
            log.info("only %d live trades (<%d) — kept the backtest seed win-rates",
                     n, _MIN_LIVE_TO_OVERRIDE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
