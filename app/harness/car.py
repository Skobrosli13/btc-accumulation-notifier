"""Cross-sectional CAR evaluator for equity event studies (§5.2).

For each event: match up to K control names on (date, cap tier, sector,
days-since-earnings bucket), then

    CAR_h = Σ over h sessions of (r_event,t − mean_t r_controls,t)

with entry at the NEXT session's open (an after-close alert can't trade the
event bar), daily returns thereafter close-over-close. SHORT events flip sign
(§5.7 symmetry — the evaluator scores the direction the emitter claimed).
Per-horizon CARs are winsorized at 1/99 across the study's events; significance
is the month-clustered t from :mod:`~app.harness.stats`.

Control discipline:
  * matched on tier + sector + ds-earnings bucket (0-10 / 11-30 / 31+);
    when a cohort has < MIN_COHORT candidates the SECTOR constraint drops first,
    then the earnings bucket (tier never drops);
  * a candidate carrying a same-study event within ±CONTROL_EXCLUSION_DAYS of
    the event date is contaminated and excluded;
  * the event's own ticker is never a control.

Everything is pure: the caller (scripts/study.py) supplies events, bars and
per-event candidate lists from the lake/PIT snapshots. The collision report
(§5.2) flags events within ±5 sessions of ANOTHER study's event on the same
permaticker; significance that survives only WITH collisions ⇒ KILLED-duplicate.
"""
from __future__ import annotations

import bisect

from . import stats

HORIZONS = (5, 10, 21, 63)          # sessions
K_CONTROLS = 25
MIN_COHORT = 10                      # relax matching below this many candidates
CONTROL_EXCLUSION_DAYS = 30          # same-study event on a control = contaminated
COLLISION_SESSIONS = 5               # ±sessions for the cross-study collision flag
_DAY_MS = 86_400_000
# ±5 sessions ≈ ±7 calendar days (weekends); collisions are flagged on calendar
# time because the other study's events carry only timestamps, not bar indices.
COLLISION_MS = 7 * _DAY_MS

WINSOR_LO, WINSOR_HI = 0.01, 0.99


def earnings_bucket(days_since_earnings) -> str:
    """0-10 / 11-30 / 31+ sessions-since-earnings bucket ('' when unknown —
    unknown matches only unknown, never a real bucket)."""
    if days_since_earnings is None:
        return ""
    d = int(days_since_earnings)
    if d <= 10:
        return "0-10"
    if d <= 30:
        return "11-30"
    return "31+"


def match_controls(event: dict, candidates: list[dict], *, k: int = K_CONTROLS,
                   min_cohort: int = MIN_COHORT,
                   study_event_ts_by_ticker: dict[str, list[int]] | None = None,
                   exclusion_days: int = CONTROL_EXCLUSION_DAYS) -> list[dict]:
    """Up to ``k`` matched controls for one event (pure).

    ``candidates``: universe rows as of the event date — {ticker, tier, sector,
    days_since_earnings}. ``study_event_ts_by_ticker``: every event timestamp of
    the SAME study per ticker (contamination filter). Matching relaxes
    sector -> earnings-bucket when the cohort is thin; tier never relaxes.
    Deterministic: candidates keep their input order (caller controls any
    randomization; determinism keeps runs reproducible).
    """
    ev_tk = event.get("ticker")
    ev_ts = int(event["event_ts"])
    excl_ms = exclusion_days * _DAY_MS
    by_ticker = study_event_ts_by_ticker or {}

    def clean(c: dict) -> bool:
        tk = c.get("ticker")
        if not tk or tk == ev_tk:
            return False
        return not any(abs(ev_ts - int(ts)) <= excl_ms for ts in by_ticker.get(tk, ()))

    pool = [c for c in candidates if clean(c) and c.get("tier") == event.get("tier")]
    ev_bucket = earnings_bucket(event.get("days_since_earnings"))

    # Relaxation ladder: (sector + bucket) -> (bucket only) -> (tier only).
    tiers = [
        [c for c in pool if c.get("sector") == event.get("sector")
         and earnings_bucket(c.get("days_since_earnings")) == ev_bucket],
        [c for c in pool if earnings_bucket(c.get("days_since_earnings")) == ev_bucket],
        pool,
    ]
    for cohort in tiers:
        if len(cohort) >= min_cohort:
            return cohort[:k]
    return tiers[-1][:k]      # thin universe: best available (may be < min_cohort)


