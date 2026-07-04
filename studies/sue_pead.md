# Study pre-registration: sue_pead

**Asset:** EQ · **Evaluator:** car · **Tier:** alpha
**Primary horizon:** 21 sessions · **Registered:** 2026-07-04 · **Author:** owner (drafted per plan §6 #2)

## Hypothesis
Post-earnings-announcement drift: prices underreact to standardized earnings
surprises; the top-decile SUE drifts up and the bottom decile down for weeks
(Bernard–Thomas 1989+). The other side is anchoring + slow institutional
rebalancing. Both legs are claimed (§5.7 symmetry): top decile LONG, bottom
decile SHORT (analytic).

## Event definition (exact)
SRW-SUE from free EDGAR XBRL (`app/data/equities/edgar/xbrl_eps.py`):
SUE_q = (EPS_q − EPS_{q−4}) / σ(preceding ≤8 seasonal diffs), ≥6 diffs,
|SUE| winsorized to 10, PIT (earliest-filed facts; diluted + basic-and-diluted
concepts merged, diluted precedence). Event timestamp = the 8-K/Item-2.02
acceptance instant's trading date (BMO/AMC preserved in meta), joined
first-announcement-on/after-period-end, one 8-K per quarter
(`app/data/equities/pead.py`).
**Deciles:** within each fiscal (year, quarter) population of crawled SUEs
(min 20 names/quarter): SUE ≥ q90 ⇒ LONG, ≤ q10 ⇒ SHORT; interior ⇒ no event.
Emitter: `scripts/emit_events.py sue_pead` over the lake `sue_events` crawl.

## Gate
ALPHA (§5.5) on OOS+LIVE at h=21: clustered t ≥ 3.0, n_months ≥ 12, n_events
≥ **60** (quarterly cadence floor), after-tax net > 0 at tier costs,
sign-consistent, placebo clean.

## Known contaminations / caveats
- **Survivorship in the event population:** the SEC ticker→CIK map covers
  current registrants, so the crawl skews to survivors. Controls/segments stay
  PIT; the event-side bias is documented and revisited if a historical CIK map
  lands. PEAD in delisted names is plausibly STRONGER, so the bias is likely
  conservative for the long leg but flattering for the short leg.
- The plan's original Finnhub-based PEAD seed was fabricated by look-ahead
  (edge audit 2026-07-01); this study replaces it wholesale.
- Textbook anomaly (1989+): historical OOS is contaminated by construction.
- Decile boundaries use the CRAWLED population per quarter, not the full
  universe — coverage gaps (loss-year tag quirks, ~66–74% mid/small) shift
  decile cutoffs slightly; documented, applies symmetrically to both legs.
- Collision candidate: insider_cluster (insiders buy after earnings); the
  ±5-session collision report + excluded re-run rule applies.

## Kill criteria
Placebo dirty ⇒ stop-and-fix before any verdict. Long and short legs with
OPPOSITE-sign failures (long works only where short also "works" the same
direction = a market-drift artifact) ⇒ KILLED regardless of pooled t.
