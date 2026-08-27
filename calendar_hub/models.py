"""Public API models."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class EventType(str, Enum):
    VIDEO = "video"
    PHONE = "phone"


class EventTypeView(BaseModel):
    id: EventType
    name: str
    description: str
    duration_minutes: int


class AvailabilityQuery(BaseModel):
    event_type: EventType
    from_date: date
    days: int = Field(default=14, ge=1, le=31)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


class AvailabilitySlot(BaseModel):
    start: datetime
    end: datetime


class AvailabilityResponse(BaseModel):
    timezone: str
    event_type: EventType
    slots: list[AvailabilitySlot]


class BookingRequest(BaseModel):
    event_type: EventType
    start: datetime
    timezone: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=16, max_length=100)
    turnstile_token: str = Field(default="", max_length=4096)

    @field_validator("name", "phone", "notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_phone_booking(self) -> BookingRequest:
        if self.event_type == EventType.PHONE:
            compact = "".join(
                character for character in self.phone if character.isdigit()
            )
            if not 7 <= len(compact) <= 15:
                raise ValueError(
                    "A valid phone number is required for phone appointments"
                )
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("start must include a UTC offset")
        return self


class BookingResponse(BaseModel):
    booking_id: str
    event_type: EventType
    start: datetime
    end: datetime
    status: str = "confirmed"
    join_url: str | None = None
    calendar_event_url: str | None = None
