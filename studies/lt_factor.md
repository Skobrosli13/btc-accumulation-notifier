# Study pre-registration: lt_factor

**Asset:** EQ · **Evaluator:** portfolio · **Tier:** (special — see gate)
**Primary horizon:** monthly rebalance · **Registered:** 2026-07-04 · **Author:** owner (drafted per plan §5.5/§6/§10)

## Hypothesis
Quality-gated value + momentum is the best-evidenced free equity edge (Fama-
French, Novy-Marx, Piotroski, Sloan; decades of OOS/international support). A
long portfolio of cheap AND working AND financially-safe names should beat both
the naive equal-weight universe and a passive value+quality ETF blend, net of
turnover cost, over the long run.

## Event / signal definition (exact)
Monthly rebalance. Universe = the PIT included universe (`build_from_lake`),
tier ∈ {small, mid, large} (micro excluded — factor investing in micro is
high-cost). Fundamentals = Sharadar SF1 **ART** (as-reported TTM), latest
`datekey ≤ month-end` (point-in-time). Momentum = 12-1 total return from adjusted
SEP (`price[M-1]/price[M-13]-1`, skipping the most recent month). Screener
`app/lt/factor_screener` (fixture-tested):
- **Gate** (value-trap purge): netinc>0 & opinc>0, gross profitability>0,
  current ratio ≥1, debt/equity ≤4, 12-1 momentum>0, net issuance ≤5% mktcap.
- **Value** (top-quintile required): earnings/EBITDA/FCF/shareholder yields.
- **Quality** (top-half required): gross profitability, ROIC, net margin,
  accruals quality (CFO−NI)/assets.
- **Momentum** (positive required).
Select the top 30 gate-survivors by mean pillar percentile, equal-weight, held
one month; entry at the decision month-end close (all inputs ≤ that close).
Portfolio return netted of turnover cost (names replaced/N × 20 bps RT).

## Gate (§5.5 — the one special outcome)
Scored iff, on the OOS window (2022→now, ≥36 months), the monthly active-return
clustered t ≥ **2.0** vs BOTH the equal-weight PIT universe AND the 50/50
VTV+QUAL ETF blend. Else labeled **"Watchlist (unscored factor screen)"** —
no third state. (The portfolio evaluator runner applies `gates.lt_factor_verdict`;
maps to study status PROMOTED = scored, WATCHLIST = unscored.)

## Known contaminations / caveats
- SF1/SEP start 2016 ⇒ rebalances 2017-02+ (need 13 months for momentum); OOS
  = 2022+ (~54 months ≥ 36). One decade, no 2008 bear.
- Factor definitions are literature-known ⇒ historical OOS is less-contaminated,
  not clean; LIVE forward evidence is what the meta-gate weights.
- ETF benchmark = VTV (value) + QUAL (quality), 50/50 monthly, from SFP —
  chosen because they ARE the passive expression of this study's two pillars
  (the honest "why not just buy the ETFs?" test).
- Survivorship-safe by construction (PIT universe includes since-delisted
  names; delisting-return policy applies to a name that leaves mid-hold).

## Kill / relabel criteria
Fails t≥2 vs EITHER benchmark ⇒ "Watchlist (unscored factor screen)". A
strategy that beats the naive universe but NOT the value+quality ETF blend is
NOT scored — the ETFs already harvest the premia more cheaply.
