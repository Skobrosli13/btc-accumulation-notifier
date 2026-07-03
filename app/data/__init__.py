"""Data adapters + lake for the EDGE-LAB target architecture (§2).

New data modules land here under their asset:
  - ``data/equities/`` — Sharadar bundle, PIT security master + universe, EDGAR
    (SRW-SUE / 8-K / full-text), QA.
  - ``data/crypto/``   — OKX/Coinalyze/on-chain/FRED adapters + store-forward
    archival (to be populated as the crypto side migrates).

NOTE (§0.5c, deferred): the EXISTING crypto + equity source adapters still live
in ``app/sources/`` and ``app/sources/stocks/``. Relocating them into this tree
is a pure no-behaviour-change sweep (it re-homes the shared ``_http`` helper into
``core/`` and rewrites ~40 relative imports) — intentionally deferred so it can
be done in isolation rather than tangled with M1 capability work. New M1 modules
are written here directly.
"""
