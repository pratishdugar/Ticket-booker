"""Deterministic offline provider for demos, tests and trying the agent without real APIs."""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta

from ..models import (
    AvailabilityStatus,
    Booking,
    BookingStatus,
    Category,
    Contact,
    Passenger,
    SearchQuery,
    TicketOption,
    parse_availability,
)
from .base import Provider, ProviderError

_TRAINS = [
    ("12951", "Rajdhani Express"),
    ("12953", "August Kranti Rajdhani"),
    ("12909", "Garib Rath Express"),
    ("22209", "Duronto Express"),
    ("12925", "Paschim Express"),
    ("12137", "Punjab Mail"),
]
_TRAIN_CLASSES = {"SL": 0.35, "3A": 1.0, "2A": 1.45, "1A": 2.4}
_OPERATORS = {
    Category.BUS: ["IntrCity SmartBus", "Zingbus", "VRL Travels", "Orange Tours"],
    Category.FLIGHT: ["IndiGo", "Air India", "Akasa Air", "SpiceJet"],
    Category.EVENT: ["Main Stage", "Fan Pit", "Gold Lounge"],
    Category.MOVIE: ["PVR", "INOX", "Cinepolis"],
    Category.OTHER: ["Standard"],
}


class MockProvider(Provider):
    provider_type = "mock"

    def search(self, query: SearchQuery) -> list[TicketOption]:
        if not self.supports(query.category):
            raise ProviderError(f"{self.name} does not serve {query.category.value}")
        seed = f"{self.settings.get('seed', 0)}|{query.category.value}|{query.origin}|{query.destination}|{query.date}"
        rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
        if query.category is Category.TRAIN:
            return self._trains(query, rng)
        return self._generic(query, rng)

    def _availability(self, rng: random.Random) -> str:
        roll = rng.random()
        if roll < 0.45:
            return f"AVAILABLE-{rng.randint(1, 120):04d}"
        if roll < 0.6:
            return f"RAC {rng.randint(1, 40)}"
        if roll < 0.9:
            return f"GNWL{rng.randint(5, 80)}/WL{rng.randint(1, 60)}"
        return "REGRET"

    def _trains(self, query: SearchQuery, rng: random.Random) -> list[TicketOption]:
        options: list[TicketOption] = []
        base_day = datetime.combine(query.date, datetime.min.time())
        classes = [query.travel_class.upper()] if query.travel_class else list(_TRAIN_CLASSES)
        for number, name in rng.sample(_TRAINS, k=4):
            departure = base_day + timedelta(hours=rng.randint(5, 22), minutes=rng.choice([0, 15, 30, 45]))
            arrival = departure + timedelta(hours=rng.randint(12, 20), minutes=rng.choice([0, 10, 25, 40]))
            base_fare = rng.randint(1500, 2300)
            for cls in classes:
                if cls not in _TRAIN_CLASSES:
                    continue
                raw = self._availability(rng)
                status, count = parse_availability(raw)
                options.append(
                    TicketOption(
                        option_id=f"{self.name}:{number}:{query.date:%Y%m%d}:{cls}:{query.quota or 'GN'}",
                        provider=self.name,
                        category=Category.TRAIN,
                        title=f"{number} {name}",
                        origin=(query.origin or "").upper(),
                        destination=(query.destination or "").upper(),
                        departure=departure,
                        arrival=arrival,
                        travel_class=cls,
                        price=round(base_fare * _TRAIN_CLASSES[cls]),
                        availability=status,
                        seats_available=count if status is AvailabilityStatus.AVAILABLE else None,
                        waitlist_position=count if status is AvailabilityStatus.WAITLIST else None,
                        raw_availability=raw,
                        attributes={"number": number, "quota": query.quota or "GN"},
                    )
                )
        return options

    def _generic(self, query: SearchQuery, rng: random.Random) -> list[TicketOption]:
        options: list[TicketOption] = []
        base_day = datetime.combine(query.date, datetime.min.time())
        for i, operator in enumerate(_OPERATORS[query.category]):
            departure = base_day + timedelta(hours=rng.randint(6, 22), minutes=rng.choice([0, 30]))
            hours = {Category.FLIGHT: rng.randint(1, 3), Category.BUS: rng.randint(6, 14)}.get(query.category, 3)
            raw = self._availability(rng)
            status, count = parse_availability(raw)
            if status in (AvailabilityStatus.RAC, AvailabilityStatus.WAITLIST):
                status, count, raw = AvailabilityStatus.AVAILABLE, rng.randint(1, 30), "AVAILABLE"
            options.append(
                TicketOption(
                    option_id=f"{self.name}:{query.category.value}:{query.date:%Y%m%d}:{i}",
                    provider=self.name,
                    category=query.category,
                    title=operator,
                    origin=query.origin,
                    destination=query.destination,
                    departure=departure,
                    arrival=departure + timedelta(hours=hours),
                    travel_class=query.travel_class or "STANDARD",
                    price=float(rng.randint(400, 9000)),
                    availability=status,
                    seats_available=count,
                    raw_availability=raw,
                    attributes={"operator": operator},
                )
            )
        return options

    def book(self, option: TicketOption, passengers: list[Passenger], contact: Contact) -> Booking:
        if option.availability is AvailabilityStatus.REGRET:
            raise ProviderError("Option is not bookable (regret/sold out)")
        confirmed = option.availability is AvailabilityStatus.AVAILABLE
        return Booking(
            booking_id=uuid.uuid4().hex[:12],
            provider=self.name,
            option=option,
            passengers=passengers,
            status=BookingStatus.CONFIRMED if confirmed else BookingStatus.WAITLISTED,
            reference=f"MOCK{uuid.uuid4().int % 10**10:010d}",
            notes=["Mock booking - no real ticket was purchased."],
        )
