from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from calendar_hub.app import create_app
from calendar_hub.config import Settings
from calendar_hub.connectors import CreatedCalendarEvent
from calendar_hub.domain import UTC
from calendar_hub.services import CalendarHub, CalendarHubService, InMemoryBookingStore


class FakeGoogle:
    name = "google"

    def busy(self, start, end):
        return []

    def create_event(self, request, *, end, join_url):
        return CreatedCalendarEvent(
            event_id="event-1",
            start=request.start.astimezone(UTC),
            end=end.astimezone(UTC),
            html_link="https://calendar.google.com/event",
            join_url=join_url,
        )


class UnusedZoom:
    def create_meeting(self, request, *, duration_minutes):
        raise AssertionError("Phone booking must not create a video meeting")

    def delete_meeting(self, meeting_id):
        raise AssertionError("No meeting should exist")


def test_public_booking_flow_and_security_headers():
    settings = Settings(
        environment="test",
        pyicloud_enabled=False,
        minimum_notice_hours=0,
        booking_window_days=60,
        availability_start="10:30",
        availability_end="12:00",
        availability_weekdays="0,1,2,3,4",
        allowed_hosts="testserver",
    )
    google = FakeGoogle()
    service = CalendarHubService(
        settings,
        hub=CalendarHub([google]),
        google=google,
        store=InMemoryBookingStore(),
        zoom=UnusedZoom(),
    )
    client = TestClient(create_app(settings, service))

    page = client.get("/")
    config = client.get("/api/v1/config")

    assert page.status_code == 200
    assert "Choose how you’d like to connect" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert config.json()["event_types"][1]["id"] == "phone"

    denver = ZoneInfo("America/Denver")
    first_day = datetime.now(denver).date() + timedelta(days=1)
    availability = client.get(
        "/api/v1/availability",
        params={
            "event_type": "phone",
            "from_date": first_day.isoformat(),
            "days": 7,
            "timezone": "America/Denver",
        },
    )
    assert availability.status_code == 200
    slot = availability.json()["slots"][0]

    booking = client.post(
        "/api/v1/bookings",
        json={
            "event_type": "phone",
            "start": slot["start"],
            "timezone": "America/Denver",
            "name": "Test Guest",
            "email": "guest@example.com",
            "phone": "+1 303 555 0100",
            "notes": "A short agenda",
            "idempotency_key": "5ff7e3cc-f9bb-4a1b-8462-3a8f454aa6d2",
        },
    )

    assert booking.status_code == 201
    assert booking.json()["booking_id"] == "event-1"
