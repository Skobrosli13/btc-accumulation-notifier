# Study pre-registration: sue_pead_confirmed

**Asset:** EQ · **Evaluator:** car · **Tier:** alpha
**Primary horizon:** 21 sessions · **Registered:** 2026-07-06 · **Author:** owner

## Hypothesis
The naked SUE decile drift (`sue_pead`) decayed out-of-sample (OOS t=0.87@h21) —
a 35-year-old published anomaly largely arbitraged away. But the drift historically
concentrates where the surprise is CREDIBLE: when the market moved WITH the
earnings surprise on the announcement (an initial under-reaction in the surprise's
direction), not when it shrugged the number off (Chan–Jegadeesh–Lakonishok 1996;
Livnat–Mendenhall 2006). This study conditions the top-SUE-decile LONG on a
positive announcement-day reaction — a smaller, higher-precision pick list of
*confirmed* positive surprises — and asks whether the drift survives OOS there.

## Event definition (exact)
Reuses `sue_events` (SRW-SUE, 8-K/Item-2.02 acceptance, PIT) via
`scripts/emit_events.emit_sue_pead_confirmed`:
1. Per fiscal (year, quarter) crawled population (≥20 names), take the **top SUE
   decile** (`sue >= q0.90`) — LONG only. **The bottom-decile SHORT leg is dropped**
   (the SEC ticker→CIK crawl is survivorship-biased against delisted names, which
   flatters a short leg).
2. **Confirmation filter:** announcement-day reaction = closeadj[report_date] /
   closeadj[prior trading bar] − 1 (SEP total return). Keep only events with
   **reaction > 0**. Look-ahead-safe: the reaction ends at report_date's close and
   the car evaluator enters at the NEXT session's open, so the filter uses only
   pre-entry information.
3. Event stamped at the 8-K acceptance instant (`report_ts`), direction LONG,
   strength = |SUE|.
Any change to the decile cut, reaction rule, or SHORT-leg policy re-registers as
`sue_pead_confirmed-v2` (old rows freeze).

## Gate
ALPHA (§5.5) on OOS+LIVE at h=21: clustered t ≥ 3.0, n_months ≥ 12, n_events ≥
100 (60 quarterly floor applies — earnings cadence), after-tax net > 0 at tier
costs, sign-consistent IS vs OOS, placebo clean.

## Known contaminations / caveats
- Confirmation-filter limitation: for after-market (AMC) 8-Ks the event-date move
  can precede the true reaction — a noise source in the filter, documented; it is
  NOT look-ahead (window ends before entry).
- Collision candidate: `insider_cluster` (insiders sometimes buy after a strong
  print) — the ±5-session collision report applies; a promotion that survives only
  WITH insider collisions is a duplicate, not a new edge (§5.2), so the collision
  note is checked before any verdict is trusted.
- Same window limits as sue_pead: SEP 2016+, OOS 2022→reg; the crawl skews to
  survivors (~66–74% mid/small coverage).
- Selection on positive reaction shrinks the population ~half — n_events is a
  binding leg; if OOS falls below the floor this SOFT-misses on sample size.

## Kill criteria
Placebo dirty ⇒ stop-and-fix. Sign flip IS↔OOS ⇒ KILLED. Survives only WITH
`insider_cluster` collisions ⇒ KILLED-duplicate. A soft miss on t/n ⇒ EXTEND once
— but note the parent `sue_pead` is already on its single EXTEND, so this sibling
carries its own (independent) extension budget.
