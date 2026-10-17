import json
from types import SimpleNamespace

import pytest

from ticketbooker.agent import BookingAgent
from ticketbooker.config import load_config
from ticketbooker.models import BookingStatus, Category, SearchQuery


def test_search_match_book_flow(service, journey_date):
    result = service.search(SearchQuery(category=Category.TRAIN, origin="NDLS", destination="BCT", date=journey_date))
    assert result.options and not result.errors
    ranked = service.match()
    best = next(r for r in ranked if r.accepted)
    assert best.option.travel_class in {"3A", "2A"}  # category preference applied

    with pytest.raises(PermissionError):
        service.book(best.option.option_id, ["me"], confirm=lambda o, p: False)
    booking = service.book(best.option.option_id, ["me", "rohan sharma"], confirm=lambda o, p: True)
    assert [p.name for p in booking.passengers] == ["Asha Sharma", "Rohan Sharma"]
    assert service.store.get(booking.booking_id).reference == booking.reference
    assert service.record_reference(booking.booking_id, "1234567890").reference == "1234567890"

    with pytest.raises(KeyError, match="Unknown traveller"):
        service.book(best.option.option_id, ["nobody"], confirm=lambda o, p: True)


def test_load_config_interpolates_env_and_skips_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TB_EMAIL", "x@example.com")
    path = tmp_path / "c.yaml"
    path.write_text(
        "contact: {email: '${TB_EMAIL}', phone: '${TB_PHONE:-none}'}\n"
        "providers:\n  - {name: a, type: mock}\n  - {name: b, type: http, enabled: false}\n"
    )
    cfg = load_config(path)
    assert cfg.contact.email == "x@example.com" and cfg.contact.phone == "none"
    assert [p["name"] for p in cfg.providers] == ["a"]


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic()."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.requests.append({**params, "messages": list(params["messages"])})
        return self.responses.pop(0)


def test_agent_runs_tools_and_asks_before_booking(service, journey_date):
    service.search(SearchQuery(category=Category.TRAIN, origin="NDLS", destination="BCT", date=journey_date))
    option_id = next(r for r in service.match() if r.accepted).option.option_id
    service.last_search = None
    client = FakeClient([
        SimpleNamespace(stop_reason="tool_use", content=[
            block("text", text="Searching."),
            block("tool_use", id="t1", name="search_tickets",
                  input={"category": "train", "origin": "NDLS", "destination": "BCT", "date": journey_date.isoformat()}),
            block("tool_use", id="t2", name="match_tickets", input={"limit": 3}),
        ]),
        SimpleNamespace(stop_reason="tool_use", content=[
            block("tool_use", id="t3", name="book_ticket", input={"option_id": option_id, "travellers": ["me"]}),
            block("tool_use", id="t4", name="booking_status", input={}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[block("text", text="Booked!")]),
    ])
    confirmations = []
    agent = BookingAgent(service, confirm=lambda o, p: confirmations.append(o.option_id) or True, client=client)

    assert agent.ask("Book me NDLS to BCT") == "Booked!"
    assert confirmations == [option_id]
    first = client.requests[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert "Today is" in first["messages"][0]["content"]

    # Both tool results from one turn go back in a single user message.
    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"] and not any(r["is_error"] for r in results)
    results = client.requests[2]["messages"][-1]["content"]
    assert results[0]["is_error"] is False
    assert json.loads(results[0]["content"])["status"] in {BookingStatus.CONFIRMED, BookingStatus.WAITLISTED}
    assert results[1]["is_error"] is True and "Invalid input" in results[1]["content"]
    assert len(service.store.all()) == 1


def test_agent_answers_dangling_tool_use_on_max_tokens(service):
    client = FakeClient([
        SimpleNamespace(stop_reason="max_tokens", content=[block("tool_use", id="t1", name="list_setup", input={})]),
    ])
    agent = BookingAgent(service, client=client)
    assert "cut off" in agent.ask("hi")
    assert agent.messages[-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "Not executed."}
