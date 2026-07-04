# archive/v1 — retired pre-lab calibration machinery

Retired 2026-07-04 by the dashboard-redesign P3 pass (see
`DASHBOARD_REDESIGN.md` in the workspace root and `studies/DECISIONS.md`).
Kept for the record, excluded from the runtime and the test suite.

Why retired, not fixed:

- **`app/st_winrates.json`** (BTC swing per-trigger win-rates) — the honest
  recalibration measured every alerted cell statistically indistinguishable
  from a coin flip. Serving a per-trigger "win rate" was false precision.
- **`app/stock_st_winrates.json`** (stock archetype win-rates) — same verdict;
  the original PEAD seed was additionally look-ahead-tainted (edge audit
  2026-07-01). The live PEAD claim now lives in the lab as `sue_pead`
  (EXTEND at retirement) with pre-registered gates.
- **`app/stock_track_record.json`** — backtest-derived; superseded by the live
  forward test (`/api/stock/positions`) and the lab's evaluators.
- **`scripts/*`** — the generators/backtests for the artifacts above, plus the
  M0-era exploratory backtests (`backtest*.py`, `st_history.py`,
  `st_validation.py`). Their statistics (overlapping episodes, no clustering)
  are strictly dominated by `app/harness`.
- **`tests/test_backtest_scripts.py`** — pinned the retired scripts' internals.

Kept alive in the main tree (deliberately): `scripts/calibrate.py` +
`app/calibration.json` (feeds live long-term scoring), `app/track_record.json`
(Evidence card, until the lab covers BTC LT episode hit-rates),
`scripts/backtest_longterm.py` (calibration input), and `app/stock_confidence.py`
(the collector still records a PRIOR-based confidence label — recording only,
never displayed as measured).

The system's honesty rule (design law): every number on screen traces to a lab
verdict or is explicitly unscored context. These artifacts were the third kind.
