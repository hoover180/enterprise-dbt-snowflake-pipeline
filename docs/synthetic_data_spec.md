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

The default is 500 orders per region. Use `--orders-per-region` or `--output-dir` to override those defaults. The script requires the `Faker` package. **`erp.py` no longer accepts a `--seed`** -- since Phase 5B pre-work (Issue #42), every order-level fact (customer, dates, items, status/lifecycle, refund) comes from the shared, seed-independent order pool (`data_gen/order_pool.py`, see below), so there is no remaining script-local randomness left for `erp.py` to seed. `--orders-per-region` must be passed identically to `clickstream.py`'s and `crm.py`'s own `--orders-per-region` for their order/transaction references to correlate against this shard -- see "Shared order pool" below.

## Shard schemas

Both shards contain the same underlying fields, but their source naming conventions intentionally diverge:

| Meaning             | US_ORDERS         | EU_ORDERS           |
| ------------------- | ----------------- | ------------------- |
| Order identifier    | `order_id`        | `order_no`          |
| Customer identifier | `customer_id` (`CUST-#####`) | `client_ref` (`EU-CLI-######`, independently assigned -- see below) |
| Order date          | `order_date`      | `placed_on`         |
| Current status      | `order_status`    | `fulfillment_state` |
| Ship (recognition) date | `ship_date`   | `ship_date`         |
| Refund date         | `refund_date`     | `refund_date`       |
| Refund amount       | `refund_amount`   | `refund_amount`     |
| Product identifier  | `sku`             | `product_code`      |
| Line number         | `line_item_no`    | `item_seq`          |
| Quantity            | `quantity`        | `units`             |
| Unit price          | `unit_price`      | `price_each`        |
| Currency            | `currency`        | `currency_code`     |
| Tax amount          | `sales_tax`       | `vat_amount`        |
| Shipping country    | `ship_to_country` | `ship_to_country`   |
| Customer email      | `customer_email`  | `contact_email`     |

The US shard uses USD and US shipping addresses. The EU shard uses EUR or GBP and a small set of EU shipping countries. Prices and tax amounts are decimal values serialized to two decimal places. `ship_date`/`refund_date`/`refund_amount` are blank (loaded as `NULL`) whenever they don't apply to an order's current status -- see "Order status lifecycle and revenue recognition" below.

## Shared order pool

`data_gen/order_pool.py` exposes `build_order(region, order_number)`, the order-level analogue of the shared identity pool below: a pure function of its two arguments plus the fixed `ORDER_POOL_SEED` (20260911), returning one order's full deterministic truth -- its customer, items, dollar amounts, lifecycle status, ship/refund timing, checkout promo outcome, and whether it has a corresponding web purchase event. `erp.py`, `clickstream.py`, and `crm.py` each call it directly; none of them reads another script's output file. This exists because Phase 5B pre-work (Issue #42) needs ERP, web, and CRM to reference the *same* order/transaction identifiers -- something none of the three needed before (they only ever shared *customer* identity, via the identity pool). See ADR-009 in `docs/data_modeling_decisions.md` for why a shared deterministic pool, not a file-read dependency between scripts, is the right mechanism.

`order_id_for(region, order_number)` (`f"{region}-{order_number:06d}"`) is the order identifier itself -- a pure string formula requiring no randomness, reused verbatim as web's `transaction_id` and as the value a CRM refund ticket's `order_reference` is agent-typed against. `erp.py` only ever emits rows for `order_number` in `1..orders_per_region`; any higher `order_number` is, by construction, never a real order. `ghost_order_number(orders_per_region, rng)` picks one of those never-real numbers (from a 200-wide range immediately past the real one, per region) using the *caller's own* locally-seeded `rng` -- unlike `build_order()` itself, this doesn't need to be pool-deterministic, since a web orphan transaction id and a CRM nonexistent order reference are independent phenomena that don't need to correlate with each other.

**`--orders-per-region` must be passed identically to `erp.py`, `clickstream.py`, and `crm.py`** for their order references to actually correlate -- if the three scripts disagree on this value, web/CRM will treat some real ERP orders as ghosts (or vice versa) purely from an inconsistent universe size, not from any of the deliberate injected-messiness mechanisms below.

## Shared identity pool

`data_gen/identity_pool.py` exposes `build_identity_pool(seed)`, which generates the same 350 synthetic people (indexed `1`..`350`, matching the `CUST-00001`..`CUST-00350` range used by the US shard and clickstream) every time it is called with `IDENTITY_POOL_SEED` (20260904). Every source script imports this pool and looks up a person's name/email by the customer index it uses internally to represent that person -- but each source is still free to mint its own external-facing identifier from that index rather than reusing `CUST-#####` verbatim. The US ERP shard and clickstream do reuse the `CUST-#####` form directly; the EU ERP shard and CRM do not (see below).

Each person also carries `collision_target_index`, `None` for almost everyone but pointing at another person's index for a small, designated set of "collision source" people -- see "Sparse, tiered-dirty customer identity capture" below for how `clickstream.py` uses it to create the identity collision tier. This is a post-hoc mutation applied after the base 350 people are built, using its own RNG independent of the Faker instance that generated names/emails, so it never perturbs the base name/email sequence for any index not chosen as a collision target.

The pool is seeded independently of any script's own `--seed` argument, so changing `--seed` reshuffles only that source's own order/account-level randomness — every source still resolves customer index `42` to the same name and email regardless of what `--seed` it was run with.

### Why ERP↔CRM join is deterministic while web is heuristic

ERP's `customer_id` (US) and `client_ref` (EU), and CRM's `account_id`, are independently assigned by each source system — there is intentionally no digit relationship between any of them, because a real ERP and a real CRM (or two regional shards of the same ERP) never share an internal ID scheme; each system mints its own primary keys for the same underlying person. Reconciling records by comparing those raw identifiers would be both unrealistic and unreliable.

The EU shard's `client_ref` is a sequential `EU-CLI-######` counter, assigned the first time a given customer index is encountered while generating the EU order stream and reused for that same customer index for the rest of that run (see `data_gen/erp.py`'s `eu_client_ref_by_index` mapping). This makes `client_ref` stable *within one deterministic generation run*, exactly like every other identifier in this project -- it is not a claim that these values would persist across independent, non-deterministic real-world extracts the way a genuine EU ERP's customer master key might.

This isn't only a cosmetic prefix difference -- confirmed directly, not assumed: `US_ORDERS.customer_id` numerically encodes `customer_index` (`CUST-00042` is always customer index 42), while `EU_ORDERS.client_ref`'s numeric part encodes *first-encounter order within the EU generation run*, which is unrelated to `customer_index`. `EU-CLI-000001` is whichever customer the EU order loop happened to reach first -- checked against the current default dataset, that's `peterchurch@example.com`, a completely different real person from `CUST-00001`'s `melissanorman@example.org`. A consumer that assumed the two shards' identifier schemes were secretly comparable (e.g. stripping prefixes and comparing the numeric remainder) would silently join the wrong people.

The genuine, deterministic join key between US and EU (and between ERP and CRM) is normalized email, sourced from this shared identity pool: every source resolves the same customer index to the same email address, so a case-insensitive/whitespace-normalized match on email reliably links records across shards and systems. For Phase 5: ERP (both shards) and CRM each carry a real person-level identifier (an email address) that a real ERP and CRM plausibly both capture, so matching on it is a legitimate deterministic join rather than a coincidence of shared test data -- the same mechanism links US to EU as links ERP to CRM. Clickstream events, by contrast, never carry a `CUST-#####`-style index at all -- see "Web clickstream source" below. Where a web event carries an identity signal, it's an email captured for the same real-world reason ERP/CRM would capture one, but sparsely and imperfectly (a real web pixel only sometimes captures identity, and what it captures is sometimes mistyped or inconsistently cased) -- so resolving it is a genuine heuristic problem (fuzzy matching against ERP/CRM's canonical email), not a second deterministic key. See ADR-008 in `docs/data_modeling_decisions.md` for the history: this was originally built as a direct `CUST-#####` copy, which was a Phase 1 gap relative to this stated design and was fixed ahead of Phase 5A's identity-resolution work.

