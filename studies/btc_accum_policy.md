# Study pre-registration: btc_accum_policy

**Asset:** BTC · **Evaluator:** portfolio · **Tier:** policy
**Primary horizon:** n/a (continuous overlay)
**Registered:** 2026-07-04 · **Author:** owner (drafted by the lab per plan §6)

## Hypothesis
The existing long-term accumulation composite (0–100 → NEUTRAL/WATCH/ACCUMULATE/
DEEP_VALUE tiers), used as a **DCA spend-tilt on NEW capital**, deploys a fixed
budget with better timing discipline than plain DCA — buying harder into
composite-cheap regimes. Claim is discipline, not alpha; the composite itself
stays exactly as shipped (this study never modifies scoring).

## Event definition (exact)
Weekly contribution budget B (every 7 calendar days). Spend multiplier from the
LT tier at the contribution close: NEUTRAL 0.75 / WATCH 1.0 / ACCUMULATE 1.5 /
DEEP_VALUE 2.0 (unknown → 1.0). Unspent budget banks as 0%-cash; multipliers >1
spend from the bank as available (total contributed capital identical to plain
DCA by construction). Implementation: `app/policies/btc.accum_scales` +
`app/harness/portfolio_bt.dca_simulate`; baseline = same contributions, all ×1.0.

Tier series:
- **Backtest leg:** the no-look-ahead historical tier reconstruction from
  `scripts/backtest_longterm` (expanding-window percentile composite on the
  multi-cycle panel; cycle multiplier + hysteresis excluded — documented there).
- **Forward leg:** the LIVE tiers recorded by `run_once` (runs table), exactly
  as alerted.

## Gate
POLICY (§5.5): overlay total return ≥ plain DCA **and** overlay max drawdown <
plain DCA (total-equity basis: cash + BTC), on the backtest leg **and** the
rolling forward leg. Fails either ⇒ unscored context. UI label:
"discipline overlay — not alpha (n≈5 independent episodes)".

## Known contaminations / caveats
- The composite's thresholds/weights were tuned knowing 2015–2025 history —
  the backtest leg is contaminated by construction (§9 honesty note); the
  forward leg is the real evidence and needs years to accrue.
- n≈5 independent accumulation episodes in the whole history; CIs are theater
  below that — the gate is deliberately a no-harm test, not a significance test.
- The backtest reconstruction excludes the cycle multiplier + tier hysteresis
  (live tiers near cutoffs can differ) and the 1-cycle free on-chain indicators.
- Passing this gate does NOT satisfy the meta-gate (§9: it reduces to "DCA with
  a dashboard").

## Kill criteria
Forward leg: overlay return < DCA OR overlay maxDD ≥ DCA over the rolling
post-registration record at any monthly review after ≥12 months.
