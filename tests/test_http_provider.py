import json

import pytest

from ticketbooker.models import AvailabilityStatus, BookingStatus, Category, Contact, Passenger, SearchQuery
from ticketbooker.providers import HttpApiProvider, IrctcProvider, ProviderError
from ticketbooker.providers.http_api import dig, render


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None})
        for prefix, response in self.responses.items():
            if url.startswith(prefix):
                return 200, json.dumps(response)
        return 404, "not found"


BUS_SETTINGS = {
    "categories": ["bus"],
    "base_url": "https://bus.test",
    "headers": {"Authorization": "Bearer k"},
    "endpoints": {
        "search": {
            "path": "/search",
            "params": {"from": "{query.origin}", "to": "{query.destination}", "date": "{query.date:%Y-%m-%d}", "class": "{query.travel_class}"},
            "results": "data.trips",
            "fields": {"option_id": "id", "title": "operator.name", "departure": "dep", "arrival": "arr",
                       "price": "fare.total", "availability": "seats"},
        },
        "book": {
            "method": "POST",
            "path": "/book",
            "body": {"trip": "{option.attributes[native_id]}", "passengers": "{passengers}", "email": "{contact.email}", "n": "{query_count}"},
            "fields": {"reference": "pnr", "status": "state"},
        },
    },
}


def test_render_and_dig():
    ctx = {"a": {"b": [1, 2]}, "query_count": 3}
    assert render({"x": "{a[b]}", "y": "n={query_count}", "z": ["{query_count}"]}, ctx) == {"x": [1, 2], "y": "n=3", "z": [3]}
    assert dig({"a": [{"b": 5}]}, "a.0.b") == 5
    assert dig({"a": []}, "a.3.b") is None
    with pytest.raises(ProviderError):
        render("{missing}", {})


def test_search_maps_fields_and_drops_empty_params(journey_date):
    transport = FakeTransport({"https://bus.test/search": {"data": {"trips": [
        {"id": "T1", "operator": {"name": "Zing"}, "dep": "21:30", "arr": "06:15", "fare": {"total": "899"}, "seats": 12},
        {"id": "T2", "operator": {"name": "VRL"}, "dep": f"{journey_date}T22:00:00", "arr": None, "fare": {"total": 650}, "seats": 0},
    ]}}})
    provider = HttpApiProvider("bus", BUS_SETTINGS, transport)
    options = provider.search(SearchQuery(category=Category.BUS, origin="Pune", destination="Goa", date=journey_date))

    url = transport.calls[0]["url"]
    assert f"date={journey_date}" in url and "from=Pune" in url and "class=" not in url
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer k"
    first, second = options
    assert first.option_id == "bus:T1" and first.title == "Zing" and first.price == 899.0
    assert first.availability is AvailabilityStatus.AVAILABLE and first.seats_available == 12
    assert first.arrival.date() > first.departure.date()  # overnight trip rolled to next day
    assert second.availability is AvailabilityStatus.REGRET


def test_book_sends_json_body(journey_date):
    transport = FakeTransport({
        "https://bus.test/search": {"data": {"trips": [{"id": "T1", "operator": {"name": "Zing"}, "seats": 5}]}},
        "https://bus.test/book": {"pnr": "PNR42", "state": "confirmed"},
    })
    settings = json.loads(json.dumps(BUS_SETTINGS))
    settings["endpoints"]["book"]["body"].pop("n")
    provider = HttpApiProvider("bus", settings, transport)
    [opt] = provider.search(SearchQuery(category=Category.BUS, origin="A", destination="B", date=journey_date))
    booking = provider.book(opt, [Passenger(name="Asha", age=32, gender="F")], Contact(email="a@x.in"))

    body = transport.calls[-1]["body"]
    assert body == {"trip": "T1", "passengers": [{"name": "Asha", "age": 32, "gender": "F", "extra": {}}], "email": "a@x.in"}
    assert booking.reference == "PNR42" and booking.status is BookingStatus.CONFIRMED


def test_http_errors_become_provider_errors(journey_date):
    provider = HttpApiProvider("bus", BUS_SETTINGS, lambda *a: (503, "down"))
    with pytest.raises(ProviderError, match="HTTP 503"):
        provider.search(SearchQuery(category=Category.BUS, origin="A", destination="B", date=journey_date))


IRCTC_SETTINGS = {
    "base_url": "https://rail.test",
    "endpoints": {
        "search": {
            "path": "/between",
            "params": {"from": "{query.origin}", "to": "{query.destination}", "quota": "{query.quota}"},
            "results": "data",
            "fields": {"option_id": "id", "number": "no", "title": "name", "travel_class": "cls",
                       "departure": "dep", "arrival": "arr", "price": "fare", "availability": "avl"},
        }
    },
}


def test_irctc_validates_query(journey_date):
    provider = IrctcProvider("irctc", IRCTC_SETTINGS, FakeTransport({}))
    with pytest.raises(ProviderError, match="station codes"):
        provider.search(SearchQuery(category=Category.TRAIN, origin="New Delhi", destination="BCT", date=journey_date))
    with pytest.raises(ProviderError, match="quota"):
        provider.search(SearchQuery(category=Category.TRAIN, origin="NDLS", destination="BCT", date=journey_date, quota="XX"))


def test_irctc_without_search_endpoint_explains(journey_date):
    provider = IrctcProvider("irctc", {})
    with pytest.raises(ProviderError, match="no public API"):
        provider.search(SearchQuery(category=Category.TRAIN, origin="NDLS", destination="BCT", date=journey_date))


def test_irctc_handoff_booking(journey_date):
    rows = [
        {"id": "12951", "no": "12951", "name": "Mumbai Rajdhani", "cls": "3A", "dep": "16:55", "arr": "08:35", "fare": 3100, "avl": "AVAILABLE-0010"},
        {"id": "12951", "no": "12951", "name": "Mumbai Rajdhani", "cls": "2A", "dep": "16:55", "arr": "08:35", "fare": 4300, "avl": "GNWL4/WL2"},
    ]
    transport = FakeTransport({"https://rail.test/between": {"data": rows}})
    provider = IrctcProvider("irctc", IRCTC_SETTINGS, transport)
    options = provider.search(SearchQuery(category=Category.TRAIN, origin="ndls", destination="bct", date=journey_date))
    assert "from=NDLS" in transport.calls[0]["url"] and "quota=GN" in transport.calls[0]["url"]
    assert [o.option_id for o in options] == ["irctc:12951", "irctc:12951:2A"]
    assert options[1].waitlist_position == 2

    passengers = [Passenger(name="Asha", age=32, gender="f", berth_preference="lb")]
    booking = provider.book(options[0], passengers, Contact())
    assert booking.status is BookingStatus.PENDING_USER_ACTION
    assert booking.handoff_url.startswith("https://www.irctc.co.in")
    sheet = booking.provider_data["booking_sheet"]
    assert sheet["train_number"] == "12951" and sheet["quota"] == "GN"
    assert sheet["passengers"][0] == {"name": "Asha", "age": 32, "gender": "F", "berth_preference": "LB"}

    with pytest.raises(ProviderError, match="at most 6"):
        provider.book(options[0], passengers * 7, Contact())
