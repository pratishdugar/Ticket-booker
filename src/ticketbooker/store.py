"""Tiny JSON file store for bookings."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Booking


class BookingStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def save(self, booking: Booking) -> Booking:
        data = self._load()
        data[booking.booking_id] = booking.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
        return booking

    def get(self, booking_id: str) -> Booking:
        data = self._load()
        if booking_id not in data:
            raise KeyError(f"No booking with id '{booking_id}'")
        return Booking.model_validate(data[booking_id])

    def all(self) -> list[Booking]:
        bookings = [Booking.model_validate(b) for b in self._load().values()]
        return sorted(bookings, key=lambda b: b.created_at, reverse=True)
