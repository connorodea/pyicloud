from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_hub.domain import UTC, AvailabilityEngine, BusyInterval, merge_intervals


def engine(**overrides):
    values = {
        "owner_timezone": "America/Denver",
        "workday_start": "10:30",
        "workday_end": "12:00",
        "weekdays": {0, 1, 2, 3, 4},
        "increment_minutes": 15,
        "minimum_notice_hours": 0,
        "booking_window_days": 60,
        "buffer_before_minutes": 10,
        "buffer_after_minutes": 10,
    }
    values.update(overrides)
    return AvailabilityEngine(**values)


def test_merge_intervals_merges_touching_ranges():
    first = BusyInterval(
        datetime(2026, 8, 31, 16, 0, tzinfo=UTC),
        datetime(2026, 8, 31, 17, 0, tzinfo=UTC),
    )
    second = BusyInterval(first.end, first.end + timedelta(minutes=30))

    assert merge_intervals([second, first]) == [BusyInterval(first.start, second.end)]


def test_availability_respects_busy_buffers():
    denver = ZoneInfo("America/Denver")
    busy = [
        BusyInterval(
            datetime(2026, 8, 31, 11, 0, tzinfo=denver),
            datetime(2026, 8, 31, 11, 30, tzinfo=denver),
        )
    ]
    slots = engine().available_slots(
        first_day=date(2026, 8, 31),
        days=1,
        duration_minutes=20,
        busy=busy,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )

    local_starts = [slot.start.astimezone(denver).strftime("%H:%M") for slot in slots]
    assert local_starts == ["10:30"]


def test_availability_keeps_denver_wall_clock_across_dst():
    denver = ZoneInfo("America/Denver")
    slots = engine(workday_end="11:00").available_slots(
        first_day=date(2026, 10, 30),
        days=5,
        duration_minutes=30,
        busy=[],
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )

    assert [
        slot.start.astimezone(denver).strftime("%Y-%m-%d %H:%M %z") for slot in slots
    ] == [
        "2026-10-30 10:30 -0600",
        "2026-11-02 10:30 -0700",
        "2026-11-03 10:30 -0700",
    ]
