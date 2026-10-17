"""Application service shared by the CLI and the AI agent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig
from .matching import MatchResult, match_options
from .models import Booking, BookingStatus, Passenger, SearchQuery, TicketOption
from .providers import Provider, ProviderError, build_provider
from .store import BookingStore

#: Called before any booking; return True to proceed.
ConfirmFn = Callable[[TicketOption, list[Passenger]], bool]


@dataclass
class SearchResult:
    query: SearchQuery
    options: list[TicketOption]
    errors: dict[str, str] = field(default_factory=dict)


class TicketService:
    def __init__(self, config: AppConfig, providers: list[Provider] | None = None, store: BookingStore | None = None):
        self.config = config
        built = providers if providers is not None else [build_provider(p) for p in config.providers]
        self.providers: dict[str, Provider] = {p.name: p for p in built}
        self.store = store or BookingStore(config.storage_path)
        self._options: dict[str, TicketOption] = {}
        self.last_search: SearchResult | None = None

    # ---------------------------------------------------------------- search
    def providers_for(self, query: SearchQuery, names: list[str] | None = None) -> list[Provider]:
        if names:
            missing = [n for n in names if n not in self.providers]
            if missing:
                raise ProviderError(f"Unknown provider(s) {missing}. Configured: {sorted(self.providers)}")
            return [self.providers[n] for n in names]
        return [p for p in self.providers.values() if p.supports(query.category)]

    def search(self, query: SearchQuery, provider_names: list[str] | None = None) -> SearchResult:
        selected = self.providers_for(query, provider_names)
        if not selected:
            raise ProviderError(f"No configured provider serves '{query.category.value}'")
        options: list[TicketOption] = []
        errors: dict[str, str] = {}
        for provider in selected:
            try:
                found = provider.search(query)
            except ProviderError as exc:
                errors[provider.name] = str(exc)
                continue
            options.extend(found)
        for option in options:
            self._options[option.option_id] = option
        self.last_search = SearchResult(query=query, options=options, errors=errors)
        return self.last_search

    def option(self, option_id: str) -> TicketOption:
        if option_id not in self._options:
            raise KeyError(f"Unknown option_id '{option_id}'. Search first and use an id from the results.")
        return self._options[option_id]

    def match(
        self,
        option_ids: list[str] | None = None,
        overrides: dict[str, Any] | None = None,
        passengers: int | None = None,
    ) -> list[MatchResult]:
        if option_ids:
            options = [self.option(i) for i in option_ids]
        elif self.last_search:
            options = self.last_search.options
        else:
            raise KeyError("Nothing to match yet - search first.")
        if not options:
            return []
        category = options[0].category
        prefs = self.config.preferences_for(category, overrides)
        count = passengers or (self.last_search.query.passengers if self.last_search else 1)
        return match_options(options, prefs, count)

    def check_availability(self, option_id: str) -> TicketOption:
        option = self.option(option_id)
        refreshed = self.providers[option.provider].check_availability(option)
        self._options[option_id] = refreshed
        return refreshed

    # --------------------------------------------------------------- booking
    def resolve_passengers(self, traveller_names: list[str]) -> list[Passenger]:
        travellers = self.config.find_travellers(traveller_names)
        return [Passenger.model_validate(t.model_dump(exclude={"aliases"})) for t in travellers]

    def book(self, option_id: str, traveller_names: list[str], confirm: ConfirmFn | None) -> Booking:
        option = self.option(option_id)
        passengers = self.resolve_passengers(traveller_names)
        if self.config.agent.require_confirmation:
            if confirm is None or not confirm(option, passengers):
                raise PermissionError("The user declined this booking.")
        booking = self.providers[option.provider].book(option, passengers, self.config.contact)
        return self.store.save(booking)

    def booking_status(self, booking_id: str) -> Booking:
        booking = self.store.get(booking_id)
        provider = self.providers.get(booking.provider)
        if provider is None:
            return booking
        return self.store.save(provider.booking_status(booking))

    def record_reference(self, booking_id: str, reference: str) -> Booking:
        """Record the PNR/reference for a booking the user finished by hand."""
        booking = self.store.get(booking_id)
        status = BookingStatus.CONFIRMED if booking.status is BookingStatus.PENDING_USER_ACTION else booking.status
        updated = booking.model_copy(update={"reference": reference, "status": status})
        return self.booking_status(self.store.save(updated).booking_id)
