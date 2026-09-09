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
| Customer identifier | `customer_id` (`CUST-#####`) | `client_ref` (`EU-CLI-######`, independently assigned -- see below) |
| Order date          | `order_date`      | `placed_on`         |
| Current status      | `order_status`    | `fulfillment_state` |
| Product identifier  | `sku`             | `product_code`      |
| Line number         | `line_item_no`    | `item_seq`          |
| Quantity            | `quantity`        | `units`             |
| Unit price          | `unit_price`      | `price_each`        |
| Currency            | `currency`        | `currency_code`     |
| Tax amount          | `sales_tax`       | `vat_amount`        |
| Shipping country    | `ship_to_country` | `ship_to_country`   |
| Customer email      | `customer_email`  | `contact_email`     |

The US shard uses USD and US shipping addresses. The EU shard uses EUR or GBP and a small set of EU shipping countries. Prices and tax amounts are decimal values serialized to two decimal places.

## Shared identity pool

`data_gen/identity_pool.py` exposes `build_identity_pool(seed)`, which generates the same 350 synthetic people (indexed `1`..`350`, matching the `CUST-00001`..`CUST-00350` range used by the US shard and clickstream) every time it is called with `IDENTITY_POOL_SEED` (20260904). Every source script imports this pool and looks up a person's name/email by the customer index it uses internally to represent that person -- but each source is still free to mint its own external-facing identifier from that index rather than reusing `CUST-#####` verbatim. The US ERP shard and clickstream do reuse the `CUST-#####` form directly; the EU ERP shard and CRM do not (see below).

The pool is seeded independently of any script's own `--seed` argument, so changing `--seed` reshuffles only that source's own order/account-level randomness — every source still resolves customer index `42` to the same name and email regardless of what `--seed` it was run with.

### Why ERP↔CRM join is deterministic while web is heuristic

ERP's `customer_id` (US) and `client_ref` (EU), and CRM's `account_id`, are independently assigned by each source system — there is intentionally no digit relationship between any of them, because a real ERP and a real CRM (or two regional shards of the same ERP) never share an internal ID scheme; each system mints its own primary keys for the same underlying person. Reconciling records by comparing those raw identifiers would be both unrealistic and unreliable.

The EU shard's `client_ref` is a sequential `EU-CLI-######` counter, assigned the first time a given customer index is encountered while generating the EU order stream and reused for that same customer index for the rest of that run (see `data_gen/erp.py`'s `eu_client_ref_by_index` mapping). This makes `client_ref` stable *within one deterministic generation run*, exactly like every other identifier in this project -- it is not a claim that these values would persist across independent, non-deterministic real-world extracts the way a genuine EU ERP's customer master key might.

The genuine, deterministic join key between US and EU (and between ERP and CRM) is normalized email, sourced from this shared identity pool: every source resolves the same customer index to the same email address, so a case-insensitive/whitespace-normalized match on email reliably links records across shards and systems. For Phase 5: ERP (both shards) and CRM each carry a real person-level identifier (an email address) that a real ERP and CRM plausibly both capture, so matching on it is a legitimate deterministic join rather than a coincidence of shared test data -- the same mechanism links US to EU as links ERP to CRM. Clickstream events, by contrast, only carry the `CUST-#####` index itself as `user_id`/`customer_global_id` — a convenience of this synthetic generator, not something a real web analytics pixel would know — so any resolution logic built against it should be treated as a heuristic stand-in for real-world web identity resolution (cookies, sessions, device fingerprints), not a second deterministic key.

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

## CRM source

`data_gen/crm.py` creates two related CSV extracts for the CRM source system:

- `data/CRM_CUSTOMERS.csv` — one row per account.
- `data/CRM_TICKETS.csv` — one row per support ticket.

Run the generator from the repository root with:

```text
python data_gen/crm.py
```

The default is 350 accounts and 900 tickets, with a repeatable seed. Use `--accounts`, `--tickets`, `--seed`, or `--output-dir` to override those defaults.

### Schema

`CRM_CUSTOMERS.csv` columns:

| Column           | Meaning                                    |
| ----------------- | ------------------------------------------ |
| `account_id`       | CRM's own internal account identifier      |
| `contact_email`     | Email address, sourced from the shared identity pool |
| `name`             | Account holder name, sourced from the shared identity pool |
| `country`          | Account country, inconsistently formatted (see below) |
| `created_date`      | Date the account was created in the CRM    |
| `last_seen_date`     | Date of the account's most recent activity |

`CRM_TICKETS.csv` columns: `ticket_id`, `account_id`, `category`, `created_date`, `status`.

`account_id` is CRM's own independently assigned identifier — a sequential `ACCT-#####` counter with no digit relationship to the shared identity pool's customer index or to ERP's `CUST-#####`/`client_ref` values. This mirrors the ERP↔CRM design already described above: a real CRM mints its own primary keys, and the genuine join back to the identity pool (and therefore to ERP) is `contact_email`, resolved from `build_identity_pool(IDENTITY_POOL_SEED)` by a randomly chosen customer index, exactly as `erp.py` does. `contact_email` is written unmodified from the pool, so the join on email stays exact by design.

### Injected messiness

#### Dirty country formatting

`country` is not standardized. Most rows carry a plain two-letter EU shipping code (`DE`, `FR`, `NL`, `ES`, `IT`), but the same logical country — the United States — is written inconsistently across rows, randomly chosen per row from `US`, `USA`, `United States`, and `u.s.a.`. Downstream staging cannot group or filter on `country` without first normalizing these variants to a single value.

#### Ghost accounts

Approximately 3-5% of the `account_id` values referenced in `CRM_TICKETS.csv` do not appear in `CRM_CUSTOMERS.csv` at all. These ghost account IDs are drawn from a number range immediately past the real account range, so they can never collide with a real account, and simulate accounts that were deleted from the CRM while their ticket history was retained. They are detectable with a simple anti-join of `CRM_TICKETS.csv.account_id` against `CRM_CUSTOMERS.csv.account_id`.

The data is fictional and contains no production customer information. The default seed makes local regeneration reproducible for dbt development and tests.

## Deliberate non-messiness

This Phase 1 extract does not inject nulls, duplicate line keys, invalid dates, or mismatched totals. Those defects would test data-quality handling rather than the specific regional-sharding, destructive-update, and order-item-grain behaviors required here.
