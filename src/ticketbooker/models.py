"""Domain models shared by providers, the matcher and the agent."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Category(str, Enum):
    TRAIN = "train"
    BUS = "bus"
    FLIGHT = "flight"
    EVENT = "event"
    MOVIE = "movie"
    OTHER = "other"


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    RAC = "RAC"  # Reservation Against Cancellation (Indian Railways)
    WAITLIST = "WAITLIST"
    REGRET = "REGRET"  # sold out / not bookable
    UNKNOWN = "UNKNOWN"


class BookingStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    WAITLISTED = "WAITLISTED"
    PENDING_USER_ACTION = "PENDING_USER_ACTION"  # e.g. user must log in / solve captcha / pay
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Passenger(BaseModel):
    name: str
    age: int
    gender: str = Field(description="M, F or T")
    berth_preference: str | None = None  # LB, MB, UB, SL, SU, WINDOW, AISLE ...
    meal_preference: str | None = None
    id_type: str | None = None
    id_number: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Contact(BaseModel):
    email: str | None = None
    phone: str | None = None


class SearchQuery(BaseModel):
    category: Category
    origin: str | None = None
    destination: str | None = None
    date: date
    travel_class: str | None = None
    quota: str | None = None  # IRCTC quota: GN, TQ, LD, SS ...
    passengers: int = 1
    extra: dict[str, Any] = Field(default_factory=dict)


class TicketOption(BaseModel):
    option_id: str
    provider: str
    category: Category
    title: str
    origin: str | None = None
    destination: str | None = None
    departure: datetime | None = None
    arrival: datetime | None = None
    travel_class: str | None = None
    price: float | None = None
    currency: str = "INR"
    availability: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    seats_available: int | None = None
    waitlist_position: int | None = None
    raw_availability: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_minutes(self) -> int | None:
        if self.departure and self.arrival:
            return int((self.arrival - self.departure).total_seconds() // 60)
        return None

    def summary(self) -> dict[str, Any]:
        """Compact representation for the agent / CLI."""
        data = self.model_dump(mode="json", exclude_none=True, exclude={"attributes"})
        if self.duration_minutes is not None:
            data["duration_minutes"] = self.duration_minutes
        return data


class Booking(BaseModel):
    booking_id: str
    provider: str
    option: TicketOption
    passengers: list[Passenger]
    status: BookingStatus
    reference: str | None = None  # PNR / booking reference issued by the provider
    created_at: datetime = Field(default_factory=datetime.now)
    handoff_url: str | None = None
    notes: list[str] = Field(default_factory=list)
    provider_data: dict[str, Any] = Field(default_factory=dict)


_AVAIL_PATTERNS: list[tuple[re.Pattern[str], AvailabilityStatus]] = [
    (re.compile(r"\bREGRET|NOT\s*AVAILABLE|SOLD\s*OUT|TRAIN\s*DEPARTED|CLOSED", re.I), AvailabilityStatus.REGRET),
    (re.compile(r"\bRAC\s*/?\s*(\d+)?", re.I), AvailabilityStatus.RAC),
    (re.compile(r"(?:[A-Z]*WL|WAIT\s*LIST(?:ED)?)\s*/?\s*(\d+)?", re.I), AvailabilityStatus.WAITLIST),
    (re.compile(r"\b(?:AVL|AVAILABLE|CURR_?AVBL|CNF|CONFIRMED?)\b[\s\-#]*(\d+)?", re.I), AvailabilityStatus.AVAILABLE),
]


def parse_availability(value: Any) -> tuple[AvailabilityStatus, int | None]:
    """Normalise provider availability strings.

    Handles IRCTC-style strings ("AVAILABLE-0045", "RAC 12", "GNWL23/WL10",
    "REGRET/WL"), plain numbers (seats left) and common words.
    Returns (status, number) where number is seats left, RAC number or
    waitlist position depending on status.
    """
    if value is None:
        return AvailabilityStatus.UNKNOWN, None
    if isinstance(value, bool):
        return (AvailabilityStatus.AVAILABLE if value else AvailabilityStatus.REGRET), None
    if isinstance(value, (int, float)):
        n = int(value)
        return (AvailabilityStatus.AVAILABLE if n > 0 else AvailabilityStatus.REGRET), n
    text = str(value).strip()
    if not text:
        return AvailabilityStatus.UNKNOWN, None
    if text.isdigit():
        n = int(text)
        return (AvailabilityStatus.AVAILABLE if n > 0 else AvailabilityStatus.REGRET), n
    for pattern, status in _AVAIL_PATTERNS:
        match = pattern.search(text)
        if match:
            number = None
            if status is AvailabilityStatus.WAITLIST:
                # "GNWL23/WL10": the current position is the last number.
                numbers = re.findall(r"WL\s*(\d+)", text, re.I) or re.findall(r"(\d+)", text)
                number = int(numbers[-1]) if numbers else None
            elif match.groups() and match.group(1):
                number = int(match.group(1))
            return status, number
    return AvailabilityStatus.UNKNOWN, None
