# TicketBooker

A generic ticket-booking assistant you set up with one YAML file. Plug in any ticket source (Indian Railways/IRCTC, buses, flights, events, movies), save your travellers and preferences, then either:

- **chat** with a Claude-powered agent ("get me and Rohan from Delhi to Mumbai on 2 Nov, 3AC, evening departure"). It searches, ranks the results against your preferences, and books only after you confirm, or
- use the plain **CLI** to search, rank and **watch** for matching tickets (for example, until a waitlisted train turns available).

```
                ┌──────────── ticketbooker.yaml ────────────┐
                │ travellers · preferences · providers       │
                └───────────────────────────────────────────┘
 you ─▶ chat (Claude agent) ─┐
 you ─▶ search / watch CLI ──┼─▶ TicketService ─▶ providers: mock | http | irctc | <your own>
                             │        │
                             │        ├─▶ matcher (hard filters + ranking)
                             │        └─▶ booking store (JSON)
                             └─ every booking needs your y/N confirmation
```

## Quick start

```bash
pip install -e ".[dev]"
ticketbooker init                      # writes ./ticketbooker.yaml (edit it)
ticketbooker search --from NDLS --to BCT --date 2026-11-02          # offline demo data
export ANTHROPIC_API_KEY=sk-ant-...    # or `ant auth login`
ticketbooker chat
```

Example chat:

```
you> Need 2 tickets Delhi to Mumbai on 2 Nov for me and Rohan, 3AC or 2AC, leaving after 4pm
  · search_tickets
  · match_tickets
agent> Top matches (NDLS → BCT, 2 Nov):
  1. 12951 Rajdhani Express, 3A, 18:15 → 08:25, ₹1,837, AVAILABLE-0029 (fastest)
  2. 22209 Duronto Express, 3A, 18:15 → 11:15, ₹2,066, AVAILABLE-0034
  ...
you> book option 1
=== Booking confirmation ===
12951 Rajdhani Express | 3A | 02 Nov 18:15 -> 03 Nov 08:25
Book this? [y/N]
```

## Commands

| Command | What it does |
|---|---|
| `ticketbooker chat [-m "..."] [-v]` | Talk to the agent (single message with `-m`, tool calls shown with `-v`). |
| `ticketbooker search --category train --from NDLS --to BCT --date YYYY-MM-DD [--class 3A] [--quota TQ] [--passengers 2]` | Search all providers that serve the category and rank the results. Options marked `x` fail your hard limits. |
| `ticketbooker watch ... --interval 300 [--confirmed-only] [--max-waitlist 5] [--once]` | Re-check every N seconds (minimum 60) and alert when a new matching ticket appears. It never books by itself. |
| `ticketbooker bookings` / `status <id>` | List bookings / refresh a booking's status, e.g. its PNR status. |
| `ticketbooker record-pnr <id> <PNR>` | Attach the PNR after you finish a hand-off booking on IRCTC. |
| `ticketbooker providers` | Show the configured providers and what each can do. |

Flags shared by `search` and `watch` for one-off preference changes: `--max-price`, `--classes 3A,2A`, `--after HH:MM`, `--before HH:MM`, `--max-waitlist N`, `--confirmed-only`, `--provider NAME`.

## Configuration

See [`src/ticketbooker/config.example.yaml`](src/ticketbooker/config.example.yaml) (`ticketbooker init` copies it). `${VAR}` / `${VAR:-default}` are read from the environment, so API keys stay out of the file. Config lookup order: `--config`, `$TICKETBOOKER_CONFIG`, `./ticketbooker.yaml`, then `~/.config/ticketbooker/config.yaml`.

- **`travellers`**: name, age, gender, berth/meal preference and aliases ("me").
- **`preferences`** and **`category_preferences`** define what counts as a match:
  - Hard limits reject an option: departure/arrival windows, allowed `classes`, `max_price`, `max_duration_minutes`, `accept_rac`, `accept_waitlist`, `max_waitlist`, `avoid`.
  - Soft factors only affect ranking: availability (confirmed > RAC > low WL), class order, price, duration, and the `preferred` trains or operators.
- **`agent`**: Claude model (default `claude-opus-5-5`), `effort`, `require_confirmation` (on by default) and `max_steps`.
- **`providers`**: a list of `{name, type, ...settings}`, each with `enabled: false` to switch it off.

### Provider types

| type | Use it for |
|---|---|
| `mock` | Offline, deterministic demo data for every category. Good for trying the agent. |
| `http` | Any JSON API. You describe `search`, `availability`, `book` and `status` endpoints: request templates (`{query.origin}`, `{query.date:%Y-%m-%d}`, `{option.attributes[native_id]}`, `{passengers}`) and dotted-path field mappings for the response. No code needed. |
| `irctc` | Indian Railways. Works like `http` but adds station-code, class, quota, 60-day window and passenger-limit checks (6 per PNR, 4 for Tatkal), and IRCTC-style availability parsing (`AVAILABLE-0045`, `RAC 12`, `GNWL23/WL10`, `REGRET`). |

Custom provider in code:

```python
from ticketbooker.providers import Provider, register_provider

@register_provider
class MyCinemaProvider(Provider):
    provider_type = "my_cinema"
    def search(self, query): ...
    def book(self, option, passengers, contact): ...
```

## IRCTC: what is and isn't automated

IRCTC has no public booking API, and its terms of use forbid automated software on irctc.co.in (bots also risk getting the account blocked). So TicketBooker does **not** script the IRCTC website, log in for you or solve captchas. It:

1. **Searches and checks availability and PNR status** through a rail-data API you subscribe to. Map that API's fields in the `irctc` provider config.
2. **Matches and ranks** trains against your preferences, and `watch` alerts you when a waitlist clears.
3. **Books** in one of two modes:
   - `booking_mode: handoff` (default): the agent picks the train, validates the passengers and gives you a ready **booking sheet** plus the IRCTC link. You log in, complete captcha/OTP and pay yourself, then run `record-pnr`.
   - `booking_mode: api`: if you have authorised booking-API access (for example as an IRCTC-authorised agent or B2B partner), add an `endpoints.book` and the agent books through it after you confirm.

## How the agent works

`src/ticketbooker/agent.py` runs a tool-use loop on the Claude API (Anthropic Python SDK). It has these tools: `list_setup`, `search_tickets`, `match_tickets`, `check_availability`, `book_ticket`, `booking_status` and `list_bookings`. Details:

- The system prompt and tools are fixed, so prompt caching applies, and the conversation history is append-only.
- It uses adaptive thinking, with effort set in config.
- If the model declines a request, a server-side fallback model takes over (`fallbacks: "default"`). Turn this off with `agent.server_side_fallback: false`, for example on a platform without server-side fallbacks.
- `book_ticket` always asks for your y/N confirmation in the terminal before any provider booking call. A decline is reported back to the model, which is told not to retry.
- Tool inputs are validated with pydantic, and errors are returned to the model as `is_error` tool results.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```
