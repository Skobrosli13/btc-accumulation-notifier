"""Event EMITTERS (§2) — thin, pure translators from raw data to `events` rows.

An emitter never scores anything: it applies a pre-registered event definition
(studies/<name>.md) to raw data and emits {study, ticker, event_ts, direction,
strength, meta} rows. Only the harness computes performance on them. Any change
to a definition here is Class B (§9.5): re-register as `<study>-v2`.
"""
