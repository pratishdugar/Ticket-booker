"""Generic JSON-over-HTTP provider, configured entirely from YAML.

Point it at any ticketing API (bus, flight, events, a rail data API, a
licensed booking partner API ...) by describing each endpoint's request
template and how to map response fields onto :class:`TicketOption`.

Example::

    - name: my_bus_api
      type: http
      categories: [bus]
      base_url: https://api.example.com/v1
      headers: {Authorization: "Bearer ${BUS_API_KEY}"}
      endpoints:
        search:
          method: GET
          path: /trips
          params: {from: "{query.origin}", to: "{query.destination}", date: "{query.date:%Y-%m-%d}"}
          results: data.trips            # dotted path to the list of results
          fields:                         # TicketOption field -> dotted path in each result
            option_id: id
            title: operator.name
            departure: departs_at
            arrival: arrives_at
            price: fare.total
            availability: seats_left
        book:
          method: POST
          path: /bookings
          body: {trip_id: "{option.attributes[id]}", passengers: "{passengers}", email: "{contact.email}"}
          fields: {reference: data.pnr, status: data.status}

Templates use Python ``str.format`` syntax against a context containing
``query``, ``option``, ``passengers``, ``contact``, ``booking`` and
``settings``. A string that is exactly one placeholder (``"{passengers}"``)
is replaced by the raw value, so lists and numbers keep their JSON type.
Every raw result item is kept in ``option.attributes`` for later templates.
"""

from __future__ import annotations

import json
import string
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel

from ..models import (
    AvailabilityStatus,
    Booking,
    BookingStatus,
    Contact,
    Passenger,
    SearchQuery,
    TicketOption,
    parse_availability,
)
from .base import BookingNotSupported, Provider, ProviderError

_FORMATTER = string.Formatter()

