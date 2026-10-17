"""Claude-powered booking agent: plans searches, matches tickets and books with your confirmation."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from .models import SearchQuery
from .providers import ProviderError
from .service import ConfirmFn, TicketService

SYSTEM_PROMPT = """\
You are TicketBooker, an assistant that finds, compares and books tickets (trains incl. \
Indian Railways/IRCTC, buses, flights, events, movies) through the providers configured by \
the user.

How to work:
- Use the tools for every fact about trains, prices, availability and bookings. Never invent \
availability, fares, PNRs or train numbers.
- For a trip request: search, then call match_tickets so options are ranked against the \
user's saved preferences, then present the best few options briefly (title, times, class, \
price, availability, why it ranks well). Mention notable rejected options only if nothing matched.
- For Indian Railways use station codes (NDLS, BCT, SBC, MAS, HWH ...) and IRCTC classes \
(1A, 2A, 3A, 3E, SL, CC, EC, 2S) and quotas (GN general, TQ Tatkal, LD ladies, SS senior).
- If something essential is missing (date, route, which travellers), ask instead of guessing.
- Only call book_ticket after the user has clearly chosen an option and the travellers. The \
tool also asks the user to confirm on their side; if they decline, do not retry.
- When a booking comes back PENDING_USER_ACTION (e.g. IRCTC hand-off), give the user the link \
and the prepared booking sheet so they can finish login, captcha and payment themselves.
- Keep answers short and scannable."""


class SearchInput(BaseModel):
    category: Literal["train", "bus", "flight", "event", "movie", "other"]
    date: date
    origin: str | None = None
    destination: str | None = None
    travel_class: str | None = None
    quota: str | None = None
    passengers: int = Field(default=1, ge=1, le=9)
    providers: list[str] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MatchInput(BaseModel):
    option_ids: list[str] | None = None
    preferences: dict[str, Any] | None = None
    limit: int = Field(default=8, ge=1, le=50)


class OptionInput(BaseModel):
    option_id: str


class BookInput(BaseModel):
    option_id: str
    travellers: list[str] = Field(min_length=1)


class BookingIdInput(BaseModel):
    booking_id: str


def _schema(model: type[BaseModel], description: str, name: str) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {"name": name, "description": description, "input_schema": schema}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_setup",
        "description": "List configured providers (with the categories they serve and whether they can book), "
        "saved travellers and the default ticket preferences.",
        "input_schema": {"type": "object", "properties": {}},
    },
    _schema(
        SearchInput,
        "Search tickets across providers. Returns options with option_id values used by the other tools. "
        "date is YYYY-MM-DD. For trains, origin/destination are station codes.",
        "search_tickets",
    ),
    _schema(
        MatchInput,
        "Rank options against the user's saved preferences (time windows, classes, budget, waitlist "
        "tolerance, preferred/avoided trains). Defaults to the latest search. 'preferences' overrides saved "
        "ones for this call, using keys: earliest_departure, latest_departure, latest_arrival (HH:MM), "
        "classes (list), max_price, max_duration_minutes, accept_rac, accept_waitlist, max_waitlist, "
        "preferred (list), avoid (list).",
        "match_tickets",
    ),
    _schema(OptionInput, "Refresh live availability and fare for one option.", "check_availability"),
    _schema(
        BookInput,
        "Book an option for saved travellers (names or aliases from list_setup). The user is asked to "
        "confirm before anything is booked.",
        "book_ticket",
    ),
    _schema(BookingIdInput, "Get the latest status (e.g. PNR status) of a booking.", "booking_status"),
    {
        "name": "list_bookings",
        "description": "List bookings made so far, newest first.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class BookingAgent:
    def __init__(
        self,
        service: TicketService,
        confirm: ConfirmFn | None = None,
        client: Any | None = None,
        on_event: Callable[[str, Any], None] | None = None,
    ):
        self.service = service
        self.settings = service.config.agent
        self.confirm = confirm
        self.client = client or anthropic.Anthropic()
        self.on_event = on_event or (lambda kind, payload: None)
        self.messages: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- tools
    def _run_tool(self, name: str, raw_input: dict[str, Any]) -> Any:
        svc = self.service
        if name == "list_setup":
            cfg = svc.config
            return {
                "providers": [p.describe() for p in svc.providers.values()],
                "travellers": [t.model_dump(exclude_none=True, exclude={"id_number"}) for t in cfg.travellers],
                "default_preferences": cfg.preferences.model_dump(mode="json", exclude_none=True),
                "category_preferences": {k.value: v for k, v in cfg.category_preferences.items()},
            }
        if name == "search_tickets":
            args = SearchInput.model_validate(raw_input)
            query = SearchQuery(**args.model_dump(exclude={"providers"}))
            result = svc.search(query, args.providers)
            return {
                "count": len(result.options),
                "options": [o.summary() for o in result.options[:60]],
                "truncated": len(result.options) > 60,
                "provider_errors": result.errors,
            }
        if name == "match_tickets":
            args = MatchInput.model_validate(raw_input)
            results = svc.match(args.option_ids, args.preferences)
            accepted = [r for r in results if r.accepted]
            return {
                "accepted": len(accepted),
                "rejected": len(results) - len(accepted),
                "top": [r.summary() for r in (accepted or results)[: args.limit]],
            }
        if name == "check_availability":
            return svc.check_availability(OptionInput.model_validate(raw_input).option_id).summary()
        if name == "book_ticket":
            args = BookInput.model_validate(raw_input)
            booking = svc.book(args.option_id, args.travellers, self.confirm)
            return booking.model_dump(mode="json", exclude_none=True)
        if name == "booking_status":
            booking = svc.booking_status(BookingIdInput.model_validate(raw_input).booking_id)
            return booking.model_dump(mode="json", exclude_none=True)
        if name == "list_bookings":
            return [
                {
                    "booking_id": b.booking_id,
                    "status": b.status.value,
                    "reference": b.reference,
                    "title": b.option.title,
                    "departure": b.option.departure.isoformat() if b.option.departure else None,
                }
                for b in svc.store.all()
            ]
        raise KeyError(f"Unknown tool '{name}'")

    def execute_tool(self, name: str, raw_input: Any) -> dict[str, Any]:
        """Run a tool and return the fields of its tool_result block."""
        self.on_event("tool_call", {"name": name, "input": raw_input})
        try:
            if not isinstance(raw_input, dict):
                raise ValueError(f"Tool input must be an object, got {type(raw_input).__name__}")
            output = self._run_tool(name, raw_input)
            content, is_error = json.dumps(output, default=str), False
        except ValidationError as exc:
            content, is_error = f"Invalid input: {exc.errors(include_url=False)}", True
        except PermissionError as exc:
            content, is_error = str(exc), True
        except (ProviderError, KeyError, ValueError) as exc:
            content, is_error = f"{type(exc).__name__}: {exc}", True
        self.on_event("tool_result", {"name": name, "is_error": is_error, "content": content})
        return {"content": content, "is_error": is_error}

    # ------------------------------------------------------------------ loop
    def _create(self) -> Any:
        params: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": self.messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.settings.server_side_fallback:
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"
        return self.client.beta.messages.create(**params)

    def ask(self, user_text: str) -> str:
        """Send one user message, run tools until Claude answers, and return the answer text."""
        if not self.messages:
            user_text = f"(Today is {date.today():%A, %Y-%m-%d}.)\n\n{user_text}"
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(self.settings.max_steps):
            response = self._create()
            # Keep the full content (incl. thinking blocks) so the history stays append-only.
            self.messages.append({"role": "assistant", "content": response.content})
            texts = [b.text for b in response.content if b.type == "text"]
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                # A refusal or max_tokens can cut a tool call off mid-input: never run it, but
                # answer it so the conversation stays valid for the next message.
                if tool_uses:
                    self.messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": "Not executed."}
                        for b in tool_uses
                    ]})
                if response.stop_reason == "refusal":
                    return "\n".join(texts) or "Sorry - I can't help with that request."
                if response.stop_reason == "max_tokens":
                    return "\n".join(texts) + "\n\n[Response was cut off - try a narrower request.]"
                return "\n".join(texts)

            if texts:
                self.on_event("progress", "\n".join(texts))
            results = [
                {"type": "tool_result", "tool_use_id": b.id, **self.execute_tool(b.name, b.input)} for b in tool_uses
            ]
            self.messages.append({"role": "user", "content": results})

        return "Stopped after too many steps - please narrow the request."
