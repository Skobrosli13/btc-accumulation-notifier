"""Regenerate the stock calibration artifacts from the LIVE forward-test.

Reads closed ``stock_positions`` (the out-of-sample record the collector accrues)
and writes:
- ``app/stock_track_record.json`` — the dashboard's live win-rate/expectancy card.
- ``app/stock_st_winrates.json`` — the confidence base rates. Live promotion
  MERGES into the backtest seed instead of overwriting it: an archetype with
  >= ``_MIN_LIVE_TO_OVERRIDE`` live closed trades gets its ``n`` / ``win_rate`` /
  ``expectancy_r`` (and month fields) replaced by the live record and is tagged
  ``source: "live"``, while the seed's ``baseline_*`` control fields are PRESERVED
  and ``not_significant`` is recomputed against that stored baseline (Wilson 95%
  lower bound of the live win-rate must beat ``baseline_win_rate``; stays True
  when the seed carries no baseline — a live record without a control can't buy
  the EDGE label). Archetypes below the threshold keep their seed cell verbatim,
  so the two generators no longer fight over the file: a backtest regen refreshes
  the seed/baselines, this script layers the live record on top.

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
_MIN_LIVE_TO_OVERRIDE = 40   # live closed trades (per archetype) before a cell is promoted
_APP = Path(__file__).resolve().parents[1] / "app"

_MERGED_METHOD = (
    "backtest seed merged with LIVE closed stock_positions (net of costs; "
    "rebased/pending excluded): archetypes with >= "
    f"{_MIN_LIVE_TO_OVERRIDE} live trades carry live n/win_rate/expectancy_r/"
    "n_months (source='live') while the seed's baseline_* control fields are "
    "preserved; not_significant = NOT (Wilson 95% lower bound of the live "
    "win_rate > seed baseline_win_rate), True when the seed has no baseline; "
    "sub-threshold archetypes keep the seed cell verbatim; PEAD cells are "
    "announcement-date aligned (live entries key off the earnings calendar)")


def wilson_lo(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial proportion — the conservative end
    of the win-rate's 95% interval. 0.0 when n == 0 (no information)."""
    if n <= 0:
        return 0.0
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (center - margin) / denom)


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


def merged_winrates(seed: dict | None, closed: list[dict], now: str) -> dict | None:
    """Merge live cells into the backtest seed (never overwrite it wholesale).

    Archetypes with >= ``_MIN_LIVE_TO_OVERRIDE`` live closed trades get their
    n / win_rate / expectancy_r / n_months / expectancy_r_month_std replaced by
    the live record, are tagged ``source: 'live'``, keep the seed's ``baseline_*``
    keys (and the ``alignment`` marker), and have ``not_significant`` recomputed as
    NOT (wilson_lo(live win_rate, live n) > seed baseline_win_rate) — True when
    the seed carries no baseline. ``delta_*`` are re-derived against the preserved
    baseline so the cell stays internally consistent. Archetypes below the
    threshold keep their seed cell verbatim. Returns None when no archetype
    qualifies (keep the committed seed untouched)."""
    live = live_cells(closed)
    promoted = {k: v for k, v in live.items()
                if int(v.get("n") or 0) >= _MIN_LIVE_TO_OVERRIDE}
    if not promoted:
        return None
    seed = seed or {}
    arch: dict[str, dict] = {k: dict(v) for k, v in (seed.get("archetypes") or {}).items()}
    for k, lv in promoted.items():
        cell = dict(arch.get(k) or {})
        cell.update({f: lv[f] for f in ("n", "win_rate", "expectancy_r",
                                        "n_months", "expectancy_r_month_std") if f in lv})
        cell["source"] = "live"
        if "alignment" in lv:        # live PEAD keys off the earnings calendar
            cell["alignment"] = lv["alignment"]
        base_wr = cell.get("baseline_win_rate")
        if base_wr is not None and lv.get("win_rate") is not None:
            cell["not_significant"] = not (
                wilson_lo(lv["win_rate"], int(lv["n"])) > base_wr)
            cell["delta_win_rate"] = round(lv["win_rate"] - base_wr, 3)
            if (cell.get("baseline_expectancy_r") is not None
                    and lv.get("expectancy_r") is not None):
                cell["delta_expectancy_r"] = round(
                    lv["expectancy_r"] - cell["baseline_expectancy_r"], 3)
        else:
            cell["not_significant"] = True   # no stored control -> can't claim edge
        arch[k] = cell
    out = dict(seed)
    out.update({
        "generated_at": now,
        "source": "live+seed",
        "note": ("Backtest seed with LIVE out-of-sample cells merged in (archetypes "
                 f"with >= {_MIN_LIVE_TO_OVERRIDE} live closed trades; seed baseline_* "
                 "fields preserved)."),
        "method": _MERGED_METHOD,
        "archetypes": arch,
    })
    return out


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
        seed_path = _APP / "stock_st_winrates.json"
        try:
            seed = json.loads(seed_path.read_text())
        except (OSError, ValueError):
            seed = None
        merged = merged_winrates(seed, closed, now)
        if merged is not None:
            seed_path.write_text(json.dumps(merged, indent=2))
            promoted = sorted(k for k, v in merged["archetypes"].items()
                              if v.get("source") == "live")
            log.info("merged LIVE cells into stock_st_winrates.json: %s",
                     ", ".join(promoted))
        else:
            log.info("no archetype has >= %d live closed trades — kept the seed "
                     "win-rates verbatim", _MIN_LIVE_TO_OVERRIDE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
