"""Shared core primitives (asset-agnostic).

Home for pieces used by both the BTC and equities stacks — starting with the
pure indicator math extracted from ``shortterm.py``. Per EDGE_LAB_PLAN §2 this
package grows to hold config/store/notify/watchdog during the Phase-0
restructure; for now it holds only :mod:`app.core.indicators`.
"""
