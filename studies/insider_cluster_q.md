# Study pre-registration: insider_cluster_q

**Asset:** EQ · **Evaluator:** car · **Tier:** alpha
**Primary horizon:** 63 sessions (one quarter) · **Registered:** 2026-07-06 · **Author:** owner

## Hypothesis
The promoted `insider_cluster` signal (clustered open-market officer/director
buying) is measured strongest at short horizons but its mean drift keeps *rising*
with the hold: OOS mean CAR climbs +1.2% → +3.9% from h5 → h63, and the
after-tax edge is LARGEST at a quarter (+2.33%/event at h63 vs +0.91% at the
h21 primary). This sibling asks the position-management question the h21 study
did not pre-commit to: **does the cluster-buy edge survive, cleanly, out to a
one-quarter hold?** It is the *same event*, held longer — not a new discovery.
Whoever sells to a conviction insider cluster is still the diversifying/liquidity
trader; the drift simply takes a quarter to fully play out.

## Event definition (exact)
IDENTICAL to `insider_cluster` — shares `app/events/insider_cluster.py`
(`cluster_events`) via a shared emitter body in `scripts/emit_events.py`, tagged
`study="insider_cluster_q"`. SF2 formtype 3/4/5, transactioncode='P',
isofficer='Y' OR isdirector='Y'; ≥2 distinct owners aggregating ≥ $50k in a
trailing 14-day transaction-date window ⇒ one LONG event at the latest
contributing filing date; routine-insider (same calendar month in ≥2 of prior 3
years) excluded before clustering; CEO/CFO ⇒ strength 1.5. The ONLY difference
from `insider_cluster` is `primary_horizon=63`, so the ALPHA gate is applied on
the quarter-hold results and the placebo is re-run at h=63.

## Gate
ALPHA (§5.5) on OOS+LIVE at **h=63**: clustered t ≥ 3.0, n_months ≥ 12,
n_events ≥ 100, after-tax net > 0 at tier costs, sign-consistent IS vs OOS,
placebo clean **at h=63**. (Placebo MUST be re-run at the quarter horizon: wider
forward windows overlap more, so the h21 placebo does not transfer — the
clone13f lesson.)

## Known contaminations / caveats
- **Deliberate 100% collision with `insider_cluster`.** The two share every
  event by construction, so `collision_report` will read ~100% overlap. This is
  NOT the killed-duplicate case (§5.2): the point is not an independent alpha but
  the *same* alpha measured at a longer hold. The h21 sibling remains the live
  short-hold surface; this one characterizes the quarter hold. Any promotion is
  reported as "insider_cluster held to a quarter", never as a second independent
  edge.
- Same window limits as `insider_cluster`: SEP/DAILY start 2016 ⇒ IS 2016–2021,
  OOS 2022→registration; hypothesis literature-known (CMP 2012) ⇒ OOS is
  less-contaminated, not clean; LIVE is the real test.
- Longer holds = fewer independent months at a given event count; watch n_months
  and the placebo exceedance closely (structurally the weakest leg here).

## Kill criteria
Placebo dirty at h=63 (exceedance > 10%) ⇒ stop-and-fix, no verdict. A sign flip
IS↔OOS at h=63 ⇒ KILLED. If the quarter-hold t falls below 3.0 it is a SOFT miss
(EXTEND once) — but given h21 is already the promoted surface, an EXTEND here just
means "hold to a quarter is not independently promotable; keep the month hold".