# Entry-adjacency guard: the entry bar must fall within this many ms of the
# event, else the name's data simply doesn't cover the event (late-starting or
# truncated series) and pricing it on the wrong calendar window would fabricate
# CAR — the exact phantom-alpha bug the M2 adversarial verification caught.
MAX_ENTRY_LAG_MS = 7 * _DAY_MS


def daily_returns(bars: list[dict], event_ts: int, h: int
                  ) -> list[tuple[int, float]] | None:
    """The h per-session (ts, return) pairs from the NEXT session's open after
    ``event_ts``.

    Session 1 = close_1/open_1 − 1 (entry at that open); sessions 2..h are
    close-over-close. None when fewer than h post-event sessions exist, when the
    entry bar lags the event by more than MAX_ENTRY_LAG_MS (a series that starts
    after the event must be skipped, never priced on the wrong window), or when
    any window bar carries a missing/zero price (a None/0 close would otherwise
    crash or fabricate a −100% session)."""
    if not bars:
        return None
    ts_list = [b["ts"] for b in bars]
    start = bisect.bisect_right(ts_list, event_ts)     # first bar AFTER the event
    if start + h > len(bars):
        return None
    if bars[start]["ts"] - event_ts > MAX_ENTRY_LAG_MS:
        return None
    window = bars[start: start + h]
    if any(not b.get("close") for b in window) or not window[0].get("open"):
        return None
    out = [(window[0]["ts"], window[0]["close"] / window[0]["open"] - 1.0)]
    for i in range(1, h):
        out.append((window[i]["ts"],
                    window[i]["close"] / window[i - 1]["close"] - 1.0))
    return out


def event_car(event: dict, bars_by_ticker: dict[str, list[dict]],
              controls: list[dict], h: int) -> tuple[float, dict] | None:
    """Signed CAR_h for one event vs its matched controls (pure).

    CAR = Σ over the event's h sessions of (r_evt,t − mean over controls of
    r_ctl,t), where control returns are matched to the event's sessions BY
    TIMESTAMP — a control with a gapped/halted series contributes exactly the
    sessions it traded and drops out of the others (per-session renormalize),
    never a positionally-shifted window. Returns (signed_car, diag) where diag
    carries {"mean_controls": avg controls per session, "zero_control_sessions":
    n} so an unhedged CAR is visible upstream; None when the event itself is
    unpriceable. SHORT flips sign.
    """
    ev = daily_returns(bars_by_ticker.get(event.get("ticker"), []),
                       int(event["event_ts"]), h)
    if ev is None:
        return None
    # Control returns keyed by session timestamp (their own calendars).
    ctl_by_ts: list[dict[int, float]] = []
    for c in controls:
        r = daily_returns(bars_by_ticker.get(c.get("ticker"), []),
                          int(event["event_ts"]), h)
        if r is not None:
            ctl_by_ts.append(dict(r))
    car = 0.0
    n_ctl_total = 0
    zero_sessions = 0
    for ts, ev_ret in ev:
        session_ctl = [d[ts] for d in ctl_by_ts if ts in d]
        n_ctl_total += len(session_ctl)
        if session_ctl:
            car += ev_ret - sum(session_ctl) / len(session_ctl)
        else:
            zero_sessions += 1
            car += ev_ret          # unhedged session (flagged via diag)
    diag = {"mean_controls": n_ctl_total / h, "zero_control_sessions": zero_sessions}
    return (-car if event.get("direction") == "SHORT" else car), diag


