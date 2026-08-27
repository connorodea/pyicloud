"""Calendar and conferencing connectors.

Only normalized busy intervals leave this module. Event titles, descriptions,
attendees, and private feed URLs are never exposed by the public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from threading import RLock
from typing import Any, Protocol
from urllib.parse import quote, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from dateutil.parser import isoparse

from calendar_hub.config import Settings
from calendar_hub.domain import UTC, BusyInterval
from calendar_hub.models import BookingRequest, EventType

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]


class ConnectorError(RuntimeError):
    """A calendar source could not be queried safely."""


class ConnectorAuthenticationError(ConnectorError):
    """A connector needs operator authentication."""


class BusyCalendarConnector(Protocol):
    name: str

    def busy(self, start: datetime, end: datetime) -> list[BusyInterval]: ...


@dataclass(frozen=True)
class CreatedCalendarEvent:
    event_id: str
    start: datetime
    end: datetime
    html_link: str | None
    join_url: str | None


@dataclass(frozen=True)
class VideoMeeting:
    meeting_id: str
    join_url: str
    host_url: str | None = None


class GoogleCalendarConnector:
    name = "google"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = RLock()
        self._service_instance: Any | None = None

    def _service(self) -> Any:
        with self._lock:
            if self._service_instance is not None:
                return self._service_instance
            try:
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build

                credentials = Credentials.from_authorized_user_file(
                    self.settings.google_oauth_token_file,
                    scopes=GOOGLE_SCOPES,
                )
                self._service_instance = build(
                    "calendar", "v3", credentials=credentials, cache_discovery=False
                )
            except Exception as exc:
                raise ConnectorAuthenticationError(
                    "Google Calendar is not authenticated"
                ) from exc
            return self._service_instance

    def busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        body = {
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "timeZone": self.settings.owner_timezone,
            "items": [
                {"id": calendar_id} for calendar_id in self.settings.busy_calendar_ids
            ],
        }
        try:
            response = self._service().freebusy().query(body=body).execute()
        except Exception as exc:
            raise ConnectorError("Google Calendar free/busy query failed") from exc

        intervals: list[BusyInterval] = []
        for calendar_id in self.settings.busy_calendar_ids:
            calendar = response.get("calendars", {}).get(calendar_id, {})
            if calendar.get("errors"):
                raise ConnectorError(f"Google Calendar could not query {calendar_id}")
            for block in calendar.get("busy", []):
                intervals.append(
                    BusyInterval(isoparse(block["start"]), isoparse(block["end"]))
                )
        return intervals

    def create_event(
        self,
        request: BookingRequest,
        *,
        end: datetime,
        join_url: str | None,
    ) -> CreatedCalendarEvent:
        owner_zone = ZoneInfo(self.settings.owner_timezone)
        start_local = request.start.astimezone(owner_zone)
        end_local = end.astimezone(owner_zone)
        event_name = (
            "Video meeting" if request.event_type == EventType.VIDEO else "Phone call"
        )
        description_lines = [
            f"Booked through {self.settings.public_base_url}",
            f"Guest timezone: {request.timezone}",
        ]
        if request.event_type == EventType.PHONE:
            description_lines.append(f"Phone: {request.phone}")
        if request.notes:
            description_lines.extend(["", "Notes:", request.notes])
        if join_url:
            description_lines.extend(["", f"Join: {join_url}"])

        body: dict[str, Any] = {
            "summary": f"{event_name} with {request.name}",
            "description": "\n".join(description_lines),
            "start": {
                "dateTime": start_local.isoformat(),
                "timeZone": self.settings.owner_timezone,
            },
            "end": {
                "dateTime": end_local.isoformat(),
                "timeZone": self.settings.owner_timezone,
            },
            "attendees": [{"email": str(request.email), "displayName": request.name}],
            "extendedProperties": {
                "private": {
                    "bookingSource": "calendar-hub",
                    "idempotencyKey": request.idempotency_key,
                    "appointmentType": request.event_type.value,
                }
            },
        }
        conference_data_version = 0
        if request.event_type == EventType.PHONE:
            body["location"] = f"Phone call — {request.phone}"
        elif self.settings.video_provider == "google_meet":
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": str(uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            conference_data_version = 1
        elif join_url:
            body["location"] = join_url

        try:
            event = (
                self._service()
                .events()
                .insert(
                    calendarId=self.settings.google_booking_calendar_id,
                    body=body,
                    sendUpdates="all",
                    conferenceDataVersion=conference_data_version,
                )
                .execute()
            )
        except Exception as exc:
            raise ConnectorError("Google Calendar event creation failed") from exc

        generated_join_url = join_url or event.get("hangoutLink")
        if not generated_join_url:
            for entry in event.get("conferenceData", {}).get("entryPoints", []):
                if entry.get("entryPointType") == "video":
                    generated_join_url = entry.get("uri")
                    break
        return CreatedCalendarEvent(
            event_id=event["id"],
            start=request.start.astimezone(UTC),
            end=end.astimezone(UTC),
            html_link=event.get("htmlLink"),
            join_url=generated_join_url,
        )

    def find_event_by_idempotency(
        self, idempotency_key: str
    ) -> CreatedCalendarEvent | None:
        """Recover a booking when a prior response was interrupted after creation."""

        try:
            response = (
                self._service()
                .events()
                .list(
                    calendarId=self.settings.google_booking_calendar_id,
                    privateExtendedProperty=f"idempotencyKey={idempotency_key}",
                    showDeleted=False,
                    singleEvents=True,
                    maxResults=1,
                )
                .execute()
            )
        except Exception as exc:
            raise ConnectorError("Google Calendar idempotency query failed") from exc
        events = response.get("items", [])
        if not events:
            return None
        event = events[0]
        try:
            event_start = isoparse(event["start"]["dateTime"])
            event_end = isoparse(event["end"]["dateTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectorError(
                "Google Calendar returned an invalid booking event"
            ) from exc
        join_url = event.get("hangoutLink")
        if not join_url:
            for entry in event.get("conferenceData", {}).get("entryPoints", []):
                if entry.get("entryPointType") == "video":
                    join_url = entry.get("uri")
                    break
        if not join_url and str(event.get("location", "")).startswith("https://"):
            join_url = event["location"]
        return CreatedCalendarEvent(
            event_id=event["id"],
            start=event_start.astimezone(UTC),
            end=event_end.astimezone(UTC),
            html_link=event.get("htmlLink"),
            join_url=join_url,
        )


class PyiCloudCalendarConnector:
    """Read-only bridge over this repository's pyiCloud calendar service."""

    name = "icloud"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._api: Any | None = None
        self._lock = RLock()

    def _client(self) -> Any:
        with self._lock:
            if self._api is not None:
                return self._api
            if not self.settings.pyicloud_apple_id:
                raise ConnectorAuthenticationError(
                    "PYICLOUD_APPLE_ID is not configured"
                )
            password = self.settings.read_secret(self.settings.pyicloud_password_file)
            try:
                from pyicloud import PyiCloudService

                api = PyiCloudService(
                    self.settings.pyicloud_apple_id,
                    password,
                    cookie_directory=self.settings.pyicloud_cookie_directory,
                    china_mainland=self.settings.pyicloud_china_mainland,
                )
            except Exception as exc:
                raise ConnectorAuthenticationError(
                    "iCloud authentication failed"
                ) from exc
            if api.requires_2fa or api.requires_2sa:
                raise ConnectorAuthenticationError(
                    "iCloud requires two-factor bootstrap authentication"
                )
            self._api = api
            return api

    def busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        owner_zone = ZoneInfo(self.settings.owner_timezone)
        try:
            with self._lock:
                events = (
                    self._client().calendar.events(
                        start.astimezone(owner_zone), end.astimezone(owner_zone)
                    )
                    or []
                )
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError("iCloud calendar query failed") from exc

        intervals: list[BusyInterval] = []
        for event in events:
            interval = parse_icloud_event(event, owner_zone)
            if interval and interval.start < end and start < interval.end:
                intervals.append(interval)
        return intervals


