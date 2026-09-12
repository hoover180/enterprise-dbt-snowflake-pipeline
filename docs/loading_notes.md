# Raw Loading Notes

`data_gen/load_raw.py` loads the local extracts described in
`docs/synthetic_data_spec.md` into `DEV_ANALYTICS.RAW` in Snowflake. This
doc records the exact table names and shapes it creates, since the Phase 3
staging models reference them by name.

## Prerequisites

1. Generate the extracts locally (if you haven't already):
   ```text
   python data_gen/erp.py
   python data_gen/crm.py
   python data_gen/clickstream.py
   ```
   All three share order/transaction identity via `data_gen/order_pool.py`
   (see `docs/synthetic_data_spec.md`'s "Shared order pool" section) --
   if you override `--orders-per-region` on one, pass the same value to
   the other two, or their order references won't correlate.
2. Set up Snowflake key-pair auth and the environment variables in
   `docs/dbt_profile_setup.md` ("Environment variables for
   `data_gen/load_raw.py`").
3. `pip install -r requirements.txt`

## Running

```text
python data_gen/load_raw.py
```

Each run does, against `DEV_ANALYTICS` (never `STAGE_ANALYTICS`/
`PROD_ANALYTICS` — see `docs/dbt_profile_setup.md`):

1. `CREATE SCHEMA IF NOT EXISTS RAW`
2. `CREATE STAGE IF NOT EXISTS RAW.RAW_LOAD_STAGE` (a Snowflake internal
   named stage, one subdirectory per table)
3. Per source file: `CREATE OR REPLACE TABLE`, `PUT` the local file to its
   stage subdirectory, then `COPY INTO` the table.

`CREATE OR REPLACE TABLE` makes the script idempotent/rerunnable during
local dev — each run fully replaces the raw tables from the current local
extracts rather than appending to them. This is a landing-zone loader, not
an incremental pipeline; incremental/append logic belongs in dbt models
that read from these raw tables, not in the loader.

## Raw table shapes

All tables live in `DEV_ANALYTICS.RAW`. Column names and order match the
source file headers documented in `docs/synthetic_data_spec.md` exactly —
no renaming happens at load time; renaming to shared/canonical field names
is staging-layer work.

### `DEV_ANALYTICS.RAW.US_ORDERS`

Source: `data/US_ORDERS.csv`

| Column           | Type           |
| ----------------- | --------------- |
| `order_id`         | VARCHAR         |
| `customer_id`      | VARCHAR         |
| `customer_email`   | VARCHAR         |
| `order_date`       | DATE            |
| `order_status`     | VARCHAR         |
| `ship_date`        | DATE            |
| `refund_date`      | DATE            |
| `refund_amount`    | NUMBER(12,2)    |
| `sku`              | VARCHAR         |
| `line_item_no`     | NUMBER          |
| `quantity`         | NUMBER          |
| `unit_price`       | NUMBER(12,2)    |
| `currency`         | VARCHAR         |
| `sales_tax`        | NUMBER(12,2)    |
| `ship_to_country`  | VARCHAR         |

### `DEV_ANALYTICS.RAW.EU_ORDERS`

Source: `data/EU_ORDERS.csv`

| Column             | Type           |
| ------------------- | --------------- |
| `order_no`           | VARCHAR         |
| `client_ref`         | VARCHAR         |
| `contact_email`      | VARCHAR         |
| `placed_on`          | DATE            |
| `fulfillment_state`  | VARCHAR         |
| `ship_date`          | DATE            |
| `refund_date`        | DATE            |
| `refund_amount`      | NUMBER(12,2)    |
| `product_code`       | VARCHAR         |
| `item_seq`           | NUMBER          |
| `units`              | NUMBER          |
| `price_each`         | NUMBER(12,2)    |
| `currency_code`      | VARCHAR         |
| `vat_amount`         | NUMBER(12,2)    |
| `ship_to_country`    | VARCHAR         |

### `DEV_ANALYTICS.RAW.CRM_CUSTOMERS`

Source: `data/CRM_CUSTOMERS.csv`

| Column            | Type    |
| ------------------ | -------- |
| `account_id`        | VARCHAR  |
| `contact_email`     | VARCHAR  |
| `name`              | VARCHAR  |
| `country`           | VARCHAR  |
| `created_date`      | DATE     |
| `last_seen_date`    | DATE     |

### `DEV_ANALYTICS.RAW.CRM_TICKETS`

Source: `data/CRM_TICKETS.csv`

| Column            | Type    |
| ------------------ | -------- |
| `ticket_id`         | VARCHAR  |
| `account_id`        | VARCHAR  |
| `category`          | VARCHAR  |
| `created_date`      | DATE     |
| `status`            | VARCHAR  |
| `claimed_amount`    | NUMBER(12,2) |
| `order_reference`   | VARCHAR  |

### `DEV_ANALYTICS.RAW.RAW_WEB_EVENTS`

Source: `data/CLICKSTREAM_EVENTS.json` (newline-delimited JSON, one event
object per line)

| Column        | Type      |
| -------------- | ---------- |
| `event_data`    | VARIANT    |

One column holding the entire event object, matching how a real landing
zone holds semi-structured data before staging models parse it — this
table is deliberately schema-less at the raw layer. Every field described
in `docs/synthetic_data_spec.md`'s clickstream section (`event_id`,
`event_type`, `event_timestamp`, the `user_email`/`customer_global_email`
schema drift, etc.) lives inside `event_data` and must be extracted with
`event_data:field_name` (or `::type` casts) in the staging model — nothing
is flattened at load time.

## Not the staging layer's job here

This loader does not deduplicate the ~5% pixel-retry duplicate events,
resolve the `user_email`/`customer_global_email` naming drift, normalize
CRM's inconsistent `country` values, or reconcile ERP's regional
column-naming drift. All of that is staging-model logic (Phase 3) that
operates on these raw tables — the loader's only job is getting the source
files into Snowflake unmodified.

`data_gen/load_raw.py --table RAW_WEB_EVENTS` reloads only the web source
table (repeatable `--table` flag) without touching `US_ORDERS`/`EU_ORDERS`/
`CRM_CUSTOMERS`/`CRM_TICKETS` — useful when only `data/CLICKSTREAM_EVENTS.json`
was regenerated and the other three sources' fixed `AS_OF_DATE` windows are
untouched.
