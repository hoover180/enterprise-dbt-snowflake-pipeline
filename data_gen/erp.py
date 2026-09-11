"""Generate regional ERP order-item extracts for the reconciliation project."""

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


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"
REGIONS = ("US", "EU")

# Fixed "as-of" reference date, matching clickstream.py's EVENT_END, so
# regeneration with the same seed is fully reproducible: date.today() would
# leave the RNG-driven content identical but silently drift every date value
# forward by however many days have passed since the last run.
AS_OF_DATE = date(2025, 12, 31)
ORDER_DATE_START = AS_OF_DATE - timedelta(days=365)
ORDER_DATE_END = AS_OF_DATE

US_COLUMNS = {
    "order_id": "order_id",
    "customer_id": "customer_id",
    "customer_email": "customer_email",
    "order_date": "order_date",
    "status": "order_status",
    "product_id": "sku",
    "line_number": "line_item_no",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "currency": "currency",
    "tax_amount": "sales_tax",
    "shipping_country": "ship_to_country",
}

EU_COLUMNS = {
    "order_id": "order_no",
    "customer_id": "client_ref",
    "customer_email": "contact_email",
    "order_date": "placed_on",
    "status": "fulfillment_state",
    "product_id": "product_code",
    "line_number": "item_seq",
    "quantity": "units",
    "unit_price": "price_each",
    "currency": "currency_code",
    "tax_amount": "vat_amount",
    "shipping_country": "ship_to_country",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orders-per-region",
        type=int,
        default=500,
        help="Number of orders to generate in each regional shard (default: 500).",
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
        help="Directory for US_ORDERS.csv and EU_ORDERS.csv.",
    )
    args = parser.parse_args()
    if args.orders_per_region < 1:
        parser.error("--orders-per-region must be at least 1")
    return args


def build_order(
    order_number: int,
    region: str,
    fake: Faker,
    rng: random.Random,
    email_by_index: dict[int, str],
    eu_client_ref_by_index: dict[int, str],
) -> list[dict[str, Any]]:
    """Create one order and its item rows at order-item grain."""
    order_id = f"{region}-{order_number:06d}"
    customer_index = rng.randint(1, 350)
    if region == "EU":
        # EU mints its own independent sequential ID, assigned in first-encounter
        # order within this generation run -- no digit relationship to
        # customer_index or to the US shard's customer_id.
        customer_id = eu_client_ref_by_index.setdefault(
            customer_index, f"EU-CLI-{len(eu_client_ref_by_index) + 1:06d}"
        )
    else:
        customer_id = f"CUST-{customer_index:05d}"
    customer_email = email_by_index[customer_index]
    order_date = fake.date_between(start_date=ORDER_DATE_START, end_date=ORDER_DATE_END)
    currency = "USD" if region == "US" else rng.choice(("EUR", "GBP"))
    shipping_country = "US" if region == "US" else rng.choice(("DE", "FR", "NL", "ES", "IT"))

    item_count = rng.randint(1, 4)
    rows = []
    for line_number in range(1, item_count + 1):
        quantity = rng.randint(1, 5)
        unit_price = Decimal(rng.randint(800, 25000)) / 100
        tax_rate = Decimal("0.08") if region == "US" else Decimal("0.20")
        tax_amount = (unit_price * quantity * tax_rate).quantize(Decimal("0.01"))
        rows.append(
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "customer_email": customer_email,
                "order_date": order_date.isoformat(),
                "status": "pending",
                "product_id": f"SKU-{rng.randint(1, 250):05d}",
                "line_number": line_number,
                "quantity": quantity,
                "unit_price": f"{unit_price:.2f}",
                "currency": currency,
                "tax_amount": f"{tax_amount:.2f}",
                "shipping_country": shipping_country,
            }
        )
    return rows


def apply_destructive_status_updates(rows: list[dict[str, Any]], rng: random.Random) -> None:
    """Advance statuses in place, retaining only each order's current state."""
    orders: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        orders.setdefault(row["order_id"], []).append(row)

    for order_rows in orders.values():
        for row in order_rows:
            row["status"] = "shipped"
        if rng.random() < 0.82:
            for row in order_rows:
                row["status"] = "delivered"


def project_columns(rows: list[dict[str, Any]], column_map: dict[str, str]) -> list[dict[str, Any]]:
    return [{column_map[field]: row[field] for field in column_map} for row in rows]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_extracts(orders_per_region: int, seed: int, output_dir: Path) -> None:
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    email_by_index = {person["index"]: person["email"] for person in build_identity_pool(IDENTITY_POOL_SEED)}
    eu_client_ref_by_index: dict[int, str] = {}

    for region, column_map in (("US", US_COLUMNS), ("EU", EU_COLUMNS)):
        rows = []
        for order_number in range(1, orders_per_region + 1):
            rows.extend(build_order(order_number, region, fake, rng, email_by_index, eu_client_ref_by_index))
        apply_destructive_status_updates(rows, rng)
        write_csv(output_dir / f"{region}_ORDERS.csv", project_columns(rows, column_map))


if __name__ == "__main__":
    arguments = parse_args()
    generate_extracts(arguments.orders_per_region, arguments.seed, arguments.output_dir)