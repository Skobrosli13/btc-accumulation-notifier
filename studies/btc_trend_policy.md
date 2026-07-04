# Study pre-registration: btc_trend_policy

**Asset:** BTC · **Evaluator:** portfolio · **Tier:** policy
**Primary horizon:** n/a (continuous overlay; results reported on the full window)
**Registered:** 2026-07-04 · **Author:** owner (drafted by the lab per plan §6)

## Hypothesis
A long/flat 200-day-MA regime filter on the HELD BTC stack harvests the asset's
regime persistence: most of BTC's catastrophic drawdowns (2018 −84%, 2022 −77%)
occurred entirely below the 200-DMA. The claim is **discipline, not alpha** —
smaller drawdowns without giving up total return vs buy-and-hold. Who's on the
other side: nobody; the cost is whipsaw churn in sideways regimes.

## Event definition (exact)
Daily BTC-USD closes (Coinbase deep history, lake `btc_daily`, 2015-07+).
Exposure at close t (causal, uses closes[..t] only):
- LONG (1.0) when close > 200-day SMA × 1.02
- FLAT (0.0) when close < 200-day SMA × 0.98
- inside the ±2% band: hold the prior state (hysteresis)
- cold start (< 200 bars of history): FLAT.
Exposure earns the t→t+1 return; each |Δexposure| pays 10 bps.
Implementation: `app/policies/btc.trend_exposure` + `app/harness/portfolio_bt.equity_curve`.

## Gate
POLICY (§5.5): total return ≥ buy-and-hold **and** max drawdown < buy-and-hold,
on the backtest window (2015-07 → registration) **and** the rolling forward
window (post-registration, re-evaluated monthly). Fails either leg ⇒ demote to
unscored context (WATCHLIST). UI label: "discipline overlay — not alpha".

## Known contaminations / caveats
- The 200-DMA filter is the single most-published crypto overlay: this backtest
  is textbook-contaminated (hypothesis known since ~2014). Historical PASS is
  weak evidence; the FORWARD leg is the evidence that counts.
- One asset, ~3 regimes. Hysteresis band (2%) and cost (10bps) are pre-registered
  constants; changing either re-registers as -v2.
- Independent of btc_accum_policy (this manages the HELD stack; accumulation
  tilts NEW capital). Owner adopts at most one per function.

## Kill criteria
Forward window: overlay return < buy-and-hold OR overlay maxDD ≥ buy-and-hold
over the rolling post-registration record at any monthly review after ≥12
months of forward data.
