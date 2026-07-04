# Study pre-registration: clone13f

**Asset:** EQ · **Evaluator:** car · **Tier:** alpha
**Primary horizon:** 63 sessions · **Registered:** 2026-07-04 · **Author:** owner (drafted per plan §6 #4)

## Hypothesis
New positions initiated by CONCENTRATED, LOW-TURNOVER, mid-sized managers carry
information worth cloning even at the 13F disclosure lag (Cohen–Polk–Silli
"best ideas"; Martin/Puthenpurackal cloning literature). Index-huggers and
mega-funds don't — hence the filters. Long horizon (63 sessions) because the
edge, if any, is slow.

## Event definition (exact)
Sharadar SF3, securitytype='SHR' only. Per (investor, quarter):
- **AUM** = Σ value ∈ [$100M, $5B]; **holdings** = #tickers ≤ 30 — both at the
  signal quarter;
- **turnover** ≤ 25%/yr: quarterly turnover_t = (#added + #dropped) /
  (2 × #held_{t−1}); annualized = mean over the trailing ≤4 consecutive
  transitions × 4; requires ≥2 prior consecutive transitions (a manager's
  early history can't qualify);
- consecutive calendar quarters only (a reporting gap breaks the chain).
**Event:** ticker present at Q, absent at Q−1, from a qualifying manager.
Aggregated per (ticker, quarter): strength = #qualifying managers adding it;
meta carries the manager list. Direction LONG only (13F shows longs).
**Event timestamp = quarter-end + 45 days** (the statutory 13F deadline —
SF3 carries no per-filing acceptance date, so the deadline is the only
timestamp at which the information is GUARANTEED public; never earlier ⇒ no
look-ahead possible, at the cost of ~2–6 weeks of staleness for early filers).
Implementation: `app/events/clone13f.py` (pure, fixture-tested).

## Gate
ALPHA (§5.5) on OOS+LIVE at h=63: clustered t ≥ 3.0, n_months ≥ 12, n_events
≥ 100, after-tax net > 0 at tier costs, sign-consistent, placebo clean.

## Known contaminations / caveats
- SF3 quarters 2013-12+ but SEP prices 2016-01+ ⇒ evaluable events 2016+.
- The 45-day convention makes entries systematically LATER than real cloners
  who watch filings daily — measured edge is a lower bound on filing-day edge.
- 13F omits shorts/options context (a "new long" may hedge something unseen).
- Collision candidates: insider_cluster, sue_pead (same names, adjacent
  catalysts) — collision report + excluded re-run applies.

## Kill criteria
Placebo dirty ⇒ stop-and-fix. Significance that survives only WITH collisions
⇒ KILLED-duplicate. Edge concentrated entirely in micro tier at 80bps costs ⇒
KILLED (unharvestable).
