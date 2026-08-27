"""Time interval and availability rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


@dataclass(frozen=True, order=True)
class BusyInterval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Busy intervals must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("Busy interval end must be after start")

    def as_utc(self) -> BusyInterval:
        return BusyInterval(self.start.astimezone(UTC), self.end.astimezone(UTC))


def merge_intervals(intervals: list[BusyInterval]) -> list[BusyInterval]:
    """Merge overlapping or touching intervals into sorted UTC intervals."""

    ordered = sorted(
        (interval.as_utc() for interval in intervals), key=lambda item: item.start
    )
    if not ordered:
        return []
    merged = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = BusyInterval(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return merged


def overlaps(left: BusyInterval, right: BusyInterval) -> bool:
    return left.start < right.end and right.start < left.end


class AvailabilityEngine:
    def __init__(
        self,
        *,
        owner_timezone: str,
        workday_start: str,
        workday_end: str,
        weekdays: set[int],
        increment_minutes: int,
        minimum_notice_hours: int,
        booking_window_days: int,
        buffer_before_minutes: int,
        buffer_after_minutes: int,
    ) -> None:
        self.timezone = ZoneInfo(owner_timezone)
        self.workday_start = _parse_clock(workday_start)
        self.workday_end = _parse_clock(workday_end)
        if self.workday_end <= self.workday_start:
            raise ValueError("availability_end must be after availability_start")
        self.weekdays = weekdays
        self.increment = timedelta(minutes=increment_minutes)
        self.minimum_notice = timedelta(hours=minimum_notice_hours)
        self.booking_window = timedelta(days=booking_window_days)
        self.buffer_before = timedelta(minutes=buffer_before_minutes)
        self.buffer_after = timedelta(minutes=buffer_after_minutes)

    def available_slots(
        self,
        *,
        first_day: date,
        days: int,
        duration_minutes: int,
        busy: list[BusyInterval],
        now: datetime | None = None,
    ) -> list[BusyInterval]:
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        earliest = now_utc + self.minimum_notice
        latest = now_utc + self.booking_window
        duration = timedelta(minutes=duration_minutes)
        busy_with_buffers = [
            BusyInterval(
                interval.start.astimezone(UTC) - self.buffer_before,
                interval.end.astimezone(UTC) + self.buffer_after,
            )
            for interval in merge_intervals(busy)
        ]

        slots: list[BusyInterval] = []
        for offset in range(days):
            local_day = first_day + timedelta(days=offset)
            if local_day.weekday() not in self.weekdays:
                continue
            cursor = datetime.combine(local_day, self.workday_start, self.timezone)
            workday_end = datetime.combine(local_day, self.workday_end, self.timezone)
            while cursor + duration <= workday_end:
                candidate = BusyInterval(
                    cursor.astimezone(UTC),
                    (cursor + duration).astimezone(UTC),
                )
                if (
                    candidate.start >= earliest
                    and candidate.end <= latest
                    and not any(
                        overlaps(candidate, blocked) for blocked in busy_with_buffers
                    )
                ):
                    slots.append(candidate)
                cursor += self.increment
        return slots

    def is_valid_slot(
        self,
        *,
        start: datetime,
        duration_minutes: int,
        busy: list[BusyInterval],
        now: datetime | None = None,
    ) -> bool:
        start_utc = start.astimezone(UTC)
        local_day = start_utc.astimezone(self.timezone).date()
        slots = self.available_slots(
            first_day=local_day,
            days=1,
            duration_minutes=duration_minutes,
            busy=busy,
            now=now,
        )
        return any(slot.start == start_utc for slot in slots)


def _parse_clock(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid clock value: {value}") from exc