# signature: (method, url, headers, body_bytes, timeout) -> (status, response_text)
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, str]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise ProviderError(f"Network error calling {url}: {exc.reason}") from exc


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def render(template: Any, context: dict[str, Any]) -> Any:
    """Recursively render a request template."""
    if isinstance(template, dict):
        return {k: render(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, context) for v in template]
    if not isinstance(template, str):
        return template
    parsed = list(_FORMATTER.parse(template))
    if len(parsed) == 1 and parsed[0][0] == "" and parsed[0][1] and not parsed[0][2] and not parsed[0][3]:
        # Whole string is a single bare placeholder: keep the raw value's type.
        try:
            value, _ = _FORMATTER.get_field(parsed[0][1], (), context)
        except (KeyError, AttributeError, IndexError) as exc:
            raise ProviderError(f"Template {template!r} references a missing value: {exc}") from exc
        return to_jsonable(value)
    try:
        return _FORMATTER.vformat(template, (), context)
    except (KeyError, AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ProviderError(f"Template {template!r} references a missing value: {exc}") from exc


def dig(data: Any, path: str | None) -> Any:
    """Follow a dotted path ("data.trips.0.id") through dicts and lists."""
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.lstrip("-").isdigit():
            idx = int(part)
            current = current[idx] if -len(current) <= idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


class HttpApiProvider(Provider):
    provider_type = "http"

    def __init__(self, name: str, settings: dict[str, Any] | None = None, transport: Transport | None = None):
        super().__init__(name, settings)
        self.base_url = str(self.settings.get("base_url", "")).rstrip("/")
        self.headers = {"Accept": "application/json", **(self.settings.get("headers") or {})}
        self.timeout = float(self.settings.get("timeout", 30))
        self.endpoints: dict[str, dict[str, Any]] = self.settings.get("endpoints") or {}
        self.datetime_formats: list[str] = self.settings.get("datetime_formats") or []
        self.transport: Transport = transport or urllib_transport

    # ------------------------------------------------------------------ http
    def has_endpoint(self, name: str) -> bool:
        return name in self.endpoints

    def call(self, endpoint: str, context: dict[str, Any]) -> Any:
        spec = self.endpoints.get(endpoint)
        if not spec:
            raise ProviderError(f"Provider '{self.name}' has no '{endpoint}' endpoint configured")
        context = {"settings": self.settings, **context}
        method = str(spec.get("method", "GET")).upper()
        url = spec.get("url") or f"{self.base_url}{render(spec.get('path', ''), context)}"
        params = render(spec.get("params") or {}, context)
        params = {k: v for k, v in params.items() if v not in (None, "", "None")}
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        headers = {**self.headers, **render(spec.get("headers") or {}, context)}
        body = None
        if "body" in spec:
            body = json.dumps(render(spec["body"], context)).encode()
            headers.setdefault("Content-Type", "application/json")
        status, text = self.transport(method, url, headers, body, self.timeout)
        if status >= 400:
            raise ProviderError(f"{self.name} {endpoint} failed with HTTP {status}: {text[:300]}")
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.name} {endpoint} returned non-JSON: {text[:200]}") from exc
        error_path = spec.get("error")
        if error_path and dig(data, error_path):
            raise ProviderError(f"{self.name} {endpoint} error: {dig(data, error_path)}")
        return data

    # --------------------------------------------------------------- mapping
    def _parse_datetime(self, value: Any, on_date: date | None) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value)
        text = str(value).strip()
        for fmt in self.datetime_formats:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        if on_date:
            for fmt in ("%H:%M", "%H:%M:%S", "%H.%M"):
                try:
                    return datetime.combine(on_date, datetime.strptime(text, fmt).time())
                except ValueError:
                    pass
        return None

    def map_option(self, item: dict[str, Any], query: SearchQuery, fields: dict[str, str]) -> TicketOption:
        get = lambda key: dig(item, fields[key]) if key in fields else None  # noqa: E731
        defaults = self.endpoints.get("search", {}).get("defaults") or {}
        raw_avail = get("availability")
        status, count = parse_availability(raw_avail)
        departure = self._parse_datetime(get("departure"), query.date)
        arrival = self._parse_datetime(get("arrival"), query.date)
        if departure and arrival and arrival < departure and arrival.date() == departure.date():
            # Time-only values for overnight journeys.
            arrival += timedelta(days=int(get("arrival_day_offset") or 1))
        seats = get("seats_available")
        native_id = get("option_id") or uuid.uuid4().hex[:10]
        price = get("price")
        return TicketOption(
            option_id=f"{self.name}:{native_id}",
            provider=self.name,
            category=query.category,
            title=str(get("title") or defaults.get("title") or native_id),
            origin=get("origin") or query.origin,
            destination=get("destination") or query.destination,
            departure=departure,
            arrival=arrival,
            travel_class=get("travel_class") or query.travel_class or defaults.get("travel_class"),
            price=float(price) if price not in (None, "") else None,
            currency=get("currency") or defaults.get("currency", "INR"),
            availability=status,
            seats_available=int(seats) if seats not in (None, "") else (count if status is AvailabilityStatus.AVAILABLE else None),
            waitlist_position=count if status is AvailabilityStatus.WAITLIST else None,
            raw_availability=None if raw_avail is None else str(raw_avail),
            attributes={**item, "number": get("number") or item.get("number"), "native_id": native_id},
        )

    # ------------------------------------------------------------- interface
    def search(self, query: SearchQuery) -> list[TicketOption]:
        spec = self.endpoints.get("search")
        if not spec:
            raise ProviderError(f"Provider '{self.name}' has no 'search' endpoint configured")
        data = self.call("search", {"query": query})
        items = dig(data, spec.get("results"))
        if items is None:
            return []
        if isinstance(items, dict):
            items = [items]
        fields = spec.get("fields") or {}
        options: list[TicketOption] = []
        seen: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            option = self.map_option(item, query, fields)
            # APIs often return one row per train/trip *and class* with the same id.
            count = seen.get(option.option_id, 0)
            seen[option.option_id] = count + 1
            if count:
                option.option_id = f"{option.option_id}:{option.travel_class or count}"
            options.append(option)
        return options

    def check_availability(self, option: TicketOption) -> TicketOption:
        if not self.has_endpoint("availability"):
            return option
        spec = self.endpoints["availability"]
        data = dig(self.call("availability", {"option": option}), spec.get("results"))
        fields = spec.get("fields") or {}
        if not isinstance(data, dict):
            return option
        update: dict[str, Any] = {}
        if "availability" in fields:
            raw = dig(data, fields["availability"])
            status, count = parse_availability(raw)
            update.update(
                availability=status,
                raw_availability=None if raw is None else str(raw),
                seats_available=count if status is AvailabilityStatus.AVAILABLE else None,
                waitlist_position=count if status is AvailabilityStatus.WAITLIST else None,
            )
        if "price" in fields and dig(data, fields["price"]) not in (None, ""):
            update["price"] = float(dig(data, fields["price"]))
        return option.model_copy(update=update)

    def _map_booking_status(self, raw: Any, default: BookingStatus) -> BookingStatus:
        mapping = {str(k).upper(): v for k, v in (self.settings.get("booking_status_map") or {}).items()}
        if raw is None:
            return default
        text = str(raw).upper()
        if text in mapping:
            return BookingStatus(mapping[text])
        try:
            return BookingStatus(text)
        except ValueError:
            status, _ = parse_availability(text)
            return {
                AvailabilityStatus.AVAILABLE: BookingStatus.CONFIRMED,
                AvailabilityStatus.RAC: BookingStatus.CONFIRMED,
                AvailabilityStatus.WAITLIST: BookingStatus.WAITLISTED,
                AvailabilityStatus.REGRET: BookingStatus.FAILED,
            }.get(status, default)

    def book(self, option: TicketOption, passengers: list[Passenger], contact: Contact) -> Booking:
        if not self.has_endpoint("book"):
            raise BookingNotSupported(f"Provider '{self.name}' has no 'book' endpoint configured")
        spec = self.endpoints["book"]
        data = self.call("book", {"option": option, "passengers": passengers, "contact": contact})
        fields = spec.get("fields") or {}
        return Booking(
            booking_id=uuid.uuid4().hex[:12],
            provider=self.name,
            option=option,
            passengers=passengers,
            status=self._map_booking_status(dig(data, fields.get("status")), BookingStatus.CONFIRMED),
            reference=(str(dig(data, fields["reference"])) if dig(data, fields.get("reference")) is not None else None),
            handoff_url=dig(data, fields.get("payment_url")) if "payment_url" in fields else None,
            provider_data=data if isinstance(data, dict) else {"response": data},
        )

    def booking_status(self, booking: Booking) -> Booking:
        if not self.has_endpoint("status") or not booking.reference:
            return booking
        spec = self.endpoints["status"]
        data = self.call("status", {"booking": booking, "reference": booking.reference})
        fields = spec.get("fields") or {}
        update: dict[str, Any] = {"provider_data": {**booking.provider_data, "last_status": data}}
        if "status" in fields:
            update["status"] = self._map_booking_status(dig(data, fields["status"]), booking.status)
        return booking.model_copy(update=update)

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info["can_book"] = self.has_endpoint("book")
        info["endpoints"] = sorted(self.endpoints)
        return info

