"""Portfolio POLICIES (§2) — discipline overlays, explicitly NOT alpha.

A policy claims *discipline* (better drawdowns / systematic timing of capital
you were deploying anyway), gated by "no harm vs the naive baseline + drawdown
improvement" (§5.5 POLICY). Pure signal functions live here; the harness's
``portfolio_bt`` scores them; the UI labels them from verdicts.
"""
