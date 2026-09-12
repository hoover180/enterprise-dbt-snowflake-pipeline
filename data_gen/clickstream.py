"""Generate raw web clickstream events for the reconciliation project."""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from faker import Faker

from identity_pool import IDENTITY_POOL_SEED, build_identity_pool
from order_pool import (
    DEFAULT_ORDERS_PER_REGION,
    REGIONS,
    WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS,
    build_order,
    ghost_order_number,
)


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"
# DEFAULT_EVENT_COUNT covers only the three original, non-order-grain event
# types (page_view/cart_addition/search_query); purchase events are layered
# on top, sized by real ERP order coverage rather than by this flag -- see
# "purchase event volume" below for why this default grew ~10x from this
# generator's original 1,000.
DEFAULT_EVENT_COUNT = 9_600
EVENT_START = date(2025, 1, 5)
EVENT_END = date(2025, 12, 31)
SCHEMA_CUTOFF = date(2025, 7, 1)
DUPLICATE_RATE = 0.05
LATE_EVENT_RATE = 0.02

PAGE_PATHS = ("/", "/products", "/products/analytics", "/account", "/checkout")
SEARCH_QUERIES = ("running shoes", "wireless headphones", "coffee maker", "laptop stand")

# purchase event volume: real ERP order coverage (order_pool.WEB_MATCH_RATE,
# 35% of orders) plus its documented orphan share (12% of purchase events,
# WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS) drives how many purchase events exist
# at all -- it is not an independent dial. Against the default 1,000
# real orders (500/region), that's ~350 matched + ~48 orphans = ~398
# purchase events. DEFAULT_EVENT_COUNT (9,600) was chosen so that ~398 stays
# a "low single digits" share (~4.0%) of the combined total (~9,998) once
# purchase events are added -- see docs/data_modeling_decisions.md ADR-009.
# A realistic global ecommerce conversion rate is 2.5-3% (Contentsquare,
# Q3 2025); 4% here is close to that while accounting for this generator's
# event-level (not session-level) approximation of "conversion."

# Per-event-type probability that a web analytics pixel actually captures a
# customer identity signal at all. Real ecommerce analytics identifies the
# minority of traffic: an ordinary page_view or search_query only carries a
# known identity when it happens inside an already-authenticated session
# (e.g. a returning logged-in shopper just browsing), which is a small slice
# of all browsing. cart_addition is materially more likely to carry identity
# because it's the step closest to checkout, where an account-linked cart or
# a guest-checkout email capture (for retargeting/abandoned-cart flows) is
# common -- but it's still well under certainty, since plenty of carts are
# built by browsers who never authenticate or leave contact details. These
# are modeling assumptions for this synthetic dataset, not measured rates
# from a real site -- see docs/synthetic_data_spec.md for the full writeup.
IDENTITY_CAPTURE_RATE_BY_EVENT_TYPE = {
    "page_view": 0.08,
    "search_query": 0.08,
    "cart_addition": 0.55,
    # A completed checkout collects a contact email for the order
    # confirmation/receipt almost every time -- materially more certain than
    # cart_addition, which a browser can still abandon without ever handing
    # over contact details. Not 100%: a small guest-checkout slice still
    # goes untracked by the analytics pixel itself (an ad blocker, a
    # server-side-only checkout flow), independent of whether ERP received
    # the order.
    "purchase": 0.90,
}

# Among captured identity signals, how the observed value relates to the
# customer's canonical (identity-pool) email -- mirrors ADR-006's tiered,
# reasoned approach to CRM's country dirtiness rather than uniform noise.
# Cumulative thresholds against one rng.random() draw: below
# CAPTURED_EXACT_RATE is byte-for-byte exact (captured programmatically from
# an account record, e.g. a logged-in session's stored email -- no human
# ever retyped it); the next CAPTURED_TIER1_RATE is trivial
# casing/whitespace noise (a human typed or pasted it into a guest-checkout
# field); the remainder is a genuine single-character typo requiring real
# fuzzy matching (a fat-fingered manual entry). Exact is deliberately the
# largest bucket because most identity capture in a real pipeline is
# programmatic, not manually typed.
CAPTURED_EXACT_RATE = 0.55
CAPTURED_TIER1_RATE = 0.25
# Remaining 0.20 is tier 2 (genuine typo), a portion of which becomes a
# "collision" instead -- see COLLISION_RATE_WITHIN_TIER2 below.

