"""Long-term equity factor engine (§2 lt/, §5.5 lt_factor).

The evidence-based QVM "long buys" screener (quality gate purges value traps;
value + quality + momentum ranked SEPARATELY and intersected — the documented
free equity edge), backed by Sharadar SF1 (ART/TTM, PIT via datekey). Pure and
fixture-tested; the harness portfolio evaluator scores it on a monthly-rebalance
backtest vs the equal-weight PIT universe AND a 50/50 value+quality ETF proxy,
and the gate either scores it or labels it "Watchlist (unscored factor screen)".
"""
