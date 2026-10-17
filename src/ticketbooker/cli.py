"""Command-line interface: `ticketbooker <command>`."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date, datetime
from importlib import resources
from pathlib import Path

from .config import AppConfig, load_config
from .matching import MatchResult
from .models import Booking, Category, Passenger, SearchQuery, TicketOption
from .providers import ProviderError
from .service import TicketService

MIN_WATCH_INTERVAL = 60  # seconds; be polite to provider APIs


def _fmt_time(value: datetime | None) -> str:
    return value.strftime("%d %b %H:%M") if value else "-"


def print_results(results: list[MatchResult], limit: int) -> None:
    if not results:
        print("No options found.")
        return
    print(f"{'#':>2}  {'score':>5}  {'title':<30} {'class':<6} {'depart':<13} {'arrive':<13} {'price':>8}  availability")
    for i, r in enumerate(results[:limit], 1):
        o = r.option
        mark = " " if r.accepted else "x"
        price = f"{o.price:,.0f}" if o.price is not None else "-"
        print(
            f"{i:>2}{mark} {r.score:>5.0f}  {o.title[:30]:<30} {(o.travel_class or '-')[:6]:<6} "
            f"{_fmt_time(o.departure):<13} {_fmt_time(o.arrival):<13} {price:>8}  {o.raw_availability or o.availability.value}"
        )
        print(f"      id={o.option_id}  {'; '.join(r.reasons)}")
    hidden = len(results) - limit
    if hidden > 0:
        print(f"... {hidden} more")


def print_booking(b: Booking) -> None:
    print(f"Booking {b.booking_id}: {b.status.value}  provider={b.provider}  reference={b.reference or '-'}")
    print(f"  {b.option.title} {b.option.travel_class or ''}  {_fmt_time(b.option.departure)}")
    print(f"  passengers: {', '.join(p.name for p in b.passengers)}")
    if b.handoff_url:
        print(f"  finish at: {b.handoff_url}")
    for note in b.notes:
        print(f"  - {note}")
    sheet = b.provider_data.get("booking_sheet")
    if sheet:
        print("  booking sheet:")
        print("    " + json.dumps(sheet, indent=2).replace("\n", "\n    "))


def confirm_on_terminal(option: TicketOption, passengers: list[Passenger]) -> bool:
    print("\n=== Booking confirmation ===")
    print(f"{option.title} | {option.travel_class or '-'} | {_fmt_time(option.departure)} -> {_fmt_time(option.arrival)}")
    price = f"{option.price:,.0f} {option.currency} per passenger" if option.price is not None else "unknown"
    print(f"Fare: {price} | Availability: {option.raw_availability or option.availability.value}")
    print(f"Provider: {option.provider} | Passengers: {', '.join(f'{p.name} ({p.age}{p.gender})' for p in passengers)}")
    try:
        answer = input("Book this? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _query_from_args(args: argparse.Namespace) -> SearchQuery:
    return SearchQuery(
        category=Category(args.category),
        origin=args.origin,
        destination=args.destination,
        date=date.fromisoformat(args.date),
        travel_class=args.travel_class,
        quota=args.quota,
        passengers=args.passengers,
    )


def _overrides(args: argparse.Namespace) -> dict:
    overrides = {}
    if args.max_price is not None:
        overrides["max_price"] = args.max_price
    if args.classes:
        overrides["classes"] = [c.strip() for c in args.classes.split(",") if c.strip()]
    if args.after:
        overrides["earliest_departure"] = args.after
    if args.before:
        overrides["latest_departure"] = args.before
    if args.max_waitlist is not None:
        overrides["max_waitlist"] = args.max_waitlist
    if args.confirmed_only:
        overrides.update(accept_rac=False, accept_waitlist=False)
    return overrides


# ------------------------------------------------------------------ commands
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    example = resources.files("ticketbooker").joinpath("config.example.yaml")
    with resources.as_file(example) as src:
        shutil.copyfile(src, target)
    print(f"Wrote {target}. Edit travellers, preferences and providers, then run `ticketbooker chat`.")
    return 0


def cmd_providers(service: TicketService, args: argparse.Namespace) -> int:
    for provider in service.providers.values():
        print(json.dumps(provider.describe()))
    return 0


def cmd_search(service: TicketService, args: argparse.Namespace) -> int:
    query = _query_from_args(args)
    result = service.search(query, args.provider)
    for name, error in result.errors.items():
        print(f"[{name}] {error}", file=sys.stderr)
    results = service.match(overrides=_overrides(args))
    if args.json:
        print(json.dumps([r.summary() for r in results[: args.limit]], indent=2, default=str))
    else:
        print_results(results, args.limit)
    return 0 if result.options or not result.errors else 2


def cmd_watch(service: TicketService, args: argparse.Namespace) -> int:
    """Poll until an option matching the preferences appears. Never books by itself."""
    interval = max(args.interval, MIN_WATCH_INTERVAL)
    query = _query_from_args(args)
    overrides = _overrides(args)
    seen: set[str] = set()
    attempt = 0
    while True:
        attempt += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            result = service.search(query, args.provider)
            matches = [r for r in service.match(overrides=overrides) if r.accepted and r.score >= args.min_score]
        except ProviderError as exc:
            print(f"[{stamp}] search failed: {exc}", file=sys.stderr)
            matches = []
        else:
            for name, error in result.errors.items():
                print(f"[{stamp}] [{name}] {error}", file=sys.stderr)
        fresh = [m for m in matches if f"{m.option.option_id}|{m.option.raw_availability}" not in seen]
        if fresh:
            print(f"\n[{stamp}] {len(fresh)} new match(es):")
            print_results(fresh, args.limit)
            print("\a", end="", flush=True)
            seen.update(f"{m.option.option_id}|{m.option.raw_availability}" for m in fresh)
            if args.once:
                return 0
        else:
            print(f"[{stamp}] check #{attempt}: no new matches", flush=True)
        if args.max_checks and attempt >= args.max_checks:
            return 0 if seen else 1
        time.sleep(interval)


def cmd_bookings(service: TicketService, args: argparse.Namespace) -> int:
    bookings = service.store.all()
    if not bookings:
        print("No bookings yet.")
    for b in bookings:
        print(f"{b.booking_id}  {b.status.value:<20} {b.reference or '-':<12} {b.option.title}  {_fmt_time(b.option.departure)}")
    return 0


def cmd_status(service: TicketService, args: argparse.Namespace) -> int:
    print_booking(service.booking_status(args.booking_id))
    return 0


def cmd_record_pnr(service: TicketService, args: argparse.Namespace) -> int:
    print_booking(service.record_reference(args.booking_id, args.reference))
    return 0


def cmd_chat(service: TicketService, args: argparse.Namespace) -> int:
    import anthropic

    from .agent import BookingAgent

    def on_event(kind: str, payload) -> None:
        if not args.verbose:
            if kind == "tool_call":
                print(f"  · {payload['name']}", file=sys.stderr)
            return
        print(f"  [{kind}] {json.dumps(payload, default=str)[:500]}", file=sys.stderr)

    agent = BookingAgent(service, confirm=confirm_on_terminal, on_event=on_event)
    prompts = [args.message] if args.message else None
    if not prompts:
        print("TicketBooker agent. Describe the trip or ticket you need (Ctrl-D or 'exit' to quit).")
    while True:
        if prompts is not None:
            if not prompts:
                return 0
            text = prompts.pop(0)
        else:
            try:
                text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text.lower() in {"exit", "quit"}:
                return 0
            if not text:
                continue
        try:
            reply = agent.ask(text)
        except anthropic.APIError as exc:
            print(f"error talking to Claude: {exc}", file=sys.stderr)
            if prompts is not None:
                return 1
            continue
        print(f"\nagent> {reply}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticketbooker", description="Configurable AI ticket booking assistant")
    parser.add_argument("-c", "--config", help="Path to config YAML (default: ./ticketbooker.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Write an example config file")
    p.add_argument("path", nargs="?", default="ticketbooker.yaml")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("chat", help="Talk to the booking agent (needs ANTHROPIC_API_KEY)")
    p.add_argument("-m", "--message", help="Send a single message and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="Show tool calls and results")

    sub.add_parser("providers", help="List configured providers")

    for name, help_text in (("search", "Search and rank tickets"), ("watch", "Poll until a matching ticket appears")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--category", default="train", choices=[c.value for c in Category])
        p.add_argument("--from", dest="origin")
        p.add_argument("--to", dest="destination")
        p.add_argument("--date", required=True, help="YYYY-MM-DD")
        p.add_argument("--class", dest="travel_class")
        p.add_argument("--quota")
        p.add_argument("--passengers", type=int, default=1)
        p.add_argument("--provider", action="append", help="Limit to provider(s)")
        p.add_argument("--max-price", type=float)
        p.add_argument("--classes", help="Allowed classes, most preferred first, e.g. 3A,2A")
        p.add_argument("--after", help="Earliest departure HH:MM")
        p.add_argument("--before", help="Latest departure HH:MM")
        p.add_argument("--max-waitlist", type=int)
        p.add_argument("--confirmed-only", action="store_true", help="Reject RAC and waitlist")
        p.add_argument("--limit", type=int, default=10)
        if name == "search":
            p.add_argument("--json", action="store_true")
        else:
            p.add_argument("--interval", type=int, default=300, help=f"Seconds between checks (min {MIN_WATCH_INTERVAL})")
            p.add_argument("--min-score", type=float, default=0)
            p.add_argument("--once", action="store_true", help="Exit after the first match")
            p.add_argument("--max-checks", type=int, default=0, help="Stop after N checks (0 = forever)")

    sub.add_parser("bookings", help="List bookings")
    p = sub.add_parser("status", help="Refresh and show a booking")
    p.add_argument("booking_id")
    p = sub.add_parser("record-pnr", help="Attach the PNR/reference to a booking you finished by hand")
    p.add_argument("booking_id")
    p.add_argument("reference")
    return parser


COMMANDS = {
    "chat": cmd_chat,
    "providers": cmd_providers,
    "search": cmd_search,
    "watch": cmd_watch,
    "bookings": cmd_bookings,
    "status": cmd_status,
    "record-pnr": cmd_record_pnr,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    try:
        config: AppConfig = load_config(args.config)
        service = TicketService(config)
        return COMMANDS[args.command](service, args)
    except (ProviderError, KeyError, ValueError, FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