# Of customers who land in the tier-2 (typo) branch AND whose canonical
# identity has a designated collision partner (identity_pool.py's
# `collision_target_index`, sized by COLLISION_PAIR_COUNT there), this is
# the chance the distortion is redirected to that partner's real canonical
# email instead of an ordinary bounded, non-colliding typo. Most customers
# have no collision partner at all, so this only ever fires for the small,
# designated collision-source population -- most tier-2 typos remain
# ordinary near-misses of the true customer, not collisions. See ADR
# (freeze baseline) in docs/data_modeling_decisions.md for the realized
# rate this produces against the full dataset.
COLLISION_RATE_WITHIN_TIER2 = 1.0

# Tier 1: trivial normalization noise that any lowercase+trim step resolves.
TIER1_VARIANTS = ("upper", "title_local", "leading_space", "trailing_space")

# Tier 2: a single-character-edit typo on the local part only (transpose,
# drop, or adjacent-key substitution), bounded to edit distance 1 so it
# can't plausibly collide with a different real customer's email while
# still requiring genuine fuzzy matching, not just normalization.
KEYBOARD_ADJACENTS = {
    "q": "wa", "w": "qes", "e": "wrd", "r": "etf", "t": "ryg", "y": "tuh",
    "u": "yij", "i": "uok", "o": "ipl", "p": "ol", "a": "qsz", "s": "awed",
    "d": "serfc", "f": "drtgv", "g": "ftyhb", "h": "gyujn", "j": "huikm",
    "k": "jiol", "l": "kop", "z": "asx", "x": "zsdc", "c": "xdfv",
    "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}


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
        "--orders-per-region",
        type=int,
        default=DEFAULT_ORDERS_PER_REGION,
        help=f"Real ERP orders per region to check for web purchase-event coverage "
        f"(default: {DEFAULT_ORDERS_PER_REGION}). Must match the value passed to "
        "erp.py's own --orders-per-region for purchase events' transaction_ids to "
        "correlate against real orders -- see data_gen/order_pool.py.",
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
    if args.orders_per_region < 1:
        parser.error("--orders-per-region must be at least 1")
    return args


def build_event_timestamp(event_date: date, rng: random.Random) -> datetime:
    """Create the true occurrence timestamp for an event (never shifted for late arrival)."""
    return datetime.combine(
        event_date,
        time(hour=rng.randrange(24), minute=rng.randrange(60), second=rng.randrange(60)),
    )


def build_ingested_at(event_timestamp: datetime, rng: random.Random, late: bool) -> datetime:
    """Create the ingestion timestamp, simulating pipeline arrival lag for late events."""
    if late:
        return event_timestamp + timedelta(days=rng.randint(2, 4))
    return event_timestamp + timedelta(seconds=rng.randint(1, 120))


def apply_trivial_normalization_noise(email: str, rng: random.Random) -> str:
    """Tier 1: casing/whitespace noise a lowercase+trim normalization step resolves."""
    style = rng.choice(TIER1_VARIANTS)
    if style == "upper":
        return email.upper()
    if style == "title_local":
        local, _, domain = email.partition("@")
        return f"{local.capitalize()}@{domain}"
    if style == "leading_space":
        return f"  {email}"
    return f"{email}  "


def apply_single_character_typo(email: str, rng: random.Random, all_emails: set[str]) -> str:
    """Tier 2: one bounded, local-part-only edit requiring genuine fuzzy matching."""
    local, _, domain = email.partition("@")
    if len(local) < 2:
        return email

    for _ in range(5):
        kind = rng.choice(("transpose", "drop", "substitute_adjacent_key"))
        if kind == "transpose":
            position = rng.randrange(len(local) - 1)
            chars = list(local)
            chars[position], chars[position + 1] = chars[position + 1], chars[position]
            candidate_local = "".join(chars)
        elif kind == "drop":
            position = rng.randrange(len(local))
            candidate_local = local[:position] + local[position + 1:]
        else:
            position = rng.randrange(len(local))
            char = local[position].lower()
            replacement = rng.choice(KEYBOARD_ADJACENTS.get(char, char))
            candidate_local = local[:position] + replacement + local[position + 1:]

        candidate = f"{candidate_local}@{domain}"
        # Bounded to a single edit on the local part so this should never
        # coincide with a different real customer's email, but check anyway
        # rather than assume -- an accidental collision would create genuine
        # ambiguity between two people, which is a different (undesired)
        # failure mode from "requires fuzzy matching."
        if candidate != email and candidate not in all_emails:
            return candidate
    return email


def roll_identity_capture(
    event_type: str,
    canonical_email: str,
    rng: random.Random,
    all_emails: set[str],
    collision_target_email: str | None = None,
) -> tuple[str | None, str, bool]:
    """Shared capture/dirty-tier roll used by every event type, purchase included.

    `collision_target_email` is non-None only for the small, designated
    collision-source population (identity_pool.py's `collision_target_index`)
    -- for everyone else, the tier-2 branch always falls through to the
    ordinary bounded, non-colliding typo.
    """
    captured = rng.random() < IDENTITY_CAPTURE_RATE_BY_EVENT_TYPE[event_type]
    identity_email = None
    dirt_tier = ""
    if captured:
        dirt_roll = rng.random()
        if dirt_roll < CAPTURED_EXACT_RATE:
            identity_email, dirt_tier = canonical_email, "exact"
        elif dirt_roll < CAPTURED_EXACT_RATE + CAPTURED_TIER1_RATE:
            identity_email, dirt_tier = apply_trivial_normalization_noise(canonical_email, rng), "tier1"
        elif collision_target_email is not None and rng.random() < COLLISION_RATE_WITHIN_TIER2:
            identity_email, dirt_tier = collision_target_email, "collision"
        else:
            identity_email, dirt_tier = apply_single_character_typo(canonical_email, rng, all_emails), "tier2"
    return identity_email, dirt_tier, captured


def match_status_for(captured: bool, dirt_tier: str) -> str:
    """Ground-truth label for CLICKSTREAM_IDENTITY_TRUTH.csv's scoreable match_status column.

    Distinguishes an ordinary resolvable identity (exact/tier1/tier2 -- a
    fuzzy matcher should map this back to the true customer) from a designed
    collision (the observed value is a DIFFERENT real customer's actual
    canonical email -- a future scoring pass should grade a resolver that
    merges this to that other customer as a false merge, not a hit), so
    Phase 5A's eventual scorer can compute a false-merge rate, not just
    recall. Blank when nothing was captured -- there is nothing to score.
    """
    if not captured:
        return ""
    return "possible_collision" if dirt_tier == "collision" else "match"


def build_event(
    fake: Faker,
    rng: random.Random,
    late: bool,
    email_by_index: dict[int, str],
    all_emails: set[str],
    collision_target_email_by_index: dict[int, str | None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one synthetic clickstream event, plus its identity-truth record."""
    event_date = EVENT_START + timedelta(days=rng.randrange((EVENT_END - EVENT_START).days + 1))
    event_type = rng.choices(
        ("page_view", "cart_addition", "search_query"),
        weights=(70, 15, 15),
        k=1,
    )[0]
    timestamp = build_event_timestamp(event_date, rng)
    ingested_at = build_ingested_at(timestamp, rng, late)

    # Every event is generated as if from one of the 350 known people --
    # this project doesn't model unknown/never-a-customer web traffic (see
    # docs/synthetic_data_spec.md) -- but the identity signal below is only
    # ever written to the event when "captured", so most events carry no
    # identity field at all regardless of whose traffic it truly is.
    customer_index = rng.randint(1, 350)
    canonical_email = email_by_index[customer_index]
    identity_email, dirt_tier, captured = roll_identity_capture(
        event_type, canonical_email, rng, all_emails, collision_target_email_by_index.get(customer_index)
    )

    event: dict[str, Any] = {
        "event_id": fake.uuid4(),
        "event_timestamp": f"{timestamp.isoformat()}Z",
        "event_date": event_date.isoformat(),
        "ingested_at": f"{ingested_at.isoformat()}Z",
        "session_id": f"SESSION-{rng.randint(1, 500):06d}",
        "event_type": event_type,
    }
    if identity_email is not None:
        event["user_email" if event_date < SCHEMA_CUTOFF else "customer_global_email"] = identity_email

    if event_type == "page_view":
        event["page_url"] = f"https://www.globalretail.example{rng.choice(PAGE_PATHS)}"
    elif event_type == "cart_addition":
        event["product_id"] = f"SKU-{rng.randint(1, 250):05d}"
        event["quantity"] = rng.randint(1, 3)
    else:
        event["search_query"] = rng.choice(SEARCH_QUERIES)

    truth: dict[str, Any] = {
        "event_id": event["event_id"],
        "customer_index": customer_index,
        "canonical_email": canonical_email,
        "captured": captured,
        "dirt_tier": dirt_tier,
        "observed_value": identity_email or "",
        "match_status": match_status_for(captured, dirt_tier),
    }
    return event, truth


def build_purchase_event(
    order: dict[str, Any],
    fake: Faker,
    rng: random.Random,
    email_by_index: dict[int, str],
    all_emails: set[str],
    collision_target_email_by_index: dict[int, str | None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one purchase event from an order_pool order (real or ghost).

    `order["order_id"]` becomes `transaction_id` -- this project's shared
    order pool is exactly what makes this correlatable to ERP. The checkout
    timestamp uses the order's own order_date (never ship_date): checkout is
    the moment the customer completed payment, which is what a real
    analytics pixel would timestamp, and which is free to land in an earlier
    calendar month than ERP's ship_date-based recognition -- see ADR-009.
    Not subject to the pixel-retry-duplicate or late-arrival mechanics below
    (those remain scoped to the original three event types, unchanged).
    """
    event_date = order["order_date"]
    timestamp = build_event_timestamp(event_date, rng)
    ingested_at = build_ingested_at(timestamp, rng, late=False)

    customer_index = order["customer_index"]
    canonical_email = email_by_index[customer_index]
    identity_email, dirt_tier, captured = roll_identity_capture(
        "purchase", canonical_email, rng, all_emails, collision_target_email_by_index.get(customer_index)
    )

    event: dict[str, Any] = {
        "event_id": fake.uuid4(),
        "event_timestamp": f"{timestamp.isoformat()}Z",
        "event_date": event_date.isoformat(),
        "ingested_at": f"{ingested_at.isoformat()}Z",
        "session_id": f"SESSION-{rng.randint(1, 500):06d}",
        "event_type": "purchase",
        "transaction_id": order["order_id"],
        "checkout_total": f"{order['checkout_total']:.2f}",
        "currency": order["currency"],
    }
    if identity_email is not None:
        event["user_email" if event_date < SCHEMA_CUTOFF else "customer_global_email"] = identity_email

    truth: dict[str, Any] = {
        "event_id": event["event_id"],
        "customer_index": customer_index,
        "canonical_email": canonical_email,
        "captured": captured,
        "dirt_tier": dirt_tier,
        "observed_value": identity_email or "",
        "match_status": match_status_for(captured, dirt_tier),
    }
    return event, truth


def write_json_lines(path: Path, events: list[dict[str, Any]]) -> None:
    """Write events as newline-delimited JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(json.dumps(event, sort_keys=True))
            output.write("\n")


def write_identity_truth_csv(path: Path, truth_records: list[dict[str, Any]]) -> None:
    """Write the per-event identity-truth sidecar (generation-time bookkeeping only).

    Not loaded into RAW_WEB_EVENTS and not itself a claim about how Phase 5A
    should score identity resolution -- see docs/synthetic_data_spec.md and
    ADR-008 in docs/data_modeling_decisions.md. Post-hoc scanning of the raw
    JSON output (as data_gen/build_match_truth.py did before this fix) can
    no longer recover ground truth once the identity signal is sparse and
    tier-2 values are genuinely, irreversibly distorted -- this file exists
    because the truth is only knowable at the moment of generation, before
    dirtying, not by inspecting the dirtied output afterward.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "event_id",
        "customer_index",
        "canonical_email",
        "captured",
        "dirt_tier",
        "observed_value",
        "match_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(truth_records)


def build_purchase_events(
    orders_per_region: int,
    rng: random.Random,
    fake: Faker,
    email_by_index: dict[int, str],
    all_emails: set[str],
    collision_target_email_by_index: dict[int, str | None],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Build every purchase event: one per real order with web coverage, plus orphans.

    Coverage (order_pool.WEB_MATCH_RATE) and the orphan share
    (order_pool.WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS) are both emergent from
    the shared order pool, not independently dialed here -- see ADR-009.
    """
    all_real_orders = [
        build_order(region, order_number)
        for region in REGIONS
        for order_number in range(1, orders_per_region + 1)
    ]
    matched = [order for order in all_real_orders if order["has_web_purchase"]]

    orphan_count = round(
        len(matched) * WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS / (1 - WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS)
    )
    orphan_orders = []
    for _ in range(orphan_count):
        region = rng.choice(REGIONS)
        ghost_number = ghost_order_number(orders_per_region, rng)
        orphan_orders.append(build_order(region, ghost_number))

    return [
        build_purchase_event(order, fake, rng, email_by_index, all_emails, collision_target_email_by_index)
        for order in matched + orphan_orders
    ]


def generate_events(event_count: int, seed: int, orders_per_region: int, output_dir: Path) -> None:
    """Generate and write a reproducible clickstream extract."""
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    people = build_identity_pool(IDENTITY_POOL_SEED)
    email_by_index = {person["index"]: person["email"] for person in people}
    all_emails = {person["email"] for person in people}
    collision_target_email_by_index: dict[int, str | None] = {
        person["index"]: (
            email_by_index[person["collision_target_index"]] if person["collision_target_index"] else None
        )
        for person in people
    }

    late_count = max(1, round(event_count * LATE_EVENT_RATE))
    built = [
        build_event(fake, rng, index < late_count, email_by_index, all_emails, collision_target_email_by_index)
        for index in range(event_count)
    ]
    events = [event for event, _truth in built]
    truth_records = [truth for _event, truth in built]

    duplicate_count = max(1, round(event_count * DUPLICATE_RATE))
    events.extend(dict(events[index]) for index in rng.sample(range(event_count), duplicate_count))

    purchase_built = build_purchase_events(
        orders_per_region, rng, fake, email_by_index, all_emails, collision_target_email_by_index
    )
    events.extend(event for event, _truth in purchase_built)
    truth_records.extend(truth for _event, truth in purchase_built)

    rng.shuffle(events)

    write_json_lines(output_dir / "CLICKSTREAM_EVENTS.json", events)
    write_identity_truth_csv(output_dir / "CLICKSTREAM_IDENTITY_TRUTH.csv", truth_records)


if __name__ == "__main__":
    arguments = parse_args()
    generate_events(arguments.events, arguments.seed, arguments.orders_per_region, arguments.output_dir)
