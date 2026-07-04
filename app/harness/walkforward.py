"""Walk-forward segmenting — IS / OOS / LIVE + embargo (§5.4).

Fixed, pre-registered boundaries:
  * **IS**   — event_ts <= 2021-12-31 (exploration is unrestricted here; §9.5)
  * **OOS**  — 2022-01-01 .. the study's registration instant
  * **LIVE** — after registration (the only genuinely uncontaminated segment;
    hypotheses were chosen in 2026 knowing history through 2025 — §9 honesty note)

An **embargo** of 21 sessions (~1 calendar month) around each boundary drops
events whose forward windows would straddle two segments — otherwise an IS-fit
event's outcome bleeds into OOS and flatters it. Events inside an embargo belong
to NO segment (excluded, not reassigned).
"""
from __future__ import annotations

from datetime import datetime, timezone

_DAY_MS = 86_400_000

# IS/OOS boundary: end of 2021 UTC (pre-registered; changing it is Class C).
IS_END_MS = int(datetime(2021, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
# 21 trading sessions ~= 30 calendar days.
EMBARGO_MS = 30 * _DAY_MS


def segment_of(event_ts: int, registered_at: int, *,
               is_end_ms: int = IS_END_MS, embargo_ms: int = EMBARGO_MS) -> str | None:
    """Segment for one event: 'IS' | 'OOS' | 'LIVE' | None (embargoed).

    The embargo brackets BOTH boundaries (IS/OOS and OOS/LIVE): an event within
    ``embargo_ms`` on either side of a boundary is dropped so overlapping
    forward windows can't leak outcomes across segments.
    """
    if abs(event_ts - is_end_ms) < embargo_ms:
        return None
    if abs(event_ts - registered_at) < embargo_ms:
        return None
    if event_ts <= is_end_ms:
        return "IS"
    if event_ts <= registered_at:
        return "OOS"
    return "LIVE"


def split_events(events: list[dict], registered_at: int, *,
                 ts_key: str = "event_ts",
                 is_end_ms: int = IS_END_MS,
                 embargo_ms: int = EMBARGO_MS) -> dict[str, list[dict]]:
    """Partition events into {'IS': [...], 'OOS': [...], 'LIVE': [...]};
    embargoed events land in the 'EMBARGOED' bucket (reported, never scored —
    a silently vanishing event count would read as coverage)."""
    out: dict[str, list[dict]] = {"IS": [], "OOS": [], "LIVE": [], "EMBARGOED": []}
    for ev in events:
        seg = segment_of(int(ev[ts_key]), registered_at,
                         is_end_ms=is_end_ms, embargo_ms=embargo_ms)
        out[seg if seg else "EMBARGOED"].append(ev)
    return out