class IcsCalendarConnector:
    """Read-only connector for operator-configured HTTPS ICS feeds."""

    name = "ics"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        for url in settings.calendar_feed_urls:
            if urlparse(url).scheme != "https":
                raise ValueError("ICS calendar feeds must use HTTPS")

    def busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        try:
            import recurring_ical_events
            from icalendar import Calendar
        except ImportError as exc:
            raise ConnectorError("ICS dependencies are not installed") from exc

        intervals: list[BusyInterval] = []
        owner_zone = ZoneInfo(self.settings.owner_timezone)
        for url in self.settings.calendar_feed_urls:
            try:
                response = requests.get(
                    url,
                    timeout=self.settings.ics_request_timeout_seconds,
                    headers={"User-Agent": "calendar-hub/0.1"},
                )
                response.raise_for_status()
                calendar = Calendar.from_ical(response.content)
                events = recurring_ical_events.of(calendar).between(start, end)
            except Exception as exc:
                raise ConnectorError(
                    "An ICS calendar feed could not be queried"
                ) from exc
            for event in events:
                if str(event.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                if str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
                    continue
                event_start = _ical_datetime(event.decoded("DTSTART"), owner_zone)
                raw_end = event.decoded("DTEND") if event.get("DTEND") else None
                if raw_end is None:
                    raw_end = event_start + timedelta(hours=1)
                event_end = _ical_datetime(raw_end, owner_zone)
                if event_end > event_start:
                    intervals.append(BusyInterval(event_start, event_end))
        return intervals


class ZoomVideoProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._access_token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=UTC)
        self._lock = RLock()

    def _token(self) -> str:
        with self._lock:
            if self._access_token and datetime.now(UTC) < self._expires_at:
                return self._access_token
            if not self.settings.zoom_account_id or not self.settings.zoom_client_id:
                raise ConnectorAuthenticationError(
                    "Zoom credentials are not configured"
                )
            secret = self.settings.read_secret(self.settings.zoom_client_secret_file)
            try:
                response = requests.post(
                    "https://zoom.us/oauth/token",
                    params={
                        "grant_type": "account_credentials",
                        "account_id": self.settings.zoom_account_id,
                    },
                    auth=(self.settings.zoom_client_id, secret),
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise ConnectorAuthenticationError(
                    "Zoom authentication failed"
                ) from exc
            self._access_token = payload["access_token"]
            self._expires_at = datetime.now(UTC) + timedelta(
                seconds=max(int(payload.get("expires_in", 3600)) - 60, 60)
            )
            return self._access_token

    def create_meeting(
        self, request: BookingRequest, *, duration_minutes: int
    ) -> VideoMeeting:
        if not self.settings.zoom_user_id:
            raise ConnectorAuthenticationError("ZOOM_USER_ID is not configured")
        topic = f"Video meeting with {request.name}"
        payload = {
            "topic": topic,
            "type": 2,
            "start_time": request.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": duration_minutes,
            "timezone": self.settings.owner_timezone,
            "agenda": request.notes,
            "settings": {
                "join_before_host": False,
                "waiting_room": True,
                "mute_upon_entry": True,
                "approval_type": 2,
            },
        }
        try:
            response = requests.post(
                "https://api.zoom.us/v2/users/"
                f"{quote(self.settings.zoom_user_id, safe='')}/meetings",
                json=payload,
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=15,
            )
            response.raise_for_status()
            meeting = response.json()
        except Exception as exc:
            raise ConnectorError("Zoom meeting creation failed") from exc
        return VideoMeeting(
            meeting_id=str(meeting["id"]),
            join_url=meeting["join_url"],
            host_url=meeting.get("start_url"),
        )

    def delete_meeting(self, meeting_id: str) -> None:
        try:
            response = requests.delete(
                f"https://api.zoom.us/v2/meetings/{quote(meeting_id, safe='')}",
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=10,
            )
            if response.status_code not in (204, 404):
                response.raise_for_status()
        except (requests.RequestException, ConnectorError):
            # Cleanup is best effort; never hide the original booking failure.
            return


def parse_icloud_event(
    event: dict[str, Any], owner_zone: ZoneInfo
) -> BusyInterval | None:
    """Normalize the known iCloud event date formats without retaining content."""

    status = str(event.get("status") or event.get("STATUS") or "").upper()
    if status == "CANCELLED" or event.get("isDeleted"):
        return None
    start_value = _first_value(
        event, "localStartDate", "startDate", "startDateUTC", "start"
    )
    end_value = _first_value(event, "localEndDate", "endDate", "endDateUTC", "end")
    if start_value is None:
        return None
    start = parse_icloud_datetime(start_value, owner_zone)
    if end_value is None:
        end = start + (timedelta(days=1) if event.get("allDay") else timedelta(hours=1))
    else:
        end = parse_icloud_datetime(end_value, owner_zone)
    if end <= start:
        return None
    return BusyInterval(start, end)


def parse_icloud_datetime(value: Any, owner_zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        parsed = isoparse(value)
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        parsed = datetime.fromtimestamp(timestamp, tz=UTC)
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        parts = [int(part) for part in value[:6]]
        parts.extend([0] * (6 - len(parts)))
        parsed = datetime(*parts, tzinfo=owner_zone)
    elif isinstance(value, dict):
        nested = _first_value(value, "date", "value", "timestamp", "dateTime")
        if nested is None:
            raise ValueError("Unsupported iCloud date object")
        return parse_icloud_datetime(nested, owner_zone)
    else:
        raise ValueError("Unsupported iCloud date value")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=owner_zone)
    return parsed


def _first_value(payload: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _ical_datetime(value: Any, owner_zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=owner_zone)
    if isinstance(value, date):
        return datetime.combine(value, time.min, owner_zone)
    raise ValueError("Unsupported ICS date value")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
