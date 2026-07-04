"""Next-run computation from the deployed cron grids (pure) — §10 freshness.

"Next" comes from the CRON GRID, never from the data: a failed run must not
push "next" into the future — the grid says when the next ATTEMPT is; the
staleness flags say whether the last one worked.

Deployed cadences (box crontab, UTC):
  * collect_once   */10 min
  * run_once       0 */6 h
  * stock_collect  22:30 Mon–Fri
  * stock_lt       Sun 08:00
The lab nightly runs on the DEV MACHINE (laptop may sleep), so its "next" is
self-calibrating: last sync + 24h expected, overdue past +26h — never a
wall-clock promise the box can't keep.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

LAB_SYNC_EXPECT_H = 24.0
LAB_SYNC_OVERDUE_H = 26.0


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_minute_grid(now: datetime, every_min: int) -> datetime:
    """Next `*/every_min` cron boundary strictly after ``now``."""
    now = _utc(now)
    base = now.replace(second=0, microsecond=0)
    slot = (base.minute // every_min + 1) * every_min
    return base + timedelta(minutes=slot - base.minute)


def next_hour_grid(now: datetime, every_h: int) -> datetime:
    """Next `0 */every_h` cron boundary strictly after ``now``."""
    now = _utc(now)
    base = now.replace(minute=0, second=0, microsecond=0)
    slot = (base.hour // every_h + 1) * every_h
    return base + timedelta(hours=slot - base.hour)


def next_weekday_at(now: datetime, hour: int, minute: int) -> datetime:
    """Next Mon–Fri occurrence of HH:MM UTC strictly after ``now``."""
    now = _utc(now)
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while cand <= now or cand.weekday() > 4:      # rolls Fri evening -> Monday
        cand = (cand + timedelta(days=1)).replace(hour=hour, minute=minute)
    return cand


def next_weekly_at(now: datetime, weekday: int, hour: int, minute: int = 0) -> datetime:
    """Next occurrence of ``weekday`` (0=Mon..6=Sun) at HH:MM UTC after ``now``."""
    now = _utc(now)
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days = (weekday - cand.weekday()) % 7
    cand += timedelta(days=days)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


def lab_sync_state(last_sync_iso: str | None, now: datetime | None = None) -> dict:
    """Self-calibrating freshness for the laptop-run lab sync."""
    now = _utc(now or datetime.now(timezone.utc))
    if not last_sync_iso:
        return {"last_sync": None, "next_expected": None,
                "age_hours": None, "overdue": True}
    try:
        last = _utc(datetime.fromisoformat(last_sync_iso))
    except ValueError:
        return {"last_sync": last_sync_iso, "next_expected": None,
                "age_hours": None, "overdue": True}
    age_h = (now - last).total_seconds() / 3600.0
    return {"last_sync": last.isoformat(),
            "next_expected": (last + timedelta(hours=LAB_SYNC_EXPECT_H)).isoformat(),
            "age_hours": round(age_h, 2),
            "overdue": age_h > LAB_SYNC_OVERDUE_H}


def btc_schedule(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"collect_next": next_minute_grid(now, 10).isoformat(),
            "run_next": next_hour_grid(now, 6).isoformat()}


def stock_schedule(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"swing_next": next_weekday_at(now, 22, 30).isoformat(),
            "longterm_next": next_weekly_at(now, 6, 8).isoformat()}
