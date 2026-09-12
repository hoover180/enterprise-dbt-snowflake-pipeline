"""Generate CRM account and support-ticket extracts for the reconciliation project."""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from faker import Faker

from identity_pool import IDENTITY_POOL_SEED, build_identity_pool
from order_pool import (
    DEFAULT_ORDERS_PER_REGION,
    REFUND_WINDOW_END,
    REGIONS,
    build_order,
    ghost_order_number,
    order_id_for,
)


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_ACCOUNT_COUNT = 350
DEFAULT_TICKET_COUNT = 900
GHOST_ACCOUNT_RATE = 0.04
GHOST_ACCOUNT_POOL_SIZE = 25

# Fixed "as-of" reference date, matching clickstream.py's EVENT_END, so
# regeneration with the same seed is fully reproducible: date.today() would
# leave the RNG-driven content identical but silently drift every date value
# forward by however many days have passed since the last run.
AS_OF_DATE = date(2025, 12, 31)
ACCOUNT_CREATED_START = AS_OF_DATE - timedelta(days=730)
ACCOUNT_CREATED_END = AS_OF_DATE - timedelta(days=1)
TICKET_CREATED_START = AS_OF_DATE - timedelta(days=365)
TICKET_CREATED_END = AS_OF_DATE

COUNTRY_CODES = ("US", "US", "US", "DE", "FR", "NL", "ES", "IT")
US_COUNTRY_VARIANTS = ("US", "USA", "United States", "u.s.a.")

TICKET_CATEGORIES = ("billing", "technical", "account_access", "shipping", "general_inquiry")
TICKET_STATUSES = ("open", "pending", "resolved", "closed")

# --- Refund tickets --------------------------------------------------------
# A ticket count relative to the number of orders order_pool actually marks
# "returned" (not an independent dial) -- >1.0 lets some returned orders draw
# more than one contact (a customer calling twice, or a duplicate contact,
# per this phase's build plan) while others draw none (self-service return,
# no CRM contact at all).
REFUND_TICKET_MULTIPLIER = 1.15

# Order-reference correctness tail, mirroring GHOST_ACCOUNT_RATE's proportions
# (a minority pattern, not a headline-sized fraction): most refund tickets
# reference the correct real order; a small tail is a transposition-style
# typo landing on a DIFFERENT real, existing order (the dangerous
# wrong-but-valid case -- a confident wrong match, not an obvious miss); a
# smaller tail reuses the ghost-reference mechanism already built for CRM's
# ghost accounts, referencing an order that doesn't exist at all.
REFUND_REF_WRONG_VALID_RATE = 0.06
REFUND_REF_NONEXISTENT_RATE = 0.04
# Remaining ~0.90 is a correct reference.

# Of refund tickets, the share still open/pending (an operational event --
# the customer's complaint -- not yet reflected in ERP's own refund_date/
# refund_amount) vs. posted (resolved/closed, ERP has since caught up).
REFUND_OPEN_RATE = 0.35

# Of POSTED refund tickets only, the fraction whose claimed_amount was typed
# before/without accounting for shipping or tax the same way ERP's actual
# refund_amount does -- a real, bounded clerical mismatch, not noise.
REFUND_POSTED_MISMATCH_RATE = 0.10
REFUND_MISMATCH_ADJUSTMENTS = (Decimal("5.99"), Decimal("7.99"), Decimal("9.99"), Decimal("12.99"))


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
        "--orders-per-region",
        type=int,
        default=DEFAULT_ORDERS_PER_REGION,
        help=f"Real ERP orders per region to draw refund tickets against (default: "
        f"{DEFAULT_ORDERS_PER_REGION}). Must match the value passed to erp.py's own "
        "--orders-per-region for order references to correlate against real orders "
        "-- see data_gen/order_pool.py.",
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
    if args.orders_per_region < 1:
        parser.error("--orders-per-region must be at least 1")
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

    created_date = fake.date_between(start_date=ACCOUNT_CREATED_START, end_date=ACCOUNT_CREATED_END)
    last_seen_date = fake.date_between(start_date=created_date, end_date=AS_OF_DATE)

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
    """Create one support ticket row referencing an account by account_id.

    `claimed_amount`/`order_reference` are always present as columns (CSV
    needs one consistent header row) but only ever populated for `category
    == "refund"` tickets, built separately by build_refund_ticket() below --
    the same category-conditional-nullability pattern this project already
    uses for web's event-type-conditional fields (page_url/product_id/
    search_query).
    """
    created_date = fake.date_between(start_date=TICKET_CREATED_START, end_date=TICKET_CREATED_END)
    return {
        "ticket_id": f"TCKT-{ticket_number:06d}",
        "account_id": account_id,
        "category": rng.choice(TICKET_CATEGORIES),
        "created_date": created_date.isoformat(),
        "status": rng.choice(TICKET_STATUSES),
        "claimed_amount": "",
        "order_reference": "",
    }


