from datetime import datetime
from zoneinfo import ZoneInfo

from calendar_hub.connectors import parse_icloud_datetime, parse_icloud_event


def test_parse_icloud_event_array_dates():
    denver = ZoneInfo("America/Denver")
    event = {
        "localStartDate": [2026, 8, 31, 10, 30],
        "localEndDate": [2026, 8, 31, 11, 0],
        "title": "Private title that must not leave the connector",
    }

    interval = parse_icloud_event(event, denver)

    assert interval.start == datetime(2026, 8, 31, 10, 30, tzinfo=denver)
    assert interval.end == datetime(2026, 8, 31, 11, 0, tzinfo=denver)


def test_parse_icloud_datetime_accepts_millisecond_timestamp():
    denver = ZoneInfo("America/Denver")
    parsed = parse_icloud_datetime(1_787_758_200_000, denver)
    assert parsed.tzinfo is not None


def test_cancelled_icloud_event_is_ignored():
    assert (
        parse_icloud_event(
            {
                "status": "CANCELLED",
                "startDate": "2026-08-31T10:30:00-06:00",
                "endDate": "2026-08-31T11:00:00-06:00",
            },
            ZoneInfo("America/Denver"),
        )
        is None
    )
