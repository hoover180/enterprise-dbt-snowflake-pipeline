"""Generate CRM account and support-ticket extracts for the reconciliation project."""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from faker import Faker

from identity_pool import IDENTITY_POOL_SEED, build_identity_pool


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_ACCOUNT_COUNT = 350
DEFAULT_TICKET_COUNT = 900
GHOST_ACCOUNT_RATE = 0.04
GHOST_ACCOUNT_POOL_SIZE = 25

COUNTRY_CODES = ("US", "US", "US", "DE", "FR", "NL", "ES", "IT")
US_COUNTRY_VARIANTS = ("US", "USA", "United States", "u.s.a.")

TICKET_CATEGORIES = ("billing", "technical", "account_access", "shipping", "general_inquiry")
TICKET_STATUSES = ("open", "pending", "resolved", "closed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts",
        type=int,
        default=DEFAULT_ACCOUNT_COUNT,
        help=f"Number of CRM accounts to generate (default: {DEFAULT_ACCOUNT_COUNT}).",
    )
    parser.add_argument(
        "--tickets",
        type=int,
        default=DEFAULT_TICKET_COUNT,
        help=f"Number of support tickets to generate (default: {DEFAULT_TICKET_COUNT}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260904,
        help="Seed for repeatable extracts (default: 20260904).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for CRM_CUSTOMERS.csv and CRM_TICKETS.csv.",
    )
    args = parser.parse_args()
    if args.accounts < 1:
        parser.error("--accounts must be at least 1")
    if args.tickets < 1:
        parser.error("--tickets must be at least 1")
    return args


def dirty_country(rng: random.Random) -> str:
    """Pick a shipping country, rendering the US in an inconsistently formatted way."""
    code = rng.choice(COUNTRY_CODES)
    return rng.choice(US_COUNTRY_VARIANTS) if code == "US" else code


def build_account(
    account_number: int,
    fake: Faker,
    rng: random.Random,
    person_by_index: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Create one CRM account row, keyed to a person via the shared identity pool."""
    customer_index = rng.randint(1, 350)
    person = person_by_index[customer_index]

    created_date = fake.date_between(start_date=date.today() - timedelta(days=730), end_date=date.today() - timedelta(days=1))
    last_seen_date = fake.date_between(start_date=created_date, end_date=date.today())

    return {
        "account_id": f"ACCT-{account_number:05d}",
        "contact_email": person["email"],
        "name": person["name"],
        "country": dirty_country(rng),
        "created_date": created_date.isoformat(),
        "last_seen_date": last_seen_date.isoformat(),
    }


def build_ticket(
    ticket_number: int,
    account_id: str,
    fake: Faker,
    rng: random.Random,
) -> dict[str, Any]:
    """Create one support ticket row referencing an account by account_id."""
    created_date = fake.date_between(start_date=date.today() - timedelta(days=365), end_date=date.today())
    return {
        "ticket_id": f"TCKT-{ticket_number:06d}",
        "account_id": account_id,
        "category": rng.choice(TICKET_CATEGORIES),
        "created_date": created_date.isoformat(),
        "status": rng.choice(TICKET_STATUSES),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_extracts(account_count: int, ticket_count: int, seed: int, output_dir: Path) -> None:
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    person_by_index = {person["index"]: person for person in build_identity_pool(IDENTITY_POOL_SEED)}

    accounts = [build_account(number, fake, rng, person_by_index) for number in range(1, account_count + 1)]
    account_ids = [account["account_id"] for account in accounts]

    # Ghost account_ids are numbered past the real account range so they can never
    # collide with a real ACCT-##### id, simulating accounts deleted from the CRM
    # while their ticket history survived.
    ghost_account_ids = [f"ACCT-{number:05d}" for number in range(account_count + 1, account_count + 1 + GHOST_ACCOUNT_POOL_SIZE)]
    ghost_count = round(ticket_count * GHOST_ACCOUNT_RATE)
    ghost_positions = set(rng.sample(range(ticket_count), ghost_count))

    tickets = []
    for position in range(ticket_count):
        account_id = rng.choice(ghost_account_ids) if position in ghost_positions else rng.choice(account_ids)
        tickets.append(build_ticket(position + 1, account_id, fake, rng))

    write_csv(output_dir / "CRM_CUSTOMERS.csv", accounts)
    write_csv(output_dir / "CRM_TICKETS.csv", tickets)


if __name__ == "__main__":
    arguments = parse_args()
    generate_extracts(arguments.accounts, arguments.tickets, arguments.seed, arguments.output_dir)
