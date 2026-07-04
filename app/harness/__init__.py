"""EDGE-LAB evaluation harness (§5).

Signals are event emitters; ONLY this package computes performance; only the
pre-registered gates promote. Pure, I/O-free modules (fixture-tested):

- :mod:`~app.harness.stats`       — month-clustered t, spaced de-correlation,
  winsorize, bootstrap/Wilson CIs (ported from the audited perf/backtest code)
- :mod:`~app.harness.walkforward` — IS / OOS / LIVE segmenting + embargo
- :mod:`~app.harness.costs`       — per-tier round-trip cost model
- :mod:`~app.harness.tax`         — gross / net / after-tax expectancy
- :mod:`~app.harness.car`         — cross-sectional CAR evaluator (equities)
- :mod:`~app.harness.ts_study`    — single-asset block-bootstrap evaluator (BTC)
- :mod:`~app.harness.placebo`     — shuffle suite (harness self-test)
- :mod:`~app.harness.gates`       — ALPHA / POLICY / PREMIUM verdicts
- :mod:`~app.harness.schema`      — events / studies / study_results DDL

Orchestration (lake reads, DB writes, git-SHA stamping) lives in
``scripts/study.py`` — never in these modules.
"""
