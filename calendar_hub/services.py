"""Calendar aggregation, availability, and transactional booking services."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from threading import RLock
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from redis.exceptions import RedisError

from calendar_hub.config import Settings
from calendar_hub.connectors import (
    BusyCalendarConnector,
    ConnectorError,
    GoogleCalendarConnector,
    IcsCalendarConnector,
    PyiCloudCalendarConnector,
    ZoomVideoProvider,
)
from calendar_hub.domain import UTC, AvailabilityEngine, BusyInterval, merge_intervals
from calendar_hub.models import (
    AvailabilityResponse,
    AvailabilitySlot,
    BookingRequest,
    BookingResponse,
    EventType,
    EventTypeView,
)


class SlotUnavailableError(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    pass


class VerificationError(RuntimeError):
    pass


class BookingInfrastructureError(RuntimeError):
    pass


class BookingStore(Protocol):
    def get_idempotent(self, key: str) -> BookingResponse | None: ...

    def save_idempotent(self, key: str, response: BookingResponse) -> None: ...

    def acquire_slot(self, key: str, token: str) -> bool: ...

    def release_slot(self, key: str, token: str) -> None: ...

    def increment_rate_limit(self, key: str, ttl_seconds: int) -> int: ...


class RedisBookingStore:
    def __init__(self, redis_url: str) -> None:
        try:
            from redis import Redis

            self.client = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
        except Exception as exc:
            raise BookingInfrastructureError("Redis could not be configured") from exc

    def get_idempotent(self, key: str) -> BookingResponse | None:
        try:
            value = self.client.get(f"booking:idempotency:{key}")
        except Exception as exc:
            raise BookingInfrastructureError("Booking state is unavailable") from exc
        return BookingResponse.model_validate_json(value) if value else None

    def save_idempotent(self, key: str, response: BookingResponse) -> None:
        try:
            self.client.set(
                f"booking:idempotency:{key}",
                response.model_dump_json(),
                ex=86_400,
            )
        except Exception as exc:
            raise BookingInfrastructureError(
                "Booking state could not be saved"
            ) from exc

    def acquire_slot(self, key: str, token: str) -> bool:
        try:
            return bool(self.client.set(f"booking:slot:{key}", token, nx=True, ex=45))
        except Exception as exc:
            raise BookingInfrastructureError("Booking lock is unavailable") from exc

    def release_slot(self, key: str, token: str) -> None:
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            self.client.eval(script, 1, f"booking:slot:{key}", token)
        except RedisError:
            return

    def increment_rate_limit(self, key: str, ttl_seconds: int) -> int:
        try:
            with self.client.pipeline() as pipeline:
                pipeline.incr(key)
                pipeline.expire(key, ttl_seconds, nx=True)
                count, _ = pipeline.execute()
            return int(count)
        except Exception as exc:
            raise BookingInfrastructureError("Rate limiter is unavailable") from exc


class InMemoryBookingStore:
    """Deterministic test/development store; do not use with multiple workers."""

    def __init__(self) -> None:
        self._idempotency: dict[str, BookingResponse] = {}
        self._locks: dict[str, str] = {}
        self._rate_limits: dict[str, int] = {}
        self._lock = RLock()

    def get_idempotent(self, key: str) -> BookingResponse | None:
        with self._lock:
            return self._idempotency.get(key)

    def save_idempotent(self, key: str, response: BookingResponse) -> None:
        with self._lock:
            self._idempotency[key] = response

    def acquire_slot(self, key: str, token: str) -> bool:
        with self._lock:
            if key in self._locks:
                return False
            self._locks[key] = token
            return True

    def release_slot(self, key: str, token: str) -> None:
        with self._lock:
            if self._locks.get(key) == token:
                self._locks.pop(key, None)

    def increment_rate_limit(self, key: str, ttl_seconds: int) -> int:
        del ttl_seconds
        with self._lock:
            self._rate_limits[key] = self._rate_limits.get(key, 0) + 1
            return self._rate_limits[key]


class CalendarHub:
    """Fail-closed aggregation of all configured busy-time sources."""

    def __init__(self, connectors: list[BusyCalendarConnector]) -> None:
        self.connectors = connectors

    def busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        if end <= start:
            raise ValueError("Busy query end must be after start")
        intervals: list[BusyInterval] = []
        for connector in self.connectors:
            intervals.extend(connector.busy(start, end))
        return merge_intervals(intervals)


class CalendarHubService:
    def __init__(
        self,
        settings: Settings,
        *,
        hub: CalendarHub | None = None,
        google: GoogleCalendarConnector | None = None,
        store: BookingStore | None = None,
        zoom: ZoomVideoProvider | None = None,
    ) -> None:
        self.settings = settings
        self.google = google or GoogleCalendarConnector(settings)
        if hub is None:
            connectors: list[BusyCalendarConnector] = [self.google]
            if settings.pyicloud_enabled:
                connectors.append(PyiCloudCalendarConnector(settings))
            if settings.calendar_feed_urls:
                connectors.append(IcsCalendarConnector(settings))
            hub = CalendarHub(connectors)
        self.hub = hub
        self.store = store or RedisBookingStore(settings.redis_url)
        self.zoom = zoom or ZoomVideoProvider(settings)
        self.engine = AvailabilityEngine(
            owner_timezone=settings.owner_timezone,
            workday_start=settings.availability_start,
            workday_end=settings.availability_end,
            weekdays=settings.weekday_numbers,
            increment_minutes=settings.slot_increment_minutes,
            minimum_notice_hours=settings.minimum_notice_hours,
            booking_window_days=settings.booking_window_days,
            buffer_before_minutes=settings.buffer_before_minutes,
            buffer_after_minutes=settings.buffer_after_minutes,
        )

    def event_types(self) -> list[EventTypeView]:
        return [
            EventTypeView(
                id=EventType.VIDEO,
                name="Video meeting",
                description=(
                    "A focused conversation by Zoom"
                    if self.settings.video_provider == "zoom"
                    else "A focused conversation by Google Meet"
                ),
                duration_minutes=self.settings.video_duration_minutes,
            ),
            EventTypeView(
                id=EventType.PHONE,
                name="Phone call",
                description=(
                    f"{self.settings.page_owner_name} will call the number you provide"
                ),
                duration_minutes=self.settings.phone_duration_minutes,
            ),
        ]

    def duration_for(self, event_type: EventType) -> int:
        if event_type == EventType.VIDEO:
            return self.settings.video_duration_minutes
        return self.settings.phone_duration_minutes

    def availability(
        self,
        *,
        event_type: EventType,
        first_day: date,
        days: int,
        visitor_timezone: str,
        now: datetime | None = None,
    ) -> AvailabilityResponse:
        _zone(visitor_timezone)
        owner_zone = ZoneInfo(self.settings.owner_timezone)
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        current_day = now_utc.astimezone(owner_zone).date()
        if first_day < current_day or first_day > current_day + timedelta(
            days=self.settings.booking_window_days
        ):
            raise ValueError("from_date is outside the booking window")
        query_start = (
            datetime.combine(first_day, self.engine.workday_start, owner_zone)
            - self.engine.buffer_before
        )
        query_end = (
            datetime.combine(
                first_day + timedelta(days=days), self.engine.workday_end, owner_zone
            )
            + self.engine.buffer_after
        )
        busy = self.hub.busy(query_start.astimezone(UTC), query_end.astimezone(UTC))
        slots = self.engine.available_slots(
            first_day=first_day,
            days=days,
            duration_minutes=self.duration_for(event_type),
            busy=busy,
            now=now_utc,
        )
        visitor_zone = ZoneInfo(visitor_timezone)
        return AvailabilityResponse(
            timezone=visitor_timezone,
            event_type=event_type,
            slots=[
                AvailabilitySlot(
                    start=slot.start.astimezone(visitor_zone),
                    end=slot.end.astimezone(visitor_zone),
                )
                for slot in slots
            ],
        )

    def book(
        self, request: BookingRequest, *, now: datetime | None = None
    ) -> BookingResponse:
        _zone(request.timezone)
        existing = self.store.get_idempotent(request.idempotency_key)
        if existing:
            return existing

        start_utc = request.start.astimezone(UTC)
        duration = self.duration_for(request.event_type)
        end_utc = start_utc + timedelta(minutes=duration)
        # All appointment types share one short lock. This closes races between
        # different durations whose intervals overlap before Google is updated.
        slot_key = "owner-calendar"
        lock_token = str(uuid4())
        if not self.store.acquire_slot(slot_key, lock_token):
            raise SlotUnavailableError("This time is being booked by someone else")

        meeting = None
        try:
            existing = self.store.get_idempotent(request.idempotency_key)
            if existing:
                return existing
            finder = getattr(self.google, "find_event_by_idempotency", None)
            recovered_event = finder(request.idempotency_key) if finder else None
            if recovered_event:
                recovered = BookingResponse(
                    booking_id=recovered_event.event_id,
                    event_type=request.event_type,
                    start=recovered_event.start,
                    end=recovered_event.end,
                    join_url=recovered_event.join_url,
                    calendar_event_url=recovered_event.html_link,
                )
                self.store.save_idempotent(request.idempotency_key, recovered)
                return recovered
            busy = self.hub.busy(
                start_utc - self.engine.buffer_before,
                end_utc + self.engine.buffer_after,
            )
            if not self.engine.is_valid_slot(
                start=start_utc,
                duration_minutes=duration,
                busy=busy,
                now=now,
            ):
                raise SlotUnavailableError("This time is no longer available")

            join_url = None
            if (
                request.event_type == EventType.VIDEO
                and self.settings.video_provider == "zoom"
            ):
                meeting = self.zoom.create_meeting(request, duration_minutes=duration)
                join_url = meeting.join_url
            event = self.google.create_event(request, end=end_utc, join_url=join_url)
            response = BookingResponse(
                booking_id=event.event_id,
                event_type=request.event_type,
                start=event.start,
                end=event.end,
                join_url=event.join_url,
                calendar_event_url=event.html_link,
            )
            try:
                self.store.save_idempotent(request.idempotency_key, response)
            except BookingInfrastructureError:
                # The Google event and invite are authoritative. A transient
                # cache failure must not turn a successful booking into an error.
                pass
            return response
        except Exception:
            if meeting is not None and "event" not in locals():
                self.zoom.delete_meeting(meeting.meeting_id)
            raise
        finally:
            self.store.release_slot(slot_key, lock_token)

    def enforce_rate_limit(self, client_key: str) -> None:
        bucket = datetime.now(UTC).strftime("%Y%m%d%H%M")
        key = f"rate:booking:{client_key}:{bucket}"
        count = self.store.increment_rate_limit(key, ttl_seconds=120)
        if count > self.settings.rate_limit_per_minute:
            raise RateLimitError("Too many booking requests; try again shortly")

    def verify_turnstile(self, token: str, remote_ip: str) -> None:
        if not self.settings.turnstile_secret_file:
            if self.settings.turnstile_required:
                raise BookingInfrastructureError("Human verification is not configured")
            return
        if not token:
            raise VerificationError("Human verification is required")
        secret = self.settings.read_secret(self.settings.turnstile_secret_file)
        try:
            response = requests.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": token, "remoteip": remote_ip},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise BookingInfrastructureError(
                "Human verification is unavailable"
            ) from exc
        if not payload.get("success"):
            raise VerificationError("Human verification failed")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def serialize_service_error(error: Exception) -> str:
    """Return a public-safe error message without leaking connector details."""

    if isinstance(error, (SlotUnavailableError, RateLimitError, VerificationError)):
        return str(error)
    if isinstance(error, (ConnectorError, BookingInfrastructureError)):
        return "Calendar availability is temporarily unavailable"
    return "The booking could not be completed"
