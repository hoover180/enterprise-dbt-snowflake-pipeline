"""Generate regional ERP order-item extracts for the reconciliation project."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from order_pool import DEFAULT_ORDERS_PER_REGION, build_order
from identity_pool import IDENTITY_POOL_SEED, build_identity_pool


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"

US_COLUMNS = {
    "order_id": "order_id",
    "customer_id": "customer_id",
    "customer_email": "customer_email",
    "order_date": "order_date",
    "status": "order_status",
    "ship_date": "ship_date",
    "refund_date": "refund_date",
    "refund_amount": "refund_amount",
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
    "ship_date": "ship_date",
    "refund_date": "refund_date",
    "refund_amount": "refund_amount",
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
        default=DEFAULT_ORDERS_PER_REGION,
        help=f"Number of orders to generate in each regional shard (default: {DEFAULT_ORDERS_PER_REGION}). "
        "Must match the value passed to clickstream.py/crm.py's own --orders-per-region for their "
        "order references to correlate against this shard -- see data_gen/order_pool.py.",
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


def build_order_rows(
    order_number: int,
    region: str,
    eu_client_ref_by_index: dict[int, str],
    email_by_index: dict[int, str],
) -> list[dict[str, Any]]:
    """Create one order's item rows at order-item grain from the shared order pool.

    All order content (customer, dates, items, status/lifecycle, refund) comes
    from order_pool.build_order() -- deterministic from (region, order_number)
    alone, independent of this script's own randomness, exactly so web/CRM can
    derive the same order without reading this script's output. This function
    only maps that shared truth onto ERP's own id-minting and column-naming
    conventions (region-specific customer_id/client_ref schemes, US/EU column
    names) -- see US_COLUMNS/EU_COLUMNS.
    """
    order = build_order(region, order_number)
    customer_index = order["customer_index"]
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

    rows = []
    for item in order["items"]:
        rows.append(
            {
                "order_id": order["order_id"],
                "customer_id": customer_id,
                "customer_email": customer_email,
                "order_date": order["order_date"].isoformat(),
                "status": order["status"],
                "ship_date": order["ship_date"].isoformat() if order["ship_date"] else "",
                "refund_date": order["refund_date"].isoformat() if order["refund_date"] else "",
                "refund_amount": f"{order['refund_amount']:.2f}" if order["refund_amount"] is not None else "",
                "product_id": item["product_id"],
                "line_number": item["line_number"],
                "quantity": item["quantity"],
                "unit_price": f"{item['unit_price']:.2f}",
                "currency": order["currency"],
                "tax_amount": f"{item['tax_amount']:.2f}",
                "shipping_country": order["shipping_country"],
            }
        )
    return rows


def project_columns(rows: list[dict[str, Any]], column_map: dict[str, str]) -> list[dict[str, Any]]:
    return [{column_map[field]: row[field] for field in column_map} for row in rows]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_extracts(orders_per_region: int, output_dir: Path) -> None:
    email_by_index = {person["index"]: person["email"] for person in build_identity_pool(IDENTITY_POOL_SEED)}

    for region, column_map in (("US", US_COLUMNS), ("EU", EU_COLUMNS)):
        eu_client_ref_by_index: dict[int, str] = {}
        rows = []
        for order_number in range(1, orders_per_region + 1):
            rows.extend(build_order_rows(order_number, region, eu_client_ref_by_index, email_by_index))
        write_csv(output_dir / f"{region}_ORDERS.csv", project_columns(rows, column_map))


if __name__ == "__main__":
    arguments = parse_args()
    generate_extracts(arguments.orders_per_region, arguments.output_dir)