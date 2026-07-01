"""Maturity-ladder labels — the Python mirror of the dashboard's ``lib/maturity.ts``.

One honesty ladder shared across BTC and stocks; keep these strings in sync with
the frontend so an emailed alert and its dashboard card read the same rung.

  EDGE     — best-evidenced free signal (published factor premia / documented anomaly)
  PRIOR    — a backtested / base-rate estimate, trusted only until live-confirmed
  FORWARD  — tracked out-of-sample from day one; no proven edge YET
  CONTEXT  — backtests ~= a coin flip; timing / confluence context, not a signal
"""
from __future__ import annotations

EDGE = "Proven edge"
PRIOR = "Calibrated prior"
FORWARD = "Live forward-test"
CONTEXT = "Context only"
