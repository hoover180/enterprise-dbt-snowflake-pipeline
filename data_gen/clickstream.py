"""Generate raw web clickstream events for the reconciliation project."""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from faker import Faker


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_EVENT_COUNT = 1_000
EVENT_START = date(2025, 1, 5)
EVENT_END = date(2025, 12, 31)
SCHEMA_CUTOFF = date(2025, 7, 1)
DUPLICATE_RATE = 0.05
LATE_EVENT_RATE = 0.02

PAGE_PATHS = ("/", "/products", "/products/analytics", "/account", "/checkout")
SEARCH_QUERIES = ("running shoes", "wireless headphones", "coffee maker", "laptop stand")


def parse_args() -> argparse.Namespace:
    """Parse command-line options for clickstream generation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events",
        type=int,
        default=DEFAULT_EVENT_COUNT,
        help=f"Number of canonical events to generate (default: {DEFAULT_EVENT_COUNT}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260904,
        help="Seed for repeatable events (default: 20260904).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for CLICKSTREAM_EVENTS.json.",
    )
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be at least 1")
    return args


def event_timestamp(event_date: date, rng: random.Random, late: bool) -> datetime:
    """Create an event timestamp, optionally simulating late arrival."""
    timestamp_date = event_date + timedelta(days=rng.randint(2, 4)) if late else event_date
    return datetime.combine(
        timestamp_date,
        time(hour=rng.randrange(24), minute=rng.randrange(60), second=rng.randrange(60)),
    )


def build_event(fake: Faker, rng: random.Random, late: bool) -> dict[str, Any]:
    """Build one synthetic clickstream event."""
    event_date = EVENT_START + timedelta(days=rng.randrange((EVENT_END - EVENT_START).days + 1))
    event_type = rng.choices(
        ("page_view", "cart_addition", "search_query"),
        weights=(70, 15, 15),
        k=1,
    )[0]
    timestamp = event_timestamp(event_date, rng, late)
    user_id = f"CUST-{rng.randint(1, 350):05d}"

    event: dict[str, Any] = {
        "event_id": fake.uuid4(),
        "event_timestamp": f"{timestamp.isoformat()}Z",
        "event_date": event_date.isoformat(),
        "session_id": f"SESSION-{rng.randint(1, 500):06d}",
        "event_type": event_type,
    }
    event["user_id" if event_date < SCHEMA_CUTOFF else "customer_global_id"] = user_id

    if event_type == "page_view":
        event["page_url"] = f"https://www.globalretail.example{rng.choice(PAGE_PATHS)}"
    elif event_type == "cart_addition":
        event["product_id"] = f"SKU-{rng.randint(1, 250):05d}"
        event["quantity"] = rng.randint(1, 3)
    else:
        event["search_query"] = rng.choice(SEARCH_QUERIES)
    return event


def write_json_lines(path: Path, events: list[dict[str, Any]]) -> None:
    """Write events as newline-delimited JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(json.dumps(event, sort_keys=True))
            output.write("\n")


def generate_events(event_count: int, seed: int, output_dir: Path) -> None:
    """Generate and write a reproducible clickstream extract."""
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    late_count = max(1, round(event_count * LATE_EVENT_RATE))
    events = [build_event(fake, rng, index < late_count) for index in range(event_count)]
    duplicate_count = max(1, round(event_count * DUPLICATE_RATE))
    events.extend(dict(events[index]) for index in rng.sample(range(event_count), duplicate_count))
    rng.shuffle(events)
    write_json_lines(output_dir / "CLICKSTREAM_EVENTS.json", events)


if __name__ == "__main__":
    arguments = parse_args()
    generate_events(arguments.events, arguments.seed, arguments.output_dir)
