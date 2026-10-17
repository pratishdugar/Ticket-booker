"""Match ticket options against traveller preferences and rank them."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from pydantic import BaseModel, Field

from .models import AvailabilityStatus, TicketOption


class Preferences(BaseModel):
    """What makes a ticket acceptable (hard filters) and good (soft scoring)."""

    earliest_departure: time | None = None
    latest_departure: time | None = None
    latest_arrival: time | None = None
    classes: list[str] = Field(default_factory=list, description="Allowed classes, most preferred first")
    max_price: float | None = Field(default=None, description="Per passenger")
    max_duration_minutes: int | None = None
    accept_rac: bool = True
    accept_waitlist: bool = True
    max_waitlist: int | None = Field(default=None, description="Reject waitlist positions above this")
    preferred: list[str] = Field(default_factory=list, description="Substrings of title/number to boost, e.g. 'Rajdhani'")
    avoid: list[str] = Field(default_factory=list, description="Substrings of title/number to reject")
    min_score: float = 0.0

    def merged(self, overrides: dict[str, Any] | None) -> Preferences:
        if not overrides:
            return self
        data = self.model_dump()
        data.update({k: v for k, v in overrides.items() if v is not None})
        return Preferences.model_validate(data)


@dataclass
class MatchResult:
    option: TicketOption
    score: float
    accepted: bool
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "accepted": self.accepted,
            "reasons": self.reasons,
            **self.option.summary(),
        }


_AVAILABILITY_SCORE = {
    AvailabilityStatus.AVAILABLE: 40.0,
    AvailabilityStatus.RAC: 25.0,
    AvailabilityStatus.UNKNOWN: 10.0,
    AvailabilityStatus.REGRET: 0.0,
}


def _contains_any(option: TicketOption, needles: Iterable[str]) -> str | None:
    haystack = " ".join(
        str(x) for x in (option.title, option.attributes.get("number"), option.attributes.get("operator")) if x
    ).lower()
    for needle in needles:
        if needle and needle.lower() in haystack:
            return needle
    return None


def evaluate(
    option: TicketOption,
    prefs: Preferences,
    passengers: int = 1,
    *,
    cheapest: float | None = None,
    fastest: int | None = None,
) -> MatchResult:
    reasons: list[str] = []
    rejects: list[str] = []

    # --- hard filters -------------------------------------------------------
    dep = option.departure.time() if option.departure else None
    arr = option.arrival.time() if option.arrival else None
    if prefs.earliest_departure and dep and dep < prefs.earliest_departure:
        rejects.append(f"departs {dep:%H:%M} before {prefs.earliest_departure:%H:%M}")
    if prefs.latest_departure and dep and dep > prefs.latest_departure:
        rejects.append(f"departs {dep:%H:%M} after {prefs.latest_departure:%H:%M}")
    if prefs.latest_arrival and arr and arr > prefs.latest_arrival:
        rejects.append(f"arrives {arr:%H:%M} after {prefs.latest_arrival:%H:%M}")

    classes = [c.upper() for c in prefs.classes]
    if classes and option.travel_class and option.travel_class.upper() not in classes:
        rejects.append(f"class {option.travel_class} not in {classes}")

    if prefs.max_price is not None and option.price is not None and option.price > prefs.max_price:
        rejects.append(f"price {option.price:g} above {prefs.max_price:g}")

    duration = option.duration_minutes
    if prefs.max_duration_minutes and duration and duration > prefs.max_duration_minutes:
        rejects.append(f"duration {duration}min above {prefs.max_duration_minutes}min")

    avoided = _contains_any(option, prefs.avoid)
    if avoided:
        rejects.append(f"matches avoid-list '{avoided}'")

    status = option.availability
    if status is AvailabilityStatus.AVAILABLE and option.seats_available is not None and option.seats_available < passengers:
        # Partially available: some passengers would land on RAC/WL.
        status = AvailabilityStatus.WAITLIST
        reasons.append(f"only {option.seats_available} seats for {passengers} passengers")
    if status is AvailabilityStatus.REGRET:
        rejects.append("not bookable (regret/sold out)")
    elif status is AvailabilityStatus.RAC and not prefs.accept_rac:
        rejects.append("RAC not accepted")
    elif status is AvailabilityStatus.WAITLIST:
        if not prefs.accept_waitlist:
            rejects.append("waitlist not accepted")
        elif (
            prefs.max_waitlist is not None
            and option.waitlist_position is not None
            and option.waitlist_position > prefs.max_waitlist
        ):
            rejects.append(f"waitlist {option.waitlist_position} above {prefs.max_waitlist}")

    # --- soft score (0-100) -------------------------------------------------
    if status is AvailabilityStatus.WAITLIST:
        wl = option.waitlist_position or 0
        score = max(2.0, 20.0 - wl)  # WL1 ~ 19 points, WL18+ ~ 2 points
    else:
        score = _AVAILABILITY_SCORE[status]
    reasons.append(f"availability {option.raw_availability or status.value}")

    if classes and option.travel_class and option.travel_class.upper() in classes:
        rank = classes.index(option.travel_class.upper())
        score += 20.0 * (1 - rank / max(len(classes), 1))
        if rank == 0:
            reasons.append(f"preferred class {option.travel_class}")
    else:
        score += 10.0

    if option.price is not None and cheapest:
        score += 20.0 * min(1.0, cheapest / option.price)
        if option.price == cheapest:
            reasons.append("cheapest")
    else:
        score += 10.0

    if duration and fastest:
        score += 10.0 * min(1.0, fastest / duration)
        if duration == fastest:
            reasons.append("fastest")
    else:
        score += 5.0

    preferred = _contains_any(option, prefs.preferred)
    if preferred:
        score += 10.0
        reasons.append(f"preferred '{preferred}'")

    if score < prefs.min_score:
        rejects.append(f"score {score:.0f} below minimum {prefs.min_score:g}")

    return MatchResult(option=option, score=score, accepted=not rejects, reasons=rejects + reasons)


def match_options(
    options: Iterable[TicketOption], prefs: Preferences, passengers: int = 1
) -> list[MatchResult]:
    """Evaluate every option; accepted ones first, each group by descending score."""
    options = list(options)
    prices = [o.price for o in options if o.price]
    durations = [o.duration_minutes for o in options if o.duration_minutes]
    cheapest = min(prices) if prices else None
    fastest = min(durations) if durations else None
    results = [evaluate(o, prefs, passengers, cheapest=cheapest, fastest=fastest) for o in options]
    results.sort(key=lambda r: (not r.accepted, -r.score))
    return results
