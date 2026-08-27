from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_hub.config import Settings
from calendar_hub.connectors import CreatedCalendarEvent, VideoMeeting
from calendar_hub.domain import UTC, BusyInterval
from calendar_hub.models import BookingRequest, EventType
from calendar_hub.services import (
    CalendarHub,
    CalendarHubService,
    InMemoryBookingStore,
    SlotUnavailableError,
)


class FakeBusyConnector:
    name = "fake"

    def __init__(self, intervals=None):
        self.intervals = intervals or []

    def busy(self, start, end):
        return [
            item for item in self.intervals if item.start < end and start < item.end
        ]


class FakeGoogle(FakeBusyConnector):
    def __init__(self):
        super().__init__()
        self.created = []

    def create_event(self, request, *, end, join_url):
        self.created.append(request)
        return CreatedCalendarEvent(
            event_id="google-event-1",
            start=request.start.astimezone(UTC),
            end=end.astimezone(UTC),
            html_link="https://calendar.google.com/event",
            join_url=join_url,
        )


class FakeZoom:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create_meeting(self, request, *, duration_minutes):
        self.created.append((request, duration_minutes))
        return VideoMeeting("zoom-1", "https://zoom.us/j/1")

    def delete_meeting(self, meeting_id):
        self.deleted.append(meeting_id)


def settings():
    return Settings(
        environment="test",
        pyicloud_enabled=False,
        minimum_notice_hours=0,
        availability_start="10:30",
        availability_end="12:00",
        availability_weekdays="0,1,2,3,4",
        video_duration_minutes=30,
        phone_duration_minutes=20,
    )


def booking(event_type=EventType.VIDEO):
    denver = ZoneInfo("America/Denver")
    return BookingRequest(
        event_type=event_type,
        start=datetime(2026, 8, 31, 10, 30, tzinfo=denver),
        timezone="America/Denver",
        name="Test Guest",
        email="guest@example.com",
        phone="+1 303 555 0100" if event_type == EventType.PHONE else "",
        idempotency_key="c0b793ca-71aa-4773-9844-acde227b1571",
    )


def test_availability_and_idempotent_video_booking():
    google = FakeGoogle()
    zoom = FakeZoom()
    service = CalendarHubService(
        settings(),
        hub=CalendarHub([google]),
        google=google,
        store=InMemoryBookingStore(),
        zoom=zoom,
    )
    now = datetime(2026, 8, 30, tzinfo=UTC)

    available = service.availability(
        event_type=EventType.VIDEO,
        first_day=date(2026, 8, 31),
        days=1,
        visitor_timezone="America/Denver",
        now=now,
    )
    result = service.book(booking(), now=now)
    repeated = service.book(booking(), now=now)

    assert len(available.slots) == 5
    assert result == repeated
    assert result.join_url == "https://zoom.us/j/1"
    assert len(google.created) == 1
    assert len(zoom.created) == 1


def test_busy_slot_cannot_be_booked():
    request = booking(EventType.PHONE)
    blocked = BusyInterval(request.start, request.start + timedelta(minutes=20))
    google = FakeGoogle()
    service = CalendarHubService(
        settings(),
        hub=CalendarHub([FakeBusyConnector([blocked])]),
        google=google,
        store=InMemoryBookingStore(),
        zoom=FakeZoom(),
    )

    with pytest.raises(SlotUnavailableError):
        service.book(request, now=datetime(2026, 8, 30, tzinfo=UTC))

    assert google.created == []
