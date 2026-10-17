from datetime import datetime, time

from ticketbooker.matching import Preferences, match_options
from ticketbooker.models import AvailabilityStatus, Category, TicketOption


def option(oid, *, dep="10:00", arr="20:00", cls="3A", price=1000.0, status=AvailabilityStatus.AVAILABLE, wl=None, seats=50, title="Express"):
    day = datetime(2026, 11, 2)
    return TicketOption(
        option_id=oid,
        provider="t",
        category=Category.TRAIN,
        title=title,
        departure=datetime.combine(day, time.fromisoformat(dep)),
        arrival=datetime.combine(day, time.fromisoformat(arr)),
        travel_class=cls,
        price=price,
        availability=status,
        waitlist_position=wl,
        seats_available=seats if status is AvailabilityStatus.AVAILABLE else None,
    )


def test_hard_filters_reject_with_reasons():
    prefs = Preferences(earliest_departure=time(8), classes=["3A"], max_price=1500, max_waitlist=10, avoid=["Garib"])
    results = {r.option.option_id: r for r in match_options([
        option("ok"),
        option("early", dep="06:00"),
        option("class", cls="SL"),
        option("pricey", price=2000),
        option("deep-wl", status=AvailabilityStatus.WAITLIST, wl=30),
        option("avoid", title="Garib Rath"),
        option("regret", status=AvailabilityStatus.REGRET),
    ], prefs)}
    assert results["ok"].accepted
    for oid, fragment in [("early", "before 08:00"), ("class", "not in"), ("pricey", "above 1500"),
                          ("deep-wl", "waitlist 30"), ("avoid", "avoid-list"), ("regret", "not bookable")]:
        assert not results[oid].accepted
        assert any(fragment in r for r in results[oid].reasons), (oid, results[oid].reasons)


def test_ranking_prefers_confirmed_then_preferred_class():
    prefs = Preferences(classes=["2A", "3A"])
    results = match_options([
        option("wl-2a", cls="2A", status=AvailabilityStatus.WAITLIST, wl=3),
        option("avl-3a", cls="3A"),
        option("avl-2a", cls="2A"),
    ], prefs)
    assert [r.option.option_id for r in results] == ["avl-2a", "avl-3a", "wl-2a"]
    assert all(r.accepted for r in results)


def test_not_enough_seats_is_treated_as_waitlist():
    prefs = Preferences(accept_waitlist=False)
    [result] = match_options([option("few", seats=1)], prefs, passengers=3)
    assert not result.accepted
    assert "only 1 seats for 3 passengers" in result.reasons


def test_merged_overrides():
    prefs = Preferences(max_price=1000).merged({"max_price": 2000, "classes": ["SL"], "latest_departure": "18:30"})
    assert prefs.max_price == 2000 and prefs.classes == ["SL"] and prefs.latest_departure == time(18, 30)
