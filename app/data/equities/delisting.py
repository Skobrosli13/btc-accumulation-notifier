"""Delisting-return policy (Shumway) — §4.3.

A backtest/forward position must consume returns *through* the delisting date,
and the terminal bar must not silently vanish (dropping delisted names is the
classic survivorship inflation). Policy, keyed off the Sharadar ACTIONS taxonomy
observed on/near the delisting date:

  * **Cash / stock M&A** (``acquisitionby`` / ``acquisitionof`` alongside
    ``delisted``): the position closes at the final price, which the last SEP
    ``closeadj`` bar already reflects — use that terminal return; if it's
    missing, 0.0 (a clean exit, no shock).
  * **Performance / bankruptcy / plain regulatory delisting** (``delisted`` with
    no acquisition, ``bankruptcyliquidation``): use the real terminal return if
    present, else assume **−30%** on the final bar (Shumway 1997's convention for
    a missing performance-delisting return — omitting it overstates returns).

Pure: given the action codes + an optional real terminal return, returns the
number to apply. The ACTIONS join + SEP terminal-return lookup are orchestration.
"""
from __future__ import annotations

# ACTIONS action codes (verified live against the Sharadar bundle; the M1
# acceptance sweep of all 352k rows found the full delisting-relevant set:
# delisted=9401, acquisitionby/of=2421 each, bankruptcyliquidation=1220,
# regulatorydelisting=474, voluntarydelisting=158, mergerto=62 — every mergerto
# co-occurs with 'delisted' and NEVER with acquisitionby/of, so it is its own
# merger-exit class; 'mergerfrom' marks the SURVIVING entity and is not a
# delisting signal).
MERGER_ACTIONS = frozenset({"acquisitionby", "acquisitionof", "mergerto"})
PERFORMANCE_ACTIONS = frozenset({"bankruptcyliquidation"})
DELISTED_ACTION = "delisted"

# Shumway (1997): assume this final-bar return when a performance-related
# delisting return is missing, rather than dropping the terminal bar.
SHUMWAY_MISSING_RETURN = -0.30


def is_merger(actions) -> bool:
    return bool(set(actions or ()) & MERGER_ACTIONS)


def is_performance_delisting(actions) -> bool:
    """A delisting that is NOT an acquisition — regulatory/voluntary/bankruptcy —
    i.e. the kind whose missing terminal return gets the Shumway haircut."""
    acts = set(actions or ())
    if DELISTED_ACTION not in acts and not (acts & PERFORMANCE_ACTIONS):
        return False
    return not (acts & MERGER_ACTIONS)


def terminal_return(actions, *, final_return: float | None = None) -> float:
    """The return to apply on a delisted position's final bar.

    ``actions``: the ACTIONS action codes on/near the delisting date.
    ``final_return``: the actual last-bar return from SEP ``closeadj`` if the
    vendor supplies one through the delisting date, else None.
    """
    if is_merger(actions):
        # Cash/stock M&A: closes at the deal/final price (already in SEP). If the
        # terminal bar is missing, exit flat rather than inventing a loss.
        return final_return if final_return is not None else 0.0
    # Performance / bankruptcy / plain regulatory:
    if final_return is not None:
        return final_return
    return SHUMWAY_MISSING_RETURN
