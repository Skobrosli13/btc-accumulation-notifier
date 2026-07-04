"""Cron-grid next-run helpers — hand-computed boundary fixtures (§10)."""
from __future__ import annotations

from datetime import datetime, timezone

from app import schedule as sch


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def test_next_minute_grid():
    assert sch.next_minute_grid(_dt("2026-07-04T10:03:20"), 10) == _dt("2026-07-04T10:10:00")
    # exactly ON a boundary -> strictly next slot
    assert sch.next_minute_grid(_dt("2026-07-04T10:10:00"), 10) == _dt("2026-07-04T10:20:00")
    # :55 rolls into the next hour
    assert sch.next_minute_grid(_dt("2026-07-04T10:55:00"), 10) == _dt("2026-07-04T11:00:00")


def test_next_hour_grid_6h():
    assert sch.next_hour_grid(_dt("2026-07-04T10:03:00"), 6) == _dt("2026-07-04T12:00:00")
    assert sch.next_hour_grid(_dt("2026-07-04T12:00:00"), 6) == _dt("2026-07-04T18:00:00")
    # 23:xx rolls to next-day 00:00
    assert sch.next_hour_grid(_dt("2026-07-04T23:30:00"), 6) == _dt("2026-07-05T00:00:00")


def test_next_weekday_at_rolls_weekend():
    # 2026-07-03 is a Friday. After Friday 22:30 -> Monday 22:30 (skip Sat/Sun).
    assert sch.next_weekday_at(_dt("2026-07-03T23:00:00"), 22, 30) == \
        _dt("2026-07-06T22:30:00")
    # Friday before the run -> same day
    assert sch.next_weekday_at(_dt("2026-07-03T10:00:00"), 22, 30) == \
        _dt("2026-07-03T22:30:00")
    # Saturday -> Monday
    assert sch.next_weekday_at(_dt("2026-07-04T09:00:00"), 22, 30) == \
        _dt("2026-07-06T22:30:00")


def test_next_weekly_sunday():
    # 2026-07-04 is a Saturday; next Sunday 08:00 is 07-05.
    assert sch.next_weekly_at(_dt("2026-07-04T09:00:00"), 6, 8) == \
        _dt("2026-07-05T08:00:00")
    # ON Sunday after 08:00 -> next week
    assert sch.next_weekly_at(_dt("2026-07-05T09:00:00"), 6, 8) == \
        _dt("2026-07-12T08:00:00")


def test_lab_sync_state():
    now = _dt("2026-07-04T12:00:00")
    fresh = sch.lab_sync_state("2026-07-04T04:00:00+00:00", now)
    assert fresh["overdue"] is False and fresh["age_hours"] == 8.0
    assert fresh["next_expected"] == "2026-07-05T04:00:00+00:00"
    stale = sch.lab_sync_state("2026-07-02T04:00:00+00:00", now)
    assert stale["overdue"] is True
    assert sch.lab_sync_state(None, now)["overdue"] is True
    assert sch.lab_sync_state("garbage", now)["overdue"] is True
