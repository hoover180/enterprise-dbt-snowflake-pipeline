"""Load local synthetic source extracts into RAW tables in DEV_ANALYTICS."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a convenience, not a hard dependency
    load_dotenv = None


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STAGE_NAME = "RAW_LOAD_STAGE"

CSV_FILE_FORMAT = (
    "FILE_FORMAT = ("
    "TYPE = CSV "
    "FIELD_DELIMITER = ',' "
    "SKIP_HEADER = 1 "
    "FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
    "EMPTY_FIELD_AS_NULL = TRUE"
    ")"
)
JSON_FILE_FORMAT = "FILE_FORMAT = (TYPE = JSON)"


@dataclass(frozen=True)
class TableSpec:
    table: str
    source_file: str
    ddl_columns: str
    copy_target_columns: str
    file_format: str


CSV_SOURCES = [
    TableSpec(
        table="US_ORDERS",
        source_file="US_ORDERS.csv",
        ddl_columns="""
            order_id VARCHAR,
            customer_id VARCHAR,
            customer_email VARCHAR,
            order_date DATE,
            order_status VARCHAR,
            ship_date DATE,
            refund_date DATE,
            refund_amount NUMBER(12,2),
            sku VARCHAR,
            line_item_no NUMBER,
            quantity NUMBER,
            unit_price NUMBER(12,2),
            currency VARCHAR,
            sales_tax NUMBER(12,2),
            ship_to_country VARCHAR
        """,
        copy_target_columns="",
        file_format=CSV_FILE_FORMAT,
    ),
    TableSpec(
        table="EU_ORDERS",
        source_file="EU_ORDERS.csv",
        ddl_columns="""
            order_no VARCHAR,
            client_ref VARCHAR,
            contact_email VARCHAR,
            placed_on DATE,
            fulfillment_state VARCHAR,
            ship_date DATE,
            refund_date DATE,
            refund_amount NUMBER(12,2),
            product_code VARCHAR,
            item_seq NUMBER,
            units NUMBER,
            price_each NUMBER(12,2),
            currency_code VARCHAR,
            vat_amount NUMBER(12,2),
            ship_to_country VARCHAR
        """,
        copy_target_columns="",
        file_format=CSV_FILE_FORMAT,
    ),
    TableSpec(
        table="CRM_CUSTOMERS",
        source_file="CRM_CUSTOMERS.csv",
        ddl_columns="""
            account_id VARCHAR,
            contact_email VARCHAR,
            name VARCHAR,
            country VARCHAR,
            created_date DATE,
            last_seen_date DATE
        """,
        copy_target_columns="",
        file_format=CSV_FILE_FORMAT,
    ),
    TableSpec(
        table="CRM_TICKETS",
        source_file="CRM_TICKETS.csv",
        ddl_columns="""
            ticket_id VARCHAR,
            account_id VARCHAR,
            category VARCHAR,
            created_date DATE,
            status VARCHAR
        """,
        copy_target_columns="",
        file_format=CSV_FILE_FORMAT,
    ),
]

JSON_SOURCE = TableSpec(
    table="RAW_WEB_EVENTS",
    source_file="CLICKSTREAM_EVENTS.json",
    ddl_columns="event_data VARIANT",
    copy_target_columns="(event_data)",
    file_format=JSON_FILE_FORMAT,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory containing the source extracts (default: repo-root data/).",
    )
    parser.add_argument(
        "--table",
        action="append",
        dest="tables",
        choices=[spec.table for spec in [*CSV_SOURCES, JSON_SOURCE]],
        help=(
            "Load only this table (repeatable). Default: load every source table. "
            "Use e.g. --table RAW_WEB_EVENTS to reload a single source without "
            "touching the others."
        ),
    )
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"Missing required environment variable {name}. "
            "See docs/dbt_profile_setup.md for the full list."
        )
    return value


def load_private_key(path: str, passphrase: str | None) -> bytes:
    key_path = Path(path).expanduser()
    if not key_path.is_file():
        sys.exit(f"Private key not found at {key_path}.")
    with key_path.open("rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=passphrase.encode() if passphrase else None,
        )
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect() -> snowflake.connector.SnowflakeConnection:
    account = require_env("SNOWFLAKE_ACCOUNT")
    user = require_env("SNOWFLAKE_USER")
    private_key_path = require_env("SNOWFLAKE_PRIVATE_KEY_PATH")
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")

    return snowflake.connector.connect(
        account=account,
        user=user,
        private_key=load_private_key(private_key_path, passphrase),
        role=os.environ.get("SNOWFLAKE_ROLE", "TRANSFORMER_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "TRANSFORM_XS"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "DEV_ANALYTICS"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "RAW"),
    )


def ensure_schema(cursor: snowflake.connector.cursor.SnowflakeCursor, schema: str) -> None:
    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def ensure_stage(cursor: snowflake.connector.cursor.SnowflakeCursor, database: str, schema: str) -> None:
    cursor.execute(f"CREATE STAGE IF NOT EXISTS {database}.{schema}.{STAGE_NAME}")


def load_table(
    cursor: snowflake.connector.cursor.SnowflakeCursor,
    database: str,
    schema: str,
    data_dir: Path,
    spec: TableSpec,
) -> None:
    local_path = data_dir / spec.source_file
    if not local_path.is_file():
        sys.exit(f"Source file not found: {local_path}")

    qualified_table = f"{database}.{schema}.{spec.table}"
    stage_path = f"@{database}.{schema}.{STAGE_NAME}/{spec.table}"

    print(f"Loading {spec.source_file} -> {qualified_table}")
    cursor.execute(f"CREATE OR REPLACE TABLE {qualified_table} ({spec.ddl_columns})")
    cursor.execute(f"PUT '{local_path.resolve().as_uri()}' {stage_path} AUTO_COMPRESS=TRUE OVERWRITE=TRUE")
    cursor.execute(
        f"COPY INTO {qualified_table} {spec.copy_target_columns} "
        f"FROM {stage_path} "
        f"{spec.file_format} "
        "ON_ERROR = 'ABORT_STATEMENT'"
    )


def main() -> None:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()
    if not args.data_dir.is_dir():
        sys.exit(f"Data directory not found: {args.data_dir}")

    conn = connect()
    try:
        database = os.environ.get("SNOWFLAKE_DATABASE", "DEV_ANALYTICS")
        schema = os.environ.get("SNOWFLAKE_SCHEMA", "RAW")

        cursor = conn.cursor()
        ensure_schema(cursor, schema)
        ensure_stage(cursor, database, schema)

        all_specs = [*CSV_SOURCES, JSON_SOURCE]
        specs = [spec for spec in all_specs if spec.table in args.tables] if args.tables else all_specs
        for spec in specs:
            load_table(cursor, database, schema, args.data_dir, spec)

        print("Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