def evaluate(events: list[dict], bars_by_ticker: dict[str, list[dict]],
             candidates_by_event: list[list[dict]], *,
             horizons: tuple[int, ...] = HORIZONS, k: int = K_CONTROLS,
             min_cohort: int = MIN_COHORT) -> dict:
    """Run the study: per-horizon winsorized CARs + month-clustered stats (pure).

    ``candidates_by_event[i]`` is the control-candidate universe for events[i]
    (PIT rows as of that event's date). Returns
    {"horizons": {h: {n_events, n_months, mean_car, t_clustered, win_rate}},
     "cars": {h: [(event_index, car), ...]},   # for placebo/collision re-runs
     "coverage": {n_events, n_priced_by_horizon}}.
    """
    study_ts: dict[str, list[int]] = {}
    for ev in events:
        study_ts.setdefault(ev.get("ticker"), []).append(int(ev["event_ts"]))

    controls = [match_controls(ev, cands, k=k, min_cohort=min_cohort,
                               study_event_ts_by_ticker=study_ts)
                for ev, cands in zip(events, candidates_by_event)]

    out: dict = {"horizons": {}, "cars": {}, "coverage": {"n_events": len(events)}}
    priced_counts: dict[int, int] = {}
    ctl_cov: dict[int, dict] = {}
    for h in horizons:
        rows = []                                   # (event_index, car)
        mean_ctls: list[float] = []
        n_unhedged = 0
        for i, ev in enumerate(events):
            res = event_car(ev, bars_by_ticker, controls[i], h)
            if res is None:
                continue
            car, diag = res
            rows.append((i, car))
            mean_ctls.append(diag["mean_controls"])
            if diag["zero_control_sessions"]:
                n_unhedged += 1
        priced_counts[h] = len(rows)
        # Control-coverage honesty: an unhedged CAR must be visible, not silent.
        ctl_cov[h] = {"mean_controls": (sum(mean_ctls) / len(mean_ctls)
                                        if mean_ctls else None),
                      "n_events_with_unhedged_sessions": n_unhedged}
        cars = stats.winsorize([c for _i, c in rows], WINSOR_LO, WINSOR_HI)
        ts_ms = [int(events[i]["event_ts"]) for i, _c in rows]
        ct = stats.clustered_t(cars, ts_ms)
        out["horizons"][h] = {
            "n_events": len(cars),
            "n_months": ct["n_months"],
            "mean_car": ct["mean"],
            "t_clustered": ct["t"],
            "win_rate": (sum(1 for c in cars if c > 0) / len(cars)) if cars else None,
        }
        out["cars"][h] = [(i, w) for (i, _raw), w in zip(rows, cars)]
    out["coverage"]["n_priced_by_horizon"] = priced_counts
    out["coverage"]["controls_by_horizon"] = ctl_cov
    return out


def collision_report(events: list[dict],
                     other_events: dict[str, list[dict]], *,
                     window_ms: int = COLLISION_MS) -> dict:
    """Cross-study collision flags (§5.2): events within ±~5 sessions of ANOTHER
    study's event on the same permaticker. Returns {"pct": float,
    "collided_indices": [i,...], "by_study": {study: n}} — the evaluator is then
    re-run excluding ``collided_indices``; significance that survives only WITH
    them ⇒ KILLED-duplicate."""
    other_ts: dict[str, list[tuple[int, str]]] = {}
    for study, evs in other_events.items():
        for ev in evs:
            key = str(ev.get("permaticker") or ev.get("ticker"))
            other_ts.setdefault(key, []).append((int(ev["event_ts"]), study))

    collided: list[int] = []
    by_study: dict[str, int] = {}
    for i, ev in enumerate(events):
        key = str(ev.get("permaticker") or ev.get("ticker"))
        ts = int(ev["event_ts"])
        hits = {study for (ots, study) in other_ts.get(key, ())
                if abs(ots - ts) <= window_ms}
        if hits:
            collided.append(i)
            for s in hits:
                by_study[s] = by_study.get(s, 0) + 1
    return {"pct": (len(collided) / len(events)) if events else 0.0,
            "collided_indices": collided, "by_study": by_study}
