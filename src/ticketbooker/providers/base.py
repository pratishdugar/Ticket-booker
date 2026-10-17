"""Provider interface. A provider is anything that can search (and optionally book) tickets."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Booking, Category, Contact, Passenger, SearchQuery, TicketOption


class ProviderError(Exception):
    """A provider failed in a way the user or agent should hear about."""


class BookingNotSupported(ProviderError):
    pass


class Provider(ABC):
    #: Registry key used in config (``type: <provider_type>``).
    provider_type: str = ""

    def __init__(self, name: str, settings: dict[str, Any] | None = None):
        self.name = name
        self.settings = settings or {}
        categories = self.settings.get("categories") or self.settings.get("category") or self.default_categories()
        if isinstance(categories, str):
            categories = [categories]
        self.categories = {Category(c) for c in categories}

    def default_categories(self) -> list[str]:
        return [c.value for c in Category]

    def supports(self, category: Category) -> bool:
        return category in self.categories

    @abstractmethod
    def search(self, query: SearchQuery) -> list[TicketOption]:
        """Return bookable options for the query."""

    def check_availability(self, option: TicketOption) -> TicketOption:
        """Refresh availability/price for one option. Default: return as-is."""
        return option

    def book(self, option: TicketOption, passengers: list[Passenger], contact: Contact) -> Booking:
        raise BookingNotSupported(f"Provider '{self.name}' does not support booking")

    def booking_status(self, booking: Booking) -> Booking:
        """Refresh a booking (e.g. PNR status). Default: return as-is."""
        return booking

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.provider_type,
            "categories": sorted(c.value for c in self.categories),
            "can_book": type(self).book is not Provider.book,
        }