## Injected messiness

### Regional schema drift

The two extracts deliberately use different names for most shared fields. This represents independently maintained regional ERP schemas and requires explicit mapping in staging models. The values remain structurally comparable so the later union can be tested without inventing a source-specific business rule.

### Order status lifecycle and revenue recognition

Every generated order follows one destructive (history-free, only-the-final-value-persists) lifecycle: `pending` → `shipped` → `delivered` → `returned`, with `cancelled` reachable at any point before shipping as a distinct terminal state -- an order that was never fulfilled at all, not one that was fulfilled and then reversed. As before, there is no status history, update timestamp, or event table; only the final value is written to the CSV, and all item rows for an order share it.

- **`cancelled`** (4% of orders): rolled before shipping ever happens. `ship_date`/`refund_date`/`refund_amount` are all blank.
- **`pending`**: an order placed close enough to `AS_OF_DATE` (2025-12-31) that it wouldn't have shipped yet as of the snapshot. A small, real population (7/1,000 in the current default dataset) -- not vacuous, but deliberately rare, since most of the 365-day order window has had time to ship. `ship_date` is blank.
- **`shipped`** / **`delivered`**: shipped orders get a `ship_date` 1-5 days after `order_date` -- this is the **recognition timestamp**, distinct from `order_date` (see policy below). 82% of shipped, non-returned orders progress to `delivered`; the rest remain `shipped` (in transit as of the snapshot).
- **`returned`** (19.3% of orders): only reachable from a shipped order. `refund_date` is 5-45 days after `ship_date` -- **deliberately not clamped to the same calendar month or even the same fixed `AS_OF_DATE` window as `order_date`/`ship_date`** (see "Fixed date window, extended for refunds" below). `refund_amount` is the order's actual recognized total (post any validated checkout promo -- see "Checkout promo / amount-divergence mechanism" below), i.e. a full-order refund; partial/line-level returns are out of scope for this dataset.

