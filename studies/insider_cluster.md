# Study pre-registration: insider_cluster

**Asset:** EQ · **Evaluator:** car · **Tier:** alpha
**Primary horizon:** 21 sessions · **Registered:** 2026-07-04 · **Author:** owner (drafted per plan §6 #1)

## Hypothesis
Clustered open-market buying by multiple officers/directors is the strongest
documented insider signal (single buys are noise; clusters are conviction).
Sellers on the other side are diversifying/liquidity traders; the information
asymmetry is legal and repeated-game (Cohen–Malloy–Pomorski 2012).

## Event definition (exact)
Sharadar SF2, formtype 3/4/5 rows with transactioncode='P' (open-market buy) and
isofficer='Y' OR isdirector='Y'. Within a trailing **14-day** window on
transaction dates, **≥2 distinct owners** with aggregate value **≥ $50,000**
(transactionvalue, falling back to price×shares) ⇒ ONE LONG event stamped at
the **latest filing date** among contributing buys (the cluster's public-
knowledge instant). A cluster emits once; a ≥14-day quiet gap closes it.
Strength ×1.5 when any contributing title matches CEO/CFO.
**Exclusions:** routine insiders — same calendar month bought in ≥2 of the
prior 3 years (per ticker+owner) — dropped BEFORE clustering. 10b5-1 plans:
SF2 carries no flag; the routine filter is the pre-registered proxy
(limitation documented).
Implementation: `app/events/insider_cluster.py` (fixture-tested).

## Gate
ALPHA (§5.5) on OOS+LIVE at h=21: clustered t ≥ 3.0, n_months ≥ 12, n_events
≥ 100, after-tax net > 0 at tier costs, sign-consistent pre/post-2020*, placebo
clean. (*data starts 2016 ⇒ pre-2020 sub-window is 2016–2019.)

## Known contaminations / caveats
- SF2 in the purchased bundle starts **2008**, SEP/DAILY **2016** ⇒ evaluable
  events start 2016; IS = 2016–2021 (~6y, one regime-cycle), OOS = 2022→reg.
- Hypothesis is literature-known since 2012 ⇒ historical OOS is *less
  contaminated*, not clean (§9); LIVE is the real test.
- Collision candidates: sue_pead (insiders buy after earnings); the collision
  report + excluded re-run applies (§5.2).

## Kill criteria
Beyond the gate: significance that survives only WITH sue_pead collisions ⇒
KILLED-duplicate; placebo dirty ⇒ stop-and-fix the harness before any verdict.
