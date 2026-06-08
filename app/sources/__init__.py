"""Data source adapters.

Every source is optional except free price data. Each adapter returns a dict of
readings (or None for unavailable indicators) and must never raise on a missing
key or a transient network/parse error — it returns None/empty so the scorer can
renormalize. Only the price source is allowed to fail hard (it is mandatory).
"""
