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

# Order-reference correctness tail. A small tail is a transposition-style
# typo landing on a DIFFERENT real, existing order (the dangerous
# wrong-but-valid case -- a confident wrong match, not an obvious miss); a
# smaller tail reuses the ghost-reference mechanism already built for CRM's
# ghost accounts, referencing an order that doesn't exist at all.
#
# REFUND_REF_WRONG_VALID_RATE was originally 0.06 (mirroring
# GHOST_ACCOUNT_RATE's proportions), but freeze-gate peer review found that
# too thin: only 4 of 224 refund tickets were even DETECTABLE as
# wrong-but-valid by a naive anti-join+status check against the default
# dataset (most of the true wrong-but-valid tail happens to land on another
# order that's also returned, making it indistinguishable from a correct
# reference without replaying the generator's own intent -- see the freeze
# baseline ADR in docs/data_modeling_decisions.md). Raised to 0.18 so a
# naive inner join on order_reference misattributes a materially visible
# share of refund tickets and dollars, not a footnote-sized one.
REFUND_REF_WRONG_VALID_RATE = 0.18
REFUND_REF_NONEXISTENT_RATE = 0.04
# Remaining ~0.78 is a correct reference.

# --- Deliberate, bounded duplicate-CRM-account population -----------------
# Found during freeze-gate investigation: build_account() used to draw
# customer_index = rng.randint(1, 350) independently per account row (a
# sample WITH replacement over the 350-person pool), which was never an
# intentional mechanism -- it just meant ~125 of the 350 real people had NO
# CRM account at all, and ~93 had 2-4 accounts purely by chance, all
# undocumented. Accounts are now assigned one-to-one (a shuffled, without-
# replacement mapping -- see generate_extracts) so CRM coverage of the
# identity pool is what the existing docs already implied it was. On top of
# that clean 1:1 baseline, a small, deliberate, documented fraction of
# people get a genuine SECOND account -- the realistic "customer signed up
# twice" case peer review asked for -- with a name-formatting/casing
# variant, mirroring dirty_country()'s reasoned-variant approach.
DUPLICATE_ACCOUNT_RATE = 0.03
NAME_VARIANT_STYLES = ("upper", "last_first", "initial_first")

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


def dirty_name_variant(name: str, rng: random.Random) -> str:
    """A plausible alternate rendering of the same person's name for a duplicate account.

    Mirrors dirty_country()'s reasoned-variant approach (a handful of fixed,
    plausible renderings) rather than random character noise -- a second
    signup from the same real person plausibly types their own name a
    different way (different casing, or last-name-first), not a typo.
    """
    style = rng.choice(NAME_VARIANT_STYLES)
    first, _, last = name.partition(" ")
    if style == "upper":
        return name.upper()
    if style == "last_first" and last:
        return f"{last}, {first}"
    if style == "initial_first" and last:
        return f"{first[0]}. {last}"
    return name.upper()


def build_account(
    account_number: int,
    customer_index: int,
    fake: Faker,
    rng: random.Random,
    person_by_index: dict[int, dict[str, Any]],
    name_override: str | None = None,
) -> dict[str, Any]:
    """Create one CRM account row, keyed to a person via the shared identity pool."""
    person = person_by_index[customer_index]

    created_date = fake.date_between(start_date=ACCOUNT_CREATED_START, end_date=ACCOUNT_CREATED_END)
    last_seen_date = fake.date_between(start_date=created_date, end_date=AS_OF_DATE)

    return {
        "account_id": f"ACCT-{account_number:05d}",
        "contact_email": person["email"],
        "name": name_override or person["name"],
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

    # One account per real person, without replacement -- see
    # DUPLICATE_ACCOUNT_RATE above for why this is no longer a plain
    # rng.randint(1, 350) draw per account. A shuffled assignment (not
    # sequential index order) keeps account_number uncorrelated with
    # customer_index, matching this project's existing "no digit
    # relationship between systems' own keys" precedent.
    shuffled_indices = list(range(1, 351))
    rng.shuffle(shuffled_indices)
    assigned_indices = shuffled_indices[:account_count]

    accounts = [
        build_account(number, customer_index, fake, rng, person_by_index)
        for number, customer_index in enumerate(assigned_indices, start=1)
    ]
    next_account_number = len(accounts) + 1

    # --account-count > 350 (not this project's default) falls back to
    # sampling with replacement for the overflow -- an edge case the default
    # run never exercises, kept simple rather than over-engineered.
    while len(accounts) < account_count:
        customer_index = rng.randint(1, 350)
        accounts.append(build_account(next_account_number, customer_index, fake, rng, person_by_index))
        next_account_number += 1

    # Small, deliberate duplicate-account population (see
    # DUPLICATE_ACCOUNT_RATE above): a bounded subset of already-assigned
    # people get a second account with a name-formatting variant.
    duplicate_count = round(len(assigned_indices) * DUPLICATE_ACCOUNT_RATE)
    duplicate_source_indices = rng.sample(assigned_indices, min(duplicate_count, len(assigned_indices)))
    for customer_index in duplicate_source_indices:
        variant_name = dirty_name_variant(person_by_index[customer_index]["name"], rng)
        accounts.append(
            build_account(next_account_number, customer_index, fake, rng, person_by_index, variant_name)
        )
        next_account_number += 1

    account_ids = [account["account_id"] for account in accounts]

    # Ghost account_ids are numbered past the real account range (including
    # the deliberate duplicate accounts appended above) so they can never
    # collide with a real ACCT-##### id, simulating accounts deleted from the
    # CRM while their ticket history survived.
    ghost_start = next_account_number
    ghost_account_ids = [f"ACCT-{number:05d}" for number in range(ghost_start, ghost_start + GHOST_ACCOUNT_POOL_SIZE)]
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
