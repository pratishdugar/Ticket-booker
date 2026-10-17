"""Indian Railways (IRCTC) provider.

IRCTC has no public booking API and its terms of use prohibit automated
booking software on irctc.co.in, so this provider deliberately does not
script the website (no login automation, no captcha solving). Instead:

* **Search / availability / PNR status** go through a rail-data API of your
  choice, configured exactly like the generic ``http`` provider.
* **Booking** works in one of two modes:

  - ``handoff`` (default): the agent picks the train, validates the
    passengers and prepares a ready-to-enter booking sheet; you finish on
    IRCTC (login, captcha, payment). Booking status is
    ``PENDING_USER_ACTION`` until you record the PNR.
  - ``api``: if you have access to an authorised booking API (e.g. as an
    IRCTC-authorised agent / B2B partner), configure a ``book`` endpoint and
    the agent books through it after your confirmation.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from typing import Any

from ..models import Booking, BookingStatus, Category, Contact, Passenger, SearchQuery, TicketOption
from .base import ProviderError
from .http_api import HttpApiProvider

IRCTC_CLASSES = {"1A", "2A", "3A", "3E", "SL", "CC", "EC", "EA", "FC", "2S"}
IRCTC_QUOTAS = {"GN", "TQ", "PT", "LD", "SS", "HP", "DF", "FT", "YU"}
TATKAL_QUOTAS = {"TQ", "PT"}
BERTH_CODES = {"LB", "MB", "UB", "SL", "SU", "WS", "NP"}
_STATION = re.compile(r"^[A-Z]{2,5}$")


class IrctcProvider(HttpApiProvider):
    provider_type = "irctc"

    def __init__(self, name: str, settings: dict[str, Any] | None = None, transport=None):
        settings = {"categories": ["train"], **(settings or {})}
        super().__init__(name, settings, transport)
        self.booking_mode = str(self.settings.get("booking_mode", "handoff")).lower()
        if self.booking_mode not in {"handoff", "api"}:
            raise ProviderError(f"{name}: booking_mode must be 'handoff' or 'api'")
        self.default_quota = str(self.settings.get("default_quota", "GN")).upper()
        self.advance_days = int(self.settings.get("advance_reservation_days", 60))
        self.handoff_url = self.settings.get("handoff_url", "https://www.irctc.co.in/nget/train-search")

    def default_categories(self) -> list[str]:
        return [Category.TRAIN.value]

    # ---------------------------------------------------------------- search
    def validate_query(self, query: SearchQuery) -> SearchQuery:
        origin = (query.origin or "").strip().upper()
        destination = (query.destination or "").strip().upper()
        for label, code in (("origin", origin), ("destination", destination)):
            if not _STATION.match(code):
                raise ProviderError(
                    f"IRCTC needs station codes (e.g. NDLS, BCT, SBC); got {label}={code or 'empty'!r}"
                )
        today = date.today()
        if query.date < today:
            raise ProviderError(f"Journey date {query.date} is in the past")
        if query.date > today + timedelta(days=self.advance_days):
            raise ProviderError(
                f"Journey date {query.date} is beyond the {self.advance_days}-day advance reservation window"
            )
        travel_class = query.travel_class.upper() if query.travel_class else None
        if travel_class and travel_class not in IRCTC_CLASSES:
            raise ProviderError(f"Unknown IRCTC class {travel_class}; use one of {sorted(IRCTC_CLASSES)}")
        quota = (query.quota or self.default_quota).upper()
        if quota not in IRCTC_QUOTAS:
            raise ProviderError(f"Unknown IRCTC quota {quota}; use one of {sorted(IRCTC_QUOTAS)}")
        return query.model_copy(
            update={"origin": origin, "destination": destination, "travel_class": travel_class, "quota": quota}
        )

    def search(self, query: SearchQuery) -> list[TicketOption]:
        if not self.has_endpoint("search"):
            raise ProviderError(
                f"Provider '{self.name}' has no search endpoint. IRCTC has no public API: configure a "
                "rail-data API under endpoints.search (see config.example.yaml), or try the 'mock' provider."
            )
        query = self.validate_query(query)
        options = super().search(query)
        for option in options:
            option.attributes.setdefault("quota", query.quota)
        return options

    # --------------------------------------------------------------- booking
    def validate_passengers(self, option: TicketOption, passengers: list[Passenger]) -> list[str]:
        problems: list[str] = []
        quota = str(option.attributes.get("quota") or self.default_quota).upper()
        limit = 4 if quota in TATKAL_QUOTAS else 6
        if not passengers:
            problems.append("at least one passenger is required")
        if len(passengers) > limit:
            problems.append(f"{quota} quota allows at most {limit} passengers per PNR")
        for p in passengers:
            if p.age < 0 or p.age > 125:
                problems.append(f"{p.name}: invalid age {p.age}")
            if p.gender.upper() not in {"M", "F", "T"}:
                problems.append(f"{p.name}: gender must be M, F or T")
            if p.berth_preference and p.berth_preference.upper() not in BERTH_CODES:
                problems.append(f"{p.name}: berth preference must be one of {sorted(BERTH_CODES)}")
        return problems

    def booking_sheet(self, option: TicketOption, passengers: list[Passenger], contact: Contact) -> dict[str, Any]:
        return {
            "from": option.origin,
            "to": option.destination,
            "journey_date": option.departure.date().isoformat() if option.departure else None,
            "train": option.title,
            "train_number": option.attributes.get("number"),
            "class": option.travel_class,
            "quota": option.attributes.get("quota") or self.default_quota,
            "availability_when_prepared": option.raw_availability or option.availability.value,
            "fare_estimate": option.price,
            "passengers": [
                {
                    "name": p.name,
                    "age": p.age,
                    "gender": p.gender.upper(),
                    "berth_preference": (p.berth_preference or "NP").upper(),
                    **({"food": p.meal_preference} if p.meal_preference else {}),
                }
                for p in passengers
            ],
            "contact": contact.model_dump(exclude_none=True),
        }

    def book(self, option: TicketOption, passengers: list[Passenger], contact: Contact) -> Booking:
        problems = self.validate_passengers(option, passengers)
        if problems:
            raise ProviderError("Cannot book: " + "; ".join(problems))
        if self.booking_mode == "api":
            return super().book(option, passengers, contact)
        sheet = self.booking_sheet(option, passengers, contact)
        return Booking(
            booking_id=uuid.uuid4().hex[:12],
            provider=self.name,
            option=option,
            passengers=passengers,
            status=BookingStatus.PENDING_USER_ACTION,
            handoff_url=self.handoff_url,
            notes=[
                "Open the IRCTC link and sign in with your own IRCTC account.",
                f"Search {sheet['from']} -> {sheet['to']} on {sheet['journey_date']}, quota {sheet['quota']}.",
                f"Choose {sheet['train']} in class {sheet['class']} and enter the passengers below "
                "(saving them in your IRCTC Master List makes this faster next time).",
                "Complete captcha, OTP and payment yourself, then record the PNR with "
                "`ticketbooker record-pnr <booking_id> <PNR>` so status checks work.",
            ],
            provider_data={"booking_sheet": sheet},
        )
