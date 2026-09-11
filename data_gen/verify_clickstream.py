"""Verify invariants of a generated clickstream extract.

Checks things `data_gen/clickstream.py` is expected to guarantee:

1. Schema-drift cutoff: no event carries both `user_email` and
   `customer_global_email`; an event carrying `user_email` is always dated
   before `SCHEMA_CUTOFF`, and an event carrying `customer_global_email` is
   always dated on/after it. Most events carry neither (identity capture is
   sparse -- see docs/synthetic_data_spec.md) -- that is expected, not a
   violation.
2. Late-arrival modeling: `event_date`/`event_timestamp` always reflect the
   true, unshifted occurrence time, and late arrival is represented solely by
   `ingested_at` landing `LATE_MIN_DAYS`-`LATE_MAX_DAYS` days after
   `event_timestamp` -- never by displacing `event_timestamp` itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "CLICKSTREAM_EVENTS.json"
SCHEMA_CUTOFF = date(2025, 7, 1)
LATE_MIN_DAYS = 2
LATE_MAX_DAYS = 4


def parse_args() -> argparse.Namespace:
    """Parse command-line options for clickstream verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_PATH,
        help=f"Path to the newline-delimited CLICKSTREAM_EVENTS.json extract (default: {DEFAULT_PATH}).",
    )
    return parser.parse_args()


def load_events(path: Path) -> list[dict[str, Any]]:
    """Load newline-delimited JSON clickstream events."""
    if not path.is_file():
        sys.exit(f"Clickstream extract not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def parse_timestamp(value: str) -> datetime:
    """Parse a `...Z`-suffixed ISO timestamp as written by clickstream.py."""
    return datetime.fromisoformat(value.removesuffix("Z"))


def check_schema_cutoff(events: list[dict[str, Any]]) -> tuple[list[str], int]:
    """Return (violation messages, count of events carrying an identity field).

    Identity capture is sparse and event-type-dependent (see
    docs/synthetic_data_spec.md) -- most events carry neither field, and
    that's expected, not a violation. Only events that DO carry a field are
    checked against the cutoff.
    """
    violations = []
    captured_count = 0
    for event in events:
        event_date = date.fromisoformat(event["event_date"])
        has_user_email = "user_email" in event
        has_global_email = "customer_global_email" in event
        if has_user_email and has_global_email:
            violations.append(f"{event['event_id']}: carries both user_email and customer_global_email")
            continue
        if has_user_email:
            captured_count += 1
            if event_date >= SCHEMA_CUTOFF:
                violations.append(f"{event['event_id']}: event_date {event_date} >= cutoff but carries user_email")
        elif has_global_email:
            captured_count += 1
            if event_date < SCHEMA_CUTOFF:
                violations.append(f"{event['event_id']}: event_date {event_date} < cutoff but carries customer_global_email")
    return violations, captured_count


def check_late_arrival(events: list[dict[str, Any]]) -> tuple[list[str], int, int]:
    """Return (violation messages, late count, on-time count) for the three-field time model."""
    violations = []
    late_count = 0
    on_time_count = 0
    for event in events:
        event_date = date.fromisoformat(event["event_date"])
        event_timestamp = parse_timestamp(event["event_timestamp"])
        ingested_at = parse_timestamp(event["ingested_at"])

        if event_timestamp.date() != event_date:
            violations.append(
                f"{event['event_id']}: event_timestamp date {event_timestamp.date()} "
                f"!= event_date {event_date} (event_timestamp must never be shifted)"
            )

        delay = ingested_at - event_timestamp
        if delay >= timedelta(days=LATE_MIN_DAYS):
            late_count += 1
            if not (timedelta(days=LATE_MIN_DAYS) <= delay <= timedelta(days=LATE_MAX_DAYS, hours=23, minutes=59, seconds=59)):
                violations.append(
                    f"{event['event_id']}: late-arrival delay {delay} outside "
                    f"[{LATE_MIN_DAYS}, {LATE_MAX_DAYS}] days"
                )
        else:
            on_time_count += 1
            if delay < timedelta(0):
                violations.append(f"{event['event_id']}: ingested_at {ingested_at} precedes event_timestamp {event_timestamp}")

    return violations, late_count, on_time_count


def main() -> int:
    args = parse_args()
    events = load_events(args.file)

    cutoff_violations, captured_count = check_schema_cutoff(events)
    late_arrival_violations, late_count, on_time_count = check_late_arrival(events)

    print(f"Checked {len(events)} records from {args.file}")
    print(f"Events carrying an identity field: {captured_count} ({captured_count / len(events):.1%})")
    print(f"Schema-cutoff violations: {len(cutoff_violations)}")
    print(f"Late events: {late_count}, on-time events: {on_time_count}")
    print(f"Late-arrival / timestamp-shift violations: {len(late_arrival_violations)}")

    all_violations = cutoff_violations + late_arrival_violations
    if all_violations:
        print("\nViolations:")
        for message in all_violations:
            print(f"  - {message}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
