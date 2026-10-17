from datetime import date, timedelta

import pytest

from ticketbooker.config import AppConfig
from ticketbooker.service import TicketService
from ticketbooker.store import BookingStore


@pytest.fixture
def journey_date() -> date:
    return date.today() + timedelta(days=20)


@pytest.fixture
def config(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "travellers": [
                {"name": "Asha Sharma", "aliases": ["me"], "age": 32, "gender": "F", "berth_preference": "LB"},
                {"name": "Rohan Sharma", "age": 35, "gender": "M"},
            ],
            "contact": {"email": "asha@example.com"},
            "category_preferences": {"train": {"classes": ["3A", "2A"]}},
            "providers": [{"name": "demo", "type": "mock"}],
            "storage_path": str(tmp_path / "bookings.json"),
        }
    )


@pytest.fixture
def service(config) -> TicketService:
    return TicketService(config, store=BookingStore(config.storage_path))