def transpose_order_number(order_number: int, orders_per_region: int, rng: random.Random) -> int:
    """A bounded, transposition-style typo on a zero-padded order number.

    Mirrors clickstream.py's apply_single_character_typo: a handful of bounded
    attempts at a single adjacent-digit swap that lands on a DIFFERENT real,
    valid order number, falling back to an explicit random-but-valid choice
    only if none of those attempts land in range -- so this always returns a
    different, real order number, the same "wrong-but-valid" case a
    transposition typo produces in reality.
    """
    digits = list(f"{order_number:06d}")
    for _ in range(5):
        position = rng.randrange(len(digits) - 1)
        candidate_digits = digits.copy()
        candidate_digits[position], candidate_digits[position + 1] = (
            candidate_digits[position + 1],
            candidate_digits[position],
        )
        candidate = int("".join(candidate_digits))
        if candidate != order_number and 1 <= candidate <= orders_per_region:
            return candidate

    while True:
        candidate = rng.randint(1, orders_per_region)
        if candidate != order_number:
            return candidate


def build_refund_ticket(
    ticket_number: int,
    account_id: str,
    anchor_order: dict[str, Any],
    orders_per_region: int,
    rng: random.Random,
) -> dict[str, Any]:
    """Create one refund ticket anchored to a real ERP-returned order.

    `anchor_order` is always a genuine `status == "returned"` order from
    order_pool -- the underlying customer complaint this ticket represents is
    always real. What varies is (a) whether the order_reference the agent
    typed actually resolves to that same order, a different real order, or no
    order at all, and (b) whether this ticket has been posted against ERP's
    own refund_date/refund_amount yet.
    """
    ref_roll = rng.random()
    if ref_roll < REFUND_REF_NONEXISTENT_RATE:
        ghost_number = ghost_order_number(orders_per_region, rng)
        order_reference = order_id_for(anchor_order["region"], ghost_number)
    elif ref_roll < REFUND_REF_NONEXISTENT_RATE + REFUND_REF_WRONG_VALID_RATE:
        wrong_number = transpose_order_number(anchor_order["order_number"], orders_per_region, rng)
        order_reference = order_id_for(anchor_order["region"], wrong_number)
    else:
        order_reference = anchor_order["order_id"]

    posted = rng.random() >= REFUND_OPEN_RATE
    claimed_amount = anchor_order["refund_amount"]
    if posted:
        status = rng.choice(("resolved", "closed"))
        if rng.random() < REFUND_POSTED_MISMATCH_RATE:
            adjustment = rng.choice(REFUND_MISMATCH_ADJUSTMENTS)
            if rng.random() < 0.5:
                adjustment = -adjustment
            claimed_amount = (claimed_amount + adjustment).quantize(Decimal("0.01"))
    else:
        status = rng.choice(("open", "pending"))

    # The ticket is the customer's own contact about the return, so it's
    # created a few days on either side of ERP's refund_date -- allowed to
    # extend to the same widened window refund_date itself uses (see
    # order_pool.REFUND_WINDOW_END), not clamped back to AS_OF_DATE.
    created_date = anchor_order["refund_date"] + timedelta(days=rng.randint(-5, 5))
    created_date = max(TICKET_CREATED_START, min(created_date, REFUND_WINDOW_END))

    return {
        "ticket_id": f"TCKT-{ticket_number:06d}",
        "account_id": account_id,
        "category": "refund",
        "created_date": created_date.isoformat(),
        "status": status,
        "claimed_amount": f"{claimed_amount:.2f}",
        "order_reference": order_reference,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_extracts(
    account_count: int,
    ticket_count: int,
    seed: int,
    orders_per_region: int,
    output_dir: Path,
) -> None:
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

    all_real_orders = [
        build_order(region, order_number)
        for region in REGIONS
        for order_number in range(1, orders_per_region + 1)
    ]
    returned_orders = [order for order in all_real_orders if order["status"] == "returned"]
    refund_ticket_count = round(len(returned_orders) * REFUND_TICKET_MULTIPLIER)
    for offset in range(refund_ticket_count):
        anchor_order = rng.choice(returned_orders)
        account_id = rng.choice(account_ids)
        tickets.append(
            build_refund_ticket(ticket_count + offset + 1, account_id, anchor_order, orders_per_region, rng)
        )

    write_csv(output_dir / "CRM_CUSTOMERS.csv", accounts)
    write_csv(output_dir / "CRM_TICKETS.csv", tickets)


if __name__ == "__main__":
    arguments = parse_args()
    generate_extracts(
        arguments.accounts,
        arguments.tickets,
        arguments.seed,
        arguments.orders_per_region,
        arguments.output_dir,
    )