Tax is still computed as `unit_price * quantity * tax_rate` (8% US, 20% EU), applied per line item to whatever `unit_price` that line ends up with after any validated promo discount.

**Return and cancellation rates are real, cited figures, not blanket guesses.** NRF/Happy Returns' "2025 Retail Returns Landscape" puts the 2025 online-retail return rate at 19.3% (up from 17.6% in 2024) -- the most-cited current benchmark for general-merchandise online returns, and considerably higher than a flat 5-10% assumption. This dataset uses 19.3% as a single blended rate: it has no product-category concept, and inventing one solely to size a return rate would be exactly the premature abstraction this project's ADRs (e.g. ADR-006's flat-list country var) avoid building ahead of need. Cancellation uses a separate, smaller rate (4%), inside the 2-8% band industry sources report for healthy ecommerce operations (Amazon, held up as the operational benchmark, targets under 2.5%) -- a cancellation is a distinct phenomenon (never fulfilled) from a return (fulfilled, then reversed), so the two rates are independent, not derived from one another.

### Revenue-recognition policy (the generator's actual contract)

**`recognized_net_revenue` for period P = ERP line-item amounts (`unit_price * quantity + tax_amount`) whose `ship_date` falls in P, minus `refund_amount` for any order whose `refund_date` falls in P. Cancelled orders, and orders that have not yet shipped (`pending`), are excluded entirely.**

Recognition is keyed on **`ship_date`, not `order_date`**: an order-create-based policy would recognize revenue at the moment of purchase, which would shrink or eliminate the real cutoff problem this dataset exists to create (a customer completing checkout in one period whose order isn't recognized as shipped revenue until a later one). Ship-based recognition is the one that produces a genuine timing gap, and it's what the generated data actually supports -- checked directly, not assumed: of the 195 returned orders in the current default dataset, 146 (74.9%) have `refund_date` falling in a different calendar month than `ship_date`. This policy statement is this branch's actual contract for the generator, not documentation written after the fact -- `order_pool.build_order()`'s lifecycle logic was built to conform to it (see the `ship_date`/`refund_date` construction above), and every number in this section and in ADR-009 was verified against the real generated/loaded data.

### Fixed date window, extended for refunds

`order_date` (and `ship_date`, `checkout_date` on web purchase events) stay inside the existing fixed window anchored to `AS_OF_DATE = 2025-12-31` (365 days back through that date). `refund_date` is deliberately allowed to extend up to 45 days **past** `AS_OF_DATE` (through 2026-02-14, `order_pool.REFUND_WINDOW_END`) -- clamping every refund to the same as-of snapshot as new order intake would silently erase the later-period reversal this whole branch exists to create; a return's refund is the tail of a process that can genuinely land after the snapshot that captured the order that started it. CRM refund tickets' `created_date` (see below) uses the same extended bound. This is a deliberate, documented widening of the fixed-window discipline established for `order_date`/`created_date` elsewhere, not a reintroduction of `date.today()` drift -- both bounds are still fixed constants.

### Checkout promo / amount-divergence mechanism

A documented, real arithmetic mechanism -- not noise sampled from a distribution -- creates the amount mismatches web/ERP reconciliation needs to find. 18% of orders have a checkout-time promotional discount (10% off) applied. Of those, 30% fail ERP's backend eligibility validation: the discount is honored at checkout (what the customer saw, and what web's `checkout_total` reflects) but ERP charges full price (what the stored line items, and therefore `refund_amount` if returned, actually reflect). The remaining 70% validate, and the discount is baked into the stored line items themselves, so ERP's recognized amount and web's checkout total genuinely agree. See "Web clickstream source" below for how this surfaces on the web side, and the freeze-baseline ADR in `docs/data_modeling_decisions.md` for the measured breakdown.

### Checkout-time shipping estimate -- a second, independent amount-divergence mechanism

The promo mechanism above is real and mechanism-driven, but freeze-gate peer review found it thin on its own: a single divergence source means every web/ERP amount mismatch traces back to the same root cause. `order_pool.SHIP_ESTIMATE_RATE` (20% of orders) adds a structurally different one: `checkout_total` bakes in a flat, client-estimated shipping charge (`order_pool.SHIP_ESTIMATE_FLAT`, \$8.99 US / \€11.99 EU) that ERP's `refund_amount`/recognized total never carries at all -- this dataset has no shipping-revenue line item in ERP's schema. Both figures are locally correct under their own definition of "the total"; this is the genuine "both sides right, different scope" reconciliation case, not a validation failure. It's independent of the promo mechanism -- an order can have neither, either, or both (7 orders in the current default dataset have both, confirmed directly). See the freeze-baseline ADR for the measured rate, dollar impact, and worked examples.

### Repeated order identifiers

Orders contain one to four items, so their order identifier repeats across rows. This is intentional: a row-count-based order grain test would be wrong, while a uniqueness test on `(order_id, line_number)` after normalization is meaningful.

### Synthetic values

Faker supplies dates, while seeded pseudo-random generation supplies customers, products, quantities, prices, tax, currencies, and item counts (via the shared, seed-independent order pool -- see above). The data is fictional and contains no production customer information.

## Web clickstream source

`data_gen/clickstream.py` creates a raw JSON extract for the web analytics source:

- `data/CLICKSTREAM_EVENTS.json`

The file is newline-delimited JSON: each line is one event object, matching the shape loaded into a Snowflake `VARIANT` column. Events are at event grain and include page views, cart additions, search queries, and purchases. Run the generator from the repository root with:

```text
python data_gen/clickstream.py
```

The default is 9,600 canonical (page_view/cart_addition/search_query) events plus injected duplicates, plus purchase events (sized by real ERP order coverage, not independently -- see "Purchase events" below). Use `--events`, `--seed`, `--orders-per-region`, or `--output-dir` to override the defaults; `--orders-per-region` must match `erp.py`'s own value (see "Shared order pool" above).

#### Purchase events

`purchase` is a rare event type layered on top of the original three, correlated to real ERP orders via the shared order pool. Its volume is emergent, not an independently-dialed rate: `order_pool.WEB_MATCH_RATE` (35% of real ERP orders get a matching purchase event -- see "Order coverage gaps" below) times the default 1,000 real orders is ~350 matched events, plus a documented 12% orphan share (`WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS`) brings the total to ~400. `DEFAULT_EVENT_COUNT` (9,600, up from this generator's original 1,000) was sized specifically so that ~400 purchase events land at a "low single digits" share (~4.0%) of the combined total (~10,000) once purchase events are added -- deliberately not an even split with the other three event types. A realistic global ecommerce conversion rate is 2.5-3% (Contentsquare, Q3 2025); 4% here is close to that while accounting for this generator's event-level (not session-level) approximation of "conversion." Each purchase event carries:

- `transaction_id` -- the order_id of the order pool order it corresponds to (real or ghost -- see "Order coverage gaps").
- `checkout_total` -- the customer-facing total computed at checkout time (see "Amount-divergence mechanism" in the ERP section above for how/when this can diverge from ERP's actual recognized amount).
- `currency` -- matching that order's currency, so a future consumer comparing amounts knows what they're comparing.

The purchase timestamp uses the order's own `order_date` (checkout time), never `ship_date` -- free to fall in an earlier calendar month than ERP's ship-based recognition, which is the actual source of the cutoff problem (not amount drift). Purchase events carry an identity-capture signal exactly like the other three event types (see "Sparse, tiered-dirty customer identity capture" below), at a distinctly higher rate (90% -- a completed checkout collects a contact email for the receipt almost every time, more certain than `cart_addition`, which a browser can still abandon before handing over contact details). Purchase events are **not** subject to the pixel-retry-duplicate or late-arrival mechanics below -- those remain scoped, unchanged, to the original three event types.

#### Order coverage gaps

Coverage is imperfect in both directions, at distinct, documented rates -- neither is small, and they don't need to be equal:

- **35% of real ERP orders have a matching purchase event** (`order_pool.WEB_MATCH_RATE`); the other 65% don't -- representing phone/in-store/ad-blocked-or-declined-pixel channels a general-merchandise omnichannel retailer's web analytics never observes. This is deliberately the majority of orders, not a small tail: a real retailer's other-channel/missed-pixel share of order volume is realistically substantial.
- **12% of all purchase events reference a transaction_id with no real ERP order at all** (`order_pool.WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS`) -- representing a checkout that failed payment before the order ever persisted in ERP. These orphan transaction_ids are drawn from `order_pool.ghost_order_number()`'s never-real range (200-wide per region -- wide enough that independent random draws for the default ~45-50 orphans per run don't collide with each other via the birthday paradox, checked directly).

See ADR-009 for the measured breakdown against the current default dataset.

### Injected messiness

#### Sparse, tiered-dirty customer identity capture

Unlike ERP and CRM, clickstream events do not carry a stable customer identifier by default -- most web traffic is anonymous, and a real analytics pixel only captures a customer identity signal when something in the page actually surfaces one (a logged-in session, an account-linked cart, a manually entered checkout email). `data_gen/clickstream.py` models this with a per-event-type capture probability rather than a uniform fraction, since different event types plausibly carry identity at very different rates:

| Event type      | Capture rate | Why |
| ---------------- | ------------ | --- |
| `page_view`       | 8%           | Ordinary browsing. Only carries identity when it happens inside an already-authenticated session -- most page views don't. |
| `search_query`     | 8%           | Same population as `page_view`: searching doesn't require authentication, so there's no reason for it to differ. |
| `cart_addition`    | 55%          | Materially more likely, since adding to cart is the step closest to checkout -- an account-linked cart or a guest-checkout email capture (for retargeting/abandoned-cart flows) is common here -- but still well under certainty, since plenty of carts are built by browsers who never authenticate or leave contact details. |

Against the default 1,000-event run, this produces roughly a 14% overall identity-capture rate (measured directly against the current generated extract, not assumed).

When a signal is captured, its value is drawn from the same shared identity pool (`data_gen/identity_pool.py`) ERP/CRM use, but is not always clean -- mirroring ADR-006's reasoned, tiered approach to CRM's country dirtiness rather than uniform randomized noise. Among captured signals:

- **~55% exact** -- byte-for-byte identical to the customer's canonical email. Represents identity captured programmatically from an account record (e.g. a logged-in session's stored email), which a human never retyped.
- **~25% Tier 1 (trivial normalization)** -- casing or whitespace noise (uppercased, capitalized local-part, or leading/trailing whitespace) that any reasonable lowercase-and-trim normalization step resolves. Represents a human typing or pasting the email into a guest-checkout field.
- **~20% Tier 2 (genuine typo)**, most of which is a **non-colliding** single-character edit (adjacent-character transposition, a dropped character, or a substitution with a keyboard-adjacent key) on the local part only, requiring real fuzzy matching, not just normalization. Represents a fat-fingered manual entry. Bounded to one edit on the local part (domain never touched), with a collision-avoidance retry against every other real email in the pool, so this specific path can't accidentally land on a different real customer's email.
- **Identity collision tier**, carved out of the tier-2 population above rather than a fifth independent rate: `identity_pool.py` designates a small, bounded, documented set of "collision pairs" (`COLLISION_PAIR_COUNT`, 40 people / 20 pairs, ~11.4% of the 350-person pool) where one person's ("source") tier-2 distortion is deliberately redirected (`clickstream.py`'s `COLLISION_RATE_WITHIN_TIER2`) to land on their designated partner's ("target") REAL canonical email -- not a synthetic near-miss nobody owns. Both people are entirely real, independently-named synthetic customers; only the target's email string is constructed (a single transposition of the source's real email, using the same edit primitive as the ordinary tier-2 typo) rather than drawn fresh from Faker, so it plausibly reads as a fat-fingered typo of their partner's address. Against the current default dataset: 44 of 356 tier-2-eligible events (12.4%) actually realize a collision (26 of the up-to-40 designated source people happened to draw a tier-2 event at all), spanning 26 distinct source/target pairs -- confirmed directly that every realized collision's observed value equals a genuinely different real customer's canonical email, never the source's own. This is deliberately a minority of the typo tier, not the majority: most tier-2 typos remain ordinary, non-colliding near-misses of the true customer, which a fuzzy matcher should resolve correctly -- the collision tier exists to give a future resolver a real, bounded "confident wrong match" case to be graded against, not to make ambiguity the norm.

`data_gen/clickstream.py` also writes `data/CLICKSTREAM_IDENTITY_TRUTH.csv`, a per-event sidecar recording which customer each event truly belongs to and how (if at all) its identity signal was captured/distorted -- generated before dirtying, since the dirtying is designed to not be reversible from the output alone. This file is not loaded into `RAW_WEB_EVENTS` (see `docs/loading_notes.md`); it exists purely so `data_gen/build_match_truth.py` can build `dbt/seeds/seed_match_truth.csv` without needing to reverse-engineer ground truth from deliberately-dirty data. See ADR-008 in `docs/data_modeling_decisions.md` for the measured match-rate breakdown this design produces and why it matters for Phase 5A.

The sidecar's `match_status` column makes the collision tier scoreable without needing to re-derive it from `dirt_tier`: blank when nothing was captured, `match` for exact/tier1/ordinary-tier2 (a resolver should map this back to the true customer), `possible_collision` for the designed collision cases (a resolver that merges this to the OTHER real customer whose email it matches is making a false merge, not a hit -- a future scoring pass can compute a false-merge rate from this column, not just recall).

#### Mid-year customer-key schema drift

Events before the fixed 2025-07-01 cutoff use `user_email` for the captured identity field, when one is captured at all. Events on or after the cutoff use `customer_global_email` instead. This is the same field-name drift the original design had (then named `user_id`/`customer_global_id`) -- only the field names and the content they carry changed, not the schema-evolution story itself: a source system renaming a field mid-year is orthogonal to what type of value that field holds. An event carries at most one of the two keys, never both, and (unlike the pre-fix version of this field) carries neither on the majority of rows where no identity signal was captured at all.

#### Web-pixel retry duplicates

Approximately 5% additional events are exact copies of other events, including their `event_id`. This simulates a browser pixel retrying after a delayed or missing acknowledgement and means raw event counts are not unique-event counts.

#### Late-arriving events

Every event carries three time fields: `event_date` and `event_timestamp` always reflect the true, unshifted moment the event occurred, and a separate `ingested_at` field records when the record was actually ingested. For most events `ingested_at` sits at or a few seconds/minutes after `event_timestamp`, simulating near-immediate ingestion.

Approximately 2% of canonical events are deliberately late-arriving: for these, `ingested_at` is 2 to 4 days after `event_timestamp`, while `event_date` and `event_timestamp` are left exactly as they would be for an on-time event. Late arrival is therefore represented purely by the gap between `ingested_at` and `event_timestamp` -- it is never modeled by displacing `event_timestamp` itself, since doing so would make a late-arriving event indistinguishable from an event that simply happened later. These records support the Phase 3B backfill demonstration.

The data is fictional and contains no production customer information. The default seed makes local regeneration reproducible for dbt development and tests.

## CRM source

`data_gen/crm.py` creates two related CSV extracts for the CRM source system:

- `data/CRM_CUSTOMERS.csv` — one row per account.
- `data/CRM_TICKETS.csv` — one row per support ticket.

Run the generator from the repository root with:

```text
python data_gen/crm.py
```

The default is 350 accounts and 900 general tickets, plus refund tickets sized by real ERP return volume (see "Refund tickets" below), with a repeatable seed. Use `--accounts`, `--tickets`, `--seed`, `--orders-per-region`, or `--output-dir` to override those defaults; `--orders-per-region` must match `erp.py`'s own value (see "Shared order pool" above).

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

`CRM_TICKETS.csv` columns: `ticket_id`, `account_id`, `category`, `created_date`, `status`, `claimed_amount`, `order_reference`. `claimed_amount`/`order_reference` are blank (loaded as `NULL`) for every category except `refund` -- the same category-conditional-nullability pattern web's event-type-specific fields already use.

`account_id` is CRM's own independently assigned identifier — a sequential `ACCT-#####` counter with no digit relationship to the shared identity pool's customer index or to ERP's `CUST-#####`/`client_ref` values. This mirrors the ERP↔CRM design already described above: a real CRM mints its own primary keys, and the genuine join back to the identity pool (and therefore to ERP) is `contact_email`, resolved from `build_identity_pool(IDENTITY_POOL_SEED)` by a randomly chosen customer index, exactly as `erp.py` does. `contact_email` is written unmodified from the pool, so the join on email stays exact by design.

### Injected messiness

#### Dirty country formatting

`country` is not standardized. Most rows carry a plain two-letter EU shipping code (`DE`, `FR`, `NL`, `ES`, `IT`), but the same logical country — the United States — is written inconsistently across rows, randomly chosen per row from `US`, `USA`, `United States`, and `u.s.a.`. Downstream staging cannot group or filter on `country` without first normalizing these variants to a single value.

#### Ghost accounts

Approximately 3-5% of the `account_id` values referenced in `CRM_TICKETS.csv` do not appear in `CRM_CUSTOMERS.csv` at all. These ghost account IDs are drawn from a number range immediately past the real account range (including any deliberate duplicate accounts -- see "Duplicate CRM accounts" below), so they can never collide with a real account, and simulate accounts that were deleted from the CRM while their ticket history was retained. They are detectable with a simple anti-join of `CRM_TICKETS.csv.account_id` against `CRM_CUSTOMERS.csv.account_id`.

#### Duplicate CRM accounts

Found during freeze-gate investigation: `build_account()` used to draw its customer via an independent `rng.randint(1, 350)` per account row -- a sample *with* replacement over the 350-person pool, which was never a deliberate mechanism. Against the default 350-account run this meant ~125 of the 350 real people had **no** CRM account at all, and ~93 had 2-4 accounts purely by chance, none of it documented or bounded. Accounts are now assigned **one-to-one**: a shuffled, without-replacement mapping over the 350-person pool (so CRM coverage of the identity pool is what the rest of this document already implied it was), with `account_number` uncorrelated to `customer_index` (no digit relationship, matching this project's existing "each system mints its own keys" precedent).

On top of that clean 1:1 baseline, `crm.py`'s `DUPLICATE_ACCOUNT_RATE` (3% of the 350 people, `dirty_name_variant()`) gives a small, deliberate, bounded population a genuine **second** account for the same real person -- a plausible "customer signed up twice," with a name-formatting/casing variant (uppercase, last-name-first, or first-initial) mirroring `dirty_country()`'s reasoned-variant approach rather than random character noise. Against the current default dataset: 360 total accounts covering all 350 people (0 with zero CRM presence, versus ~125 before the fix), 10 people (2.9%) with exactly 2 accounts each -- confirmed directly, both locally and against `DEV_ANALYTICS.RAW.CRM_CUSTOMERS`. `dbt/seeds/seed_match_truth.csv`'s existing `crm_account_id` column (semicolon-joined per canonical email) already makes this scoreable with no format change: a person with 2 accounts shows both ids in that one field.

#### Refund tickets

`category == "refund"` tickets extend the ghost-reference mechanism above from accounts to orders. Each is anchored to a real, `status == "returned"` order from the shared order pool -- the underlying customer complaint is always real -- but what the agent actually typed into `order_reference`, and whether ERP has since caught up, both vary:

- **Reference correctness**: ~78% reference the correct real order. A materially larger tail (18%, `REFUND_REF_WRONG_VALID_RATE`) is a transposition-style typo (`transpose_order_number()`, bounded and checked exactly like clickstream's own single-character-typo mechanism) landing on a **different, real, existing** order -- the dangerous "wrong-but-valid" case: a confident wrong match, not an obvious miss, and one that can coincidentally still resolve to another returned order (indistinguishable from a correct reference by a simple anti-join alone). This rate was originally 6% (mirroring `GHOST_ACCOUNT_RATE`'s proportions), but freeze-gate peer review found that too thin -- only 4 of 224 refund tickets were even detectable as wrong-but-valid by a naive anti-join+status check. Raised to 18% (true realized rate: 38/224, 17.0%, of which 22 are anti-join-detectable) so a naive inner join on `order_reference` misattributes a materially visible share of tickets (22/224, 9.8%) and dollars (\$26,817 of \$266,395 claimed, 10.1%) -- see the freeze-baseline ADR for the full breakdown. A smaller tail still (~4%, `REFUND_REF_NONEXISTENT_RATE`) reuses the ghost-order mechanism (`order_pool.ghost_order_number()`) to reference an order that doesn't exist at all.
- **Open vs. posted**: ~35% of refund tickets are `open`/`pending` -- an operational event (the customer's complaint) that hasn't yet been reflected in ERP's own `refund_date`/`refund_amount`. The remaining ~65% are `resolved`/`closed` ("posted"): ERP has since caught up, and `claimed_amount` should mostly match ERP's actual `refund_amount`.
- **Posted-amount mismatch**: of posted tickets whose reference resolves to a real order, ~10% (`REFUND_POSTED_MISMATCH_RATE`) have a `claimed_amount` that differs from ERP's `refund_amount` by a flat shipping/tax-like adjustment (`REFUND_MISMATCH_ADJUSTMENTS`, ±\$5.99-\$12.99) -- representing an agent who forgot to include or exclude shipping/tax in what they typed, a real bounded clerical error, not noise.
- More than one ticket may reference the same order (a customer calling twice, or a duplicate contact) -- ticket count per order is never artificially capped at one.

Ticket count is `round(returned_order_count * REFUND_TICKET_MULTIPLIER)` (1.15) -- emergent from real ERP return volume, not an independent dial: some returned orders draw more than one contact, others draw none (self-service return, no CRM contact at all).

The data is fictional and contains no production customer information.

### Fixed date window

`created_date`, `last_seen_date` (accounts), and `created_date` (general tickets) are all drawn from windows anchored to the same fixed `AS_OF_DATE = 2025-12-31` reference date `erp.py` and `clickstream.py` use, not `date.today()` -- see "Fixed date window, extended for refunds" above for why this matters for reproducibility. Refund tickets' `created_date` is anchored to the anchor order's `refund_date` (± a few days) and uses that same section's extended upper bound (`order_pool.REFUND_WINDOW_END`, 2026-02-14), not the un-extended `AS_OF_DATE`.

## Deliberate non-messiness

This Phase 1 extract does not inject nulls, duplicate line keys, invalid dates, or mismatched totals. Those defects would test data-quality handling rather than the specific regional-sharding, destructive-update, and order-item-grain behaviors required here. Web clickstream's `user_email`/`customer_global_email` being absent on most events (see "Sparse, tiered-dirty customer identity capture" above) is not an exception to this -- it's not an injected data-quality defect, it's the field legitimately having no value most of the time, the same way `page_url` legitimately has no value on a `search_query` event. The same reasoning applies to ERP's `ship_date`/`refund_date`/`refund_amount` (blank for `pending`/`cancelled`/non-returned orders) and CRM's `claimed_amount`/`order_reference` (blank outside `category == "refund"`) added in Phase 5B pre-work -- legitimate status/category-conditional absence, not injected data-quality defects.
