# Synthetic ERP Data Specification

## Purpose

`data_gen/erp.py` creates local CSV extracts for the two regional ERP shards used in Phase 1:

- `data/US_ORDERS.csv`
- `data/EU_ORDERS.csv`

Each file is at **order-item grain**: one row represents one product line on an order. An order therefore appears once for each item it contains, and `line_item_no` or `item_seq` identifies the line within the order.

Run the generator from the repository root with:

```text
python data_gen/erp.py
```

The default is 500 orders per region, with a repeatable seed. Use `--orders-per-region`, `--seed`, or `--output-dir` to override those defaults. The script requires the `Faker` package.

## Shard schemas

Both shards contain the same underlying fields, but their source naming conventions intentionally diverge:

| Meaning             | US_ORDERS         | EU_ORDERS           |
| ------------------- | ----------------- | ------------------- |
| Order identifier    | `order_id`        | `order_no`          |
| Customer identifier | `customer_id`     | `client_ref`        |
| Order date          | `order_date`      | `placed_on`         |
| Current status      | `order_status`    | `fulfillment_state` |
| Product identifier  | `sku`             | `product_code`      |
| Line number         | `line_item_no`    | `item_seq`          |
| Quantity            | `quantity`        | `units`             |
| Unit price          | `unit_price`      | `price_each`        |
| Currency            | `currency`        | `currency_code`     |
| Tax amount          | `sales_tax`       | `vat_amount`        |
| Shipping country    | `ship_to_country` | `ship_to_country`   |

The US shard uses USD and US shipping addresses. The EU shard uses EUR or GBP and a small set of EU shipping countries. Prices and tax amounts are decimal values serialized to two decimal places.

## Injected messiness

### Regional schema drift

The two extracts deliberately use different names for most shared fields. This represents independently maintained regional ERP schemas and requires explicit mapping in staging models. The values remain structurally comparable so the later union can be tested without inventing a source-specific business rule.

### Destructive status updates

Every generated order starts with `pending`. Its same status field is then overwritten in place with `shipped`; 82% of orders are overwritten again with `delivered`. Only the final value is written to the CSV. There is no status history, update timestamp, or event table. This simulates a source system whose operational table does not provide change tracking and means downstream models cannot reconstruct when an order changed state.

Tax is computed as `unit_price * quantity * tax_rate`, using an 8% tax rate for US orders and a 20% VAT rate for EU orders.

All item rows for an order share the order's current status. This preserves a consistent order-level status while retaining item-level revenue records.

### Repeated order identifiers

Orders contain one to four items, so their order identifier repeats across rows. This is intentional: a row-count-based order grain test would be wrong, while a uniqueness test on `(order_id, line_number)` after normalization is meaningful.

### Synthetic values

Faker supplies dates, while seeded pseudo-random generation supplies customers, products, quantities, prices, tax, currencies, and item counts. The data is fictional and contains no production customer information. The default seed makes local regeneration reproducible for dbt development and tests.

## Web clickstream source

`data_gen/clickstream.py` creates a raw JSON extract for the web analytics source:

- `data/CLICKSTREAM_EVENTS.json`

The file is newline-delimited JSON: each line is one event object, matching the shape loaded into a Snowflake `VARIANT` column. Events are at event grain and include page views, cart additions, and search queries. Run the generator from the repository root with:

```text
python data_gen/clickstream.py
```

The default is 1,000 canonical events plus injected duplicates, with a repeatable seed. Use `--events`, `--seed`, or `--output-dir` to override those defaults.

### Injected messiness

#### Mid-year customer-key schema drift

Events before the fixed 2025-07-01 cutoff use `user_id` for the customer identifier. Events on or after the cutoff use `customer_global_id` instead. The identifier values are drawn from the same synthetic customer population, but the key name changes and requires normalization in staging.

#### Web-pixel retry duplicates

Approximately 5% additional events are exact copies of other events, including their `event_id`. This simulates a browser pixel retrying after a delayed or missing acknowledgement and means raw event counts are not unique-event counts.

#### Late-arriving events

Approximately 2% of canonical events are deliberately late-arriving. Their `event_timestamp` is 2 to 4 days after their `event_date`, providing source records whose arrival lags the event date. These records support the Phase 3B backfill demonstration.

The data is fictional and contains no production customer information. The default seed makes local regeneration reproducible for dbt development and tests.

## Deliberate non-messiness

This Phase 1 extract does not inject nulls, duplicate line keys, invalid dates, or mismatched totals. Those defects would test data-quality handling rather than the specific regional-sharding, destructive-update, and order-item-grain behaviors required here.
