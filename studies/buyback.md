# Study pre-registration: buyback

**Asset:** EQ · **Evaluator:** portfolio · **Tier:** policy
**Primary horizon:** 0 (continuous monthly rebalance) · **Registered:** 2026-07-06 · **Author:** owner

## Hypothesis
The net-share-issuance / buyback anomaly (Pontiff–Woodgate 2008; Daniel–Titman
2006; Ikenberry–Lakonishok–Vermaelen): firms that genuinely return cash by
repurchasing their own stock outperform net issuers over 6–12 months. Unlike a
valuation ratio, this is a hard corporate action — management using real cash to
shrink the share base — and it has historically survived out-of-sample better
than value because it is harder to arbitrage and less crowded. The seller is the
diluting/issuing firm and the momentum-chasing marginal buyer; the disciplined
repurchaser is on the other side. Tested as a monthly-rebalanced long-only book
of the strongest net repurchasers.

## Event definition (exact)
No discrete events — a continuous monthly-rebalanced portfolio (evaluator
`portfolio`, runner `scripts/study._run_buyback`). Each month-end `as_of`:
1. PIT universe = `eq_universe.build_from_lake(as_of)` restricted to
   small/mid/large tiers (micro excluded — cost-prohibitive).
2. PIT fundamentals = latest SF1 **ART** row with `datekey <= as_of` per ticker.
3. **Buyback yield = −ncfcommon / marketcap** (net $ returned via share
   repurchase over market cap; `ncfcommon` is negative when the firm is a net
   repurchaser). Restrict to `marketcap > 0`, `ncfcommon` present, `bby > 0`
   (genuine net repurchasers only).
4. LONG the top **30** by buyback yield, equal-weight; hold one month.
5. Return leg = mean forward one-month total return (`closeadj`) minus turnover ×
   20bps; benchmarks = equal-weight universe forward return AND the **PKW**
   (Invesco BuyBack Achievers) ETF total return.

Signal note: this is the cash-flow *net-repurchase-yield* formulation (split-
adjustment-immune), not the raw split-adjusted share-count log-change. PKW selects
on ≥5% trailing share-count reduction — a close but not identical construct, so it
is a fair-to-tough benchmark, not a tautological one. Any change to the signal,
top-N, tiers, cost, or ETF re-registers as `buyback-v2` (old rows freeze).

## Gate
POLICY/factor gate = `gates.lt_factor_verdict` (§5.5): OOS active-return
clustered t ≥ **2.0** over ≥ **36 months** vs BOTH the equal-weight PIT universe
AND the 50/50-analogue PKW ETF benchmark. Fails either leg ⇒ **WATCHLIST**
(unscored factor screen — the honest "just buy PKW" outcome). No third state.

## Known contaminations / caveats
- SEP/SF1 start 2016 ⇒ IS 2016–2021, OOS 2022→registration (~1.2 regimes, no
  real bear); OOS is the binding evidence and is regime-thin.
- Financials/REITs have share dynamics driven by capital structure, not
  conviction repurchase; not sector-neutralized in v1 (a v2 refinement if the
  headline result is promising-but-contaminated).
- ncfcommon nets issuance and repurchase; a firm doing a big secondary and a
  buyback in the same TTM window can be misclassified — the `bby > 0` floor keeps
  only net repurchasers, but the magnitude can be noisy.
- Explicitly benchmarked vs PKW per the lt_factor lesson: a factor that only
  matches a cheap ETF is WATCHLIST, not a pick.

## Kill criteria
This is a POLICY/factor study — there is no KILLED state, only PROMOTED (beats
BOTH benchmarks OOS at t ≥ 2) or WATCHLIST (does not). A WATCHLIST verdict is a
published honest null, kept for the record, exactly like lt_factor.
