"""Stock swing-tracker data adapters.

Same fail-soft contract as the BTC ``sources/`` package: every optional adapter
returns ``None`` / ``[]`` / ``{}`` on any failure and never raises, so a dead
layer renormalizes away instead of darkening a whole run. Prices are the one
near-mandatory feed (a name with no bars is simply skipped, not fatal).

Free/keyless by default: ``universe`` (SEC ticker map) and ``prices`` (Stooq CSV),
``insider`` (SEC EDGAR). ``earnings``/``estimates`` need a free Finnhub key;
``prices`` optionally upgrades to Alpaca/Tiingo when keyed.
"""
