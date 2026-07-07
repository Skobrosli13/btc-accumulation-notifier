# Study pre-registration: insider_cluster_hi

**Asset:** EQ · **Evaluator:** car · **Tier:** alpha
**Primary horizon:** 21 sessions · **Registered:** 2026-07-06 · **Author:** owner

## Hypothesis
Cohen–Malloy–Pomorski (2012) find the insider edge concentrates in the most
*conviction-revealing* trades: opportunistic (non-routine) buys, by senior
executives, in meaningful size. The promoted `insider_cluster` (≥2 owners,
≥$50k) pools all clusters. This study asks whether a **higher-conviction subset**
— a cluster of **≥ $250k** aggregate in which a **CEO or CFO** personally
participated — earns a materially larger, cleaner edge, i.e. a smaller but
higher-precision pick list. The other side (diversifying/liquidity sellers) is
unchanged; the claim is signal *concentration*, not a new mechanism.

## Event definition (exact)
Same clustering machinery as `insider_cluster` (`app/events/insider_cluster.py`,
shared SF2 code-P officer/director fills, 14-day transaction window, routine
excluded, stamped at latest contributing filing) with TWO tightenings, applied
in `scripts/emit_events.emit_insider_cluster_hi`:
1. aggregate cluster value **≥ $250,000** (`min_agg_usd=250_000`), and
2. **CEO/CFO participation required** — keep only events with strength ≥ 1.5
   (an executive title matched among the contributing buys).
LONG only, primary horizon 21. Any change to these thresholds re-registers as
`insider_cluster_hi-v2` (old rows freeze).

## Gate
ALPHA (§5.5) on OOS+LIVE at h=21: clustered t ≥ 3.0, n_months ≥ 12, n_events
≥ 100, after-tax net > 0 at tier costs, sign-consistent IS vs OOS, placebo clean.
Because the filter is strict, **n_events is the leg most at risk** — if the OOS
population falls below 100 this SOFT-misses (EXTEND once) on sample size, not
signal.

## Known contaminations / caveats
- **Subset collision with `insider_cluster` is expected and large** — every
  event here is also an `insider_cluster` event by construction. This is NOT the
  killed-duplicate case: the claim is that the *stricter* subset concentrates the
  edge. The comparison of interest is hi's OOS t / after-tax vs the base's
  (+0.91%/event @ h21); a hi edge that is not visibly larger than the base is a
  null refinement (report as "no concentration benefit"), not a second edge.
- Same window limits as `insider_cluster` (SEP 2016+, OOS 2022→reg); CMP is
  literature-known ⇒ OOS less-contaminated, not clean; LIVE is the real test.
- Strictness trades sample for conviction; watch n_events and n_months.

## Kill criteria
Placebo dirty ⇒ stop-and-fix. Sign flip IS↔OOS ⇒ KILLED. If n_events/t miss on
sample size ⇒ EXTEND (weaker than the base's promotion, meaning the tightening
did not help and the base `insider_cluster` remains the surface to trade).
