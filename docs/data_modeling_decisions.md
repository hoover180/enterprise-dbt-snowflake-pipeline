# Data Modeling Decisions

An append-only log of architecture decision records (ADRs) for this project. Per the PR template, any PR that changes an architectural decision links back to an entry here.

## ADR-001: EMAIL_MASK is role-based, not a blanket restriction

**Status:** Accepted (2026-09-08); amended 2026-09-09 to move governance objects into a dedicated `GOVERNANCE` database.
**Phase:** Defined in Phase 2 (this PR — Terraform PII tagging/masking infra); attached to a gold-model column in Phase 6.

### Context

`terraform/masking_policies.tf` defines `EMAIL_MASK`, a Snowflake masking policy, and `terraform/tags.tf` defines the paired `PII` tag. Neither Phase 2 (infra) nor any phase before it produces a table with an email column — the future consumer is `dim_customer_360.contact_email` (naming per the ERP/CRM `contact_email`/`customer_email` fields documented in `docs/synthetic_data_spec.md`), which doesn't exist until the Phase 6 gold models are built. The policy and tag are written now so the pattern is established and reviewable before there's a real column to attach it to, rather than being designed under time pressure once Phase 6 lands.

The two options considered for the policy body:

1. **Blanket restriction** — mask the column for every querying role, with no exception.
2. **Role-based** — unmask for the role that legitimately needs the real value, mask for everyone else.

### Decision

`EMAIL_MASK` is role-based: `CURRENT_ROLE()` in `('TRANSFORMER_ROLE')` returns the real value; every other role, including `READ_ONLY_ANALYST`, gets a fixed placeholder (`'***MASKED***'`).

### Ownership: `GOVERNANCE.SECURITY`, not `PROD_ANALYTICS.PUBLIC`

`EMAIL_MASK` and `PII` were originally defined in `PROD_ANALYTICS.PUBLIC`. That was wrong: `PROD_ANALYTICS` is one peer among `DEV_ANALYTICS`/`STAGE_ANALYTICS`/`PROD_ANALYTICS`, and the tag/policy are meant to be referenced by fully-qualified name from *all three* (see `terraform/tags.tf`'s comment on why tags are routinely cross-database-referenced). Owning shared vocabulary inside one peer environment means DEV and STAGE columns would depend on an object that lives in, and is lifecycled with, PROD — the wrong dependency direction, and one that would force PROD's database to exist and stay stable before DEV could even reference the tag.

Both objects now live in a new `GOVERNANCE` database (`terraform/databases.tf`), in a dedicated `GOVERNANCE.SECURITY` schema rather than `GOVERNANCE.PUBLIC` — `SECURITY` scopes the schema to exactly this kind of object (PII tags, masking policies) so a future unrelated governance object doesn't get dumped into the same namespace by default. **`GOVERNANCE.SECURITY` is the one fully-qualified name used everywhere** a tag or policy is defined or attached — Terraform, this ADR, and the Phase 6 `ALTER TABLE` example below all use it; there is no other name for these objects anywhere in the codebase.

`TRANSFORMER_ROLE` has `USAGE` on `GOVERNANCE` (database) and `GOVERNANCE.SECURITY` (schema) — see `terraform/roles.tf`. That grant makes the tag and policy visible/referenceable by fully-qualified name; it is **not** sufficient to attach either to a column. Attaching requires the separate `APPLY TAG` and `APPLY MASKING POLICY` privileges, which this PR deliberately does **not** grant — there's no real column to attach to yet, and granting an attachment privilege with nothing to exercise it against is premature. That grant is deferred to Phase 6, alongside the `ALTER TABLE` statements themselves. `READ_ONLY_ANALYST` gets no `GOVERNANCE` grant at all, in either phase: masking is enforced at query time against the mart table the policy is attached to, not by the querying role reading the policy object.

A blanket restriction was rejected because `TRANSFORMER_ROLE` (see `terraform/roles.tf`) is the identity dbt itself runs as. Identity resolution — matching CRM `contact_email` against ERP `customer_email`/`contact_email` via the shared identity pool (`docs/synthetic_data_spec.md`, "Why ERP↔CRM join is deterministic") — is a join *on* the real email value. If dbt's own role saw a masked placeholder, every row would collapse to the same masked string and the join would match everything to everything. A masking policy has no notion of "the pipeline that's currently building the table" separate from "a role querying it" — the warehouse role is the only trust boundary Snowflake gives us, so the exception has to be expressed as a role, not as a build-time/query-time distinction.

### Consequences

- dbt transform runs (as `TRANSFORMER_ROLE`) operate on real email values end-to-end, so identity resolution keeps working once it's built in Phase 5.
- `READ_ONLY_ANALYST` — and any future role — never sees a real email address by default; a new role only gains visibility by being added to the `CURRENT_ROLE()` allow-list in the policy body, which is a reviewable one-line Terraform diff, not a per-table grant that's easy to miss.
- The trust boundary is enforced at the role Snowflake already authenticates the session against, not by trusting the application/BI layer to filter columns correctly.
- If a future role needs the real value (e.g. a support/ops role handling GDPR data-subject requests), it's added to the same allow-list rather than the policy being redesigned.

### Note on attachment timing

`EMAIL_MASK` and the `PII` tag are defined in this PR but **not attached to any table or column** — none exists yet, and the `APPLY TAG`/`APPLY MASKING POLICY` privileges needed to attach them have deliberately not been granted to `TRANSFORMER_ROLE` either (see "Ownership" above — the `USAGE` grant this PR does add only makes the objects referenceable, it does not authorize attachment). Both the grants and the attachment itself land together in Phase 6, once a gold model materializes the email column, via:

```sql
ALTER TABLE PROD_ANALYTICS.<schema>.DIM_CUSTOMER_360
  MODIFY COLUMN CONTACT_EMAIL
  SET MASKING POLICY GOVERNANCE.SECURITY.EMAIL_MASK;

ALTER TABLE PROD_ANALYTICS.<schema>.DIM_CUSTOMER_360
  MODIFY COLUMN CONTACT_EMAIL
  SET TAG GOVERNANCE.SECURITY.PII = 'EMAIL';
```

That `ALTER TABLE` is expected to be added to the Phase 6 dbt model (a post-hook or a `snowflake__copy_grants`-style migration) rather than run by hand, so the attachment survives a full `dbt build` from a clean database.

## ADR-002: ERP staging grain, currency, and timezone standardization

**Status:** Accepted (2026-09-10).
**Phase:** Phase 3 (this PR — ERP staging models, Issue #16). Snapshots/SCD2 over order status is a separate PR (Issue #17) and is explicitly out of scope here. Cross-shard customer identity resolution is Phase 5.

### Context

`DEV_ANALYTICS.RAW.US_ORDERS` and `DEV_ANALYTICS.RAW.EU_ORDERS` are two regional ERP shards at order-item grain with independently-named columns (see `docs/synthetic_data_spec.md`). Building a single canonical staging model requires deciding (a) the business key/grain to enforce, (b) how to reconcile two different currencies into one reporting currency, and (c) how to reconcile any regional timestamp/timezone differences before the shards are unioned.

### Decision: grain and business key

The canonical staging model, `stg_erp__order_items`, is at **order-item grain**, matching the raw shards. The business key is the composite `(region, source_order_id, source_line_item_id)` — `source_order_id` is `order_id` (US) / `order_no` (EU), and `source_line_item_id` is `line_item_no` (US) / `item_seq` (EU). This composite is exposed as a single `order_item_key` column with `unique` and `not_null` tests.

`source_order_id` alone is **intentionally non-unique** at this grain (confirmed directly against `DEV_ANALYTICS.RAW`: 500 distinct orders produce 1,254 US rows and 1,273 EU rows) — an order with N line items produces N rows sharing the same order id. No deduplication or aggregation is performed to force a row-count-based uniqueness test to pass; the composite key is the correct grain, not the row count.

### Decision: currency standardization

Monetary fields (`unit_price`, tax amount) are converted to **USD** before the cross-shard union, via the `erp_convert_to_usd()` macro and two dbt vars (`dbt_project.yml`): `erp_eur_to_usd_rate` (`1.10`) and `erp_gbp_to_usd_rate` (`1.27`). USD was chosen as the reporting currency because neither `docs/synthetic_data_spec.md` nor any other project doc specifies a different reporting currency, and the US shard is already natively USD.

Both rates are explicitly **synthetic modeling assumptions for this portfolio project** — fixed, not sourced from any FX provider or rate table, and not varying by date. Precision doesn't matter here; what matters is that the assumption is a documented variable, not an inline literal in model SQL. No FX lookup table or external FX integration is built in this branch; that is out of scope by design.

**Found and fixed during implementation:** `EU_ORDERS.currency_code` actually contains both `EUR` (650 rows) and `GBP` (623 rows) — the synthetic data spec's "EUR or GBP" note was confirmed directly against the loaded raw data, not assumed. The first pass through this PR only defined a conversion rate for EUR, which meant `erp_convert_to_usd()` returned `null` for the 623 GBP rows and routed all of them to `stg_erp__order_items_quarantine` as `unsupported_currency:GBP` — 49% of EU order-items quarantined solely for that reason. `erp_gbp_to_usd_rate` was added in the same PR to close the gap; `erp_convert_to_usd()` now handles `USD`/`EUR`/`GBP`, and quarantine is empty against the current synthetic dataset (see Consequences).

### Decision: timezone standardization

Checked directly against `DEV_ANALYTICS.RAW`: `US_ORDERS.order_date` and `EU_ORDERS.placed_on` are both Snowflake `DATE` columns with no time-of-day component. There is no regional UTC offset present anywhere in the raw ERP data to convert — unlike the clickstream source (`event_timestamp`/`ingested_at`, Phase 3B), the ERP shards never carried timezone information in the first place.

`stg_erp__order_items.order_date` is therefore a direct, documented cast (`::date`) of the native column, not a numeric offset shift. This preserves the source's business-date semantics unchanged; the model does not reinterpret `order_date`/`placed_on` as an ingestion timestamp, and no late-arrival/replay semantics are introduced (that distinction belongs to clickstream's Phase 3B design, not ERP staging). If a future ERP source ever lands with an actual timezone-bearing timestamp, this staging layer will need a real offset conversion at that point — today there is nothing to convert.

### Decision: quarantine routing

Basic row-level validation happens in `erp_quarantine_reason()`, applied identically to both `stg_erp__order_items` (keeps only passing rows) and `stg_erp__order_items_quarantine` (keeps only failing rows, plus `quarantine_reason` and `quarantined_at`). Checks: missing business key, missing customer id, missing/invalid quantity, missing/invalid unit price, missing currency, and unsupported currency (no USD conversion available — none of the currencies present in the current synthetic dataset, `USD`/`EUR`/`GBP`, currently trigger this; the check exists for whatever currency shows up next without a defined rate).

This is deliberately minimal: no retry counts, no replay orchestration, no resolved-status audit trail, and no generic data-quality framework. That is Phase 6. `stg_erp__order_items_quarantine` is materialized as a `table`, not the staging-default `view` — `quarantined_at` uses `current_timestamp()`, and a view would re-evaluate that on every `SELECT` instead of fixing it to when the row was actually quarantined.

### Consequences

- `stg_erp__order_items` holds 2,527 rows (1,254 US + 1,273 EU — all of both shards, USD and EUR and GBP alike); `stg_erp__order_items_quarantine` is empty (0 rows) against the current synthetic dataset. No validation-failure reason is currently exercised, since the source data has no nulls/invalid amounts (`docs/synthetic_data_spec.md`, "Deliberate non-messiness") and both currencies now convert. An empty quarantine table here is the correct signal that the validation logic works and this PR's currency scope now matches what's actually in the raw data — not evidence the checks are unreachable dead code.
- Any downstream model reading `stg_erp__order_items` for revenue/analytics gets a consistent USD figure across both regions and all three source currencies. Anyone adding a fourth ERP shard, or a currency neither rate covers, should extend `erp_convert_to_usd()` with another `var`-backed rate the same way, not special-case it inline.
- `sqlfluff`'s jinja templater does not know project-defined dbt macros by default; `.sqlfluff` now sets `load_macros_from_path = dbt/macros` so `sqlfluff lint` can render `erp_order_items_unioned()`/`erp_quarantine_reason()`/`erp_convert_to_usd()` instead of failing to template them. This is the first PR with real staging `.sql` files, so it's the first time this gap in the Slim CI harness (from PR #22) was exercised.

## ADR-003: dbt Snapshots (SCD2) over ERP's destructive order-status updates

**Status:** Accepted (2026-09-10).
**Phase:** Phase 3 (this PR — Issue #17). Builds on `stg_erp__order_items` (Issue #16, ADR-002) but is intentionally a separate model and a separate PR.

### Context

`docs/synthetic_data_spec.md` ("Destructive status updates") is explicit: the ERP source overwrites `order_status`/`fulfillment_state` in place, retains no history, and carries no update timestamp. Downstream models therefore cannot reconstruct *when* an order changed state from the raw data alone — the only way to recover that history is to start capturing it going forward, which is exactly what a dbt snapshot does: it diffs each run against the previous snapshotted state and writes a new row only when something relevant changed.

### Contrast with Project 2's one-time `ALTER TABLE`

A previous portfolio project (Project 2) handled a schema change with a one-time `ALTER TABLE` migration. That pattern fits a source that changed shape *once*, at a known point, where the fix is a single forward-only DDL statement. It does not fit here: this ERP source has no change history at all, by design, on every run — there is no single point-in-time migration to write, because the destructive overwrite is the source's permanent, ongoing behavior. A dbt snapshot is the correct tool specifically because it runs repeatedly and accumulates history incrementally each time it's invoked, rather than making one fixed correction and being done.

### Decision: separate order-grain staging model (`stg_erp__orders`)

`stg_erp__order_items` (ADR-002) is order-item grain by design — an order with N line items produces N rows, which is required for Issue #16's item-level revenue/tax fields. Snapshotting that model directly would be wrong for two independent reasons:

1. `check_cols: ['order_status']` would need to evaluate identically across every line-item row of the same order for the snapshot to behave sanely at order-item grain, turning a status-history feature into an implicit, undocumented dependency on line-item-level invariants.
2. Snapshotting at item grain would snapshot every item's currency/quantity/price fields too unless carefully excluded, none of which are part of the status-history question this issue is about.

A new order-grain model, `stg_erp__orders`, was built instead — one row per `(region, order_id)`, exposing exactly `region`, `order_id`, `customer_id`, `order_status`, `order_date`. It reuses `stg_erp__order_items`'/`erp_order_items_unioned()`'s config-driven per-shard mapping pattern (a new `erp_orders_unioned()` macro, same shape, order-level columns only) rather than referencing `stg_erp__order_items` directly, so that order-level status history has no dependency on item-level quarantine routing — an order's status is a fact about the order regardless of whether one of its line items happens to fail item-level validation.

Collapsing item-grain rows to order grain via `select distinct` is non-lossy here: verified directly against `DEV_ANALYTICS.RAW.US_ORDERS`/`EU_ORDERS` that `order_status`/`fulfillment_state` and `customer_id`/`client_ref` are identical across every line item of the same order (0 inconsistent orders in either shard, out of 500 orders/region) — not assumed from `data_gen/erp.py`'s "all item rows for an order share the order's current status" note, though that note does explain *why* it holds.

### Decision: `check` strategy, not `timestamp`

dbt's `timestamp` strategy requires a genuine `updated_at`-style column on the source. Neither ERP shard has one — `docs/synthetic_data_spec.md` is explicit that there is no update timestamp at all, only the final overwritten value. `check` is therefore the only strategy that applies, comparing the configured `check_cols` against the previous snapshotted state on every run instead of comparing timestamps.

`check_cols: ['order_status']` only — deliberately not `check_cols: 'all'` and not including `order_date`/`customer_id`. This snapshot answers one question (when did this order's status change, and to what) and is scoped to that; it is not a general-purpose change-tracker for every order field. A future need to track e.g. customer reassignment would be a new, separately-scoped snapshot or an explicit extension of `check_cols`, not an incidental side effect of this one.

### Decision: composite `unique_key` via native list syntax, YAML `config:` block

Checked against the current dbt-core 1.12 documentation directly (this project has been burned before by assuming stale patterns — the sqlfluff/dbt-templater gap and GitHub Actions version drift are both called out elsewhere in this repo) rather than defaulting to a familiar-but-outdated pattern:

- Snapshot definitions are written in a YAML `config:` block (`dbt/snapshots/erp_orders_status_snapshot.yml`, top-level `snapshots:` key, `relation: ref(...)`), the syntax dbt-core has recommended since 1.9 — not the legacy `{% snapshot %}` Jinja SQL block.
- Composite `unique_key` is expressed as a native YAML list, `unique_key: [region, order_id]` — dbt-core 1.9 added first-class support for this (dbt-labs/dbt-core#9992); string concatenation (`region || '-' || order_id`) was the pre-1.9 workaround and is no longer the recommended approach. `stg_erp__orders.order_key` (the same `region-order_id` concatenation, matching `stg_erp__order_items.order_item_key`'s established convention) still exists as a convenience column for `not_null`/`unique` testing on the staging model itself, but the snapshot's own `unique_key` config uses the list form directly against `region`/`order_id`, not that derived column.

The snapshot lands in the `RAW` schema, same as every other model in this project currently — `docs/dbt_profile_setup.md` already documents that per-layer schema separation via a `generate_schema_name` macro is deliberately deferred future work, not solved by this PR; giving the snapshot a one-off custom schema would be inconsistent with every other model's current behavior for no benefit.

### Manual-update demonstration methodology

`data_gen/erp.py` has no mechanism for producing a second, differing state on a subsequent run — `apply_destructive_status_updates()` is a single deterministic pass per seed (`pending`→`shipped`→82% chance of `delivered`), with no day-N flag or seed variant (confirmed by reading the script, not assumed). `data_gen/load_raw.py` also always does `CREATE OR REPLACE TABLE`, so simply re-running the generator and reloader can never produce a differing second load. The only way to demonstrate a genuine second state is a manual, one-time `UPDATE` run directly against `DEV_ANALYTICS.RAW.US_ORDERS`/`EU_ORDERS` — explicitly a stand-in for what a real ERP's own destructive update would do, not a repeatable part of the pipeline. This is a demonstration step only; it is not part of `dbt build`, CI, or any script in this repo.

Before running it, `TRANSFORMER_ROLE`'s privileges were checked: `terraform/roles.tf` grants no explicit `UPDATE` (only `CREATE TABLE`/`CREATE VIEW`/`CREATE STAGE` and `USAGE`). But `data_gen/load_raw.py` connects as `TRANSFORMER_ROLE` by default and creates `US_ORDERS`/`EU_ORDERS` itself via `CREATE OR REPLACE TABLE`, so `TRANSFORMER_ROLE` owns those tables — ownership implies full DML. This was confirmed empirically (not just inferred) with a no-op `UPDATE ... SET order_status = order_status WHERE 1=0` before touching real rows, which succeeded with 0 rows affected and no permission error.

Five `shipped` orders per shard were selected by querying current status directly (not assumed) — `shipped`→`delivered` was used because it is the only transition present in the synthetic data (confirmed: every order in `DEV_ANALYTICS.RAW` is currently either `shipped` or `delivered`, 90/410 split per shard, matching the generator's 82% delivery rate). Original status was recorded before any change:

| Order | Region | Status before | Status after |
| ----- | ------ | -------------- | -------------- |
| US-000004, US-000006, US-000012, US-000013, US-000019 | US | shipped | delivered |
| EU-000002, EU-000005, EU-000007, EU-000028, EU-000053 | EU | shipped | delivered |

After the manual `UPDATE` (11 line-item rows affected per shard, matching those 5 orders' 1-4 items each) and rerunning `stg_erp__orders` then `dbt snapshot`, `erp_orders_status_snapshot` was queried directly:

- All 10 changed orders now have exactly 2 rows: one closed (`order_status = 'shipped'`, `dbt_valid_to` = the second run's timestamp) and one open (`order_status = 'delivered'`, `dbt_valid_from` = the same timestamp) — the closed row's `dbt_valid_to` matches its successor's `dbt_valid_from` exactly for all 10 (0 mismatches, checked directly).
- All 990 untouched orders still have exactly 1 open row — no spurious history anywhere.
- Total snapshot row count: 1010 (1000 baseline + 10 new versions), matching 1000 orders × (1 + 10 having a second version).

### Consequences

- `erp_orders_status_snapshot` is the only place in this project with real order-status history; every other model still sees only the current (post-overwrite) status, exactly as the source provides.
- The singular test `assert_erp_orders_status_snapshot_single_open_version` (`dbt/tests/`) encodes the invariant that actually matters for SCD2 correctness — never more than one currently-open row per order — rather than relying solely on schema tests, which can't express a cross-row condition like this.
- Extending this to CRM or clickstream status-like fields in the future should follow the same shape: a narrow, grain-appropriate staging model feeding a `check`-strategy snapshot scoped to the specific column(s) that need history, not a blanket snapshot of an entire wide model.

## ADR-004: Web clickstream staging — VARIANT parsing, customer-key coalesce, pixel-retry dedup

**Status:** Accepted (2026-09-10); amended 2026-09-10 (see ADR-005) to cast `event_timestamp`/`ingested_at` to `TIMESTAMP_TZ` instead of `TIMESTAMP_NTZ` -- the original `TIMESTAMP_NTZ` choice silently dropped the source's UTC marker and caused incorrect results once a UTC-boundary-comparing consumer (ADR-005's microbatch model) was built on top of this model; amended again 2026-09-11 (see ADR-008) -- `customer_id`'s underlying identity signal is renamed to `customer_email` and changes from an always-present, directly-copied `CUST-#####` key to a sparse, sometimes-dirty captured email. This section's `customer_id`/`user_id`/`customer_global_id` naming, its "always present" framing, and the specific counts below (e.g. 503/547, 482/518) describe the state as originally built and are left as the historical record of what was verified at the time, per this project's amendment convention -- ADR-008 has the current behavior and the reasoning for the change.
**Phase:** Phase 3B (this PR — clickstream staging). The incremental model, 3-day look-back window, and backfill/replay demonstration described in `docs/synthetic_data_spec.md`'s late-arriving-events section are explicitly out of scope here and land in a separate, later PR.

### Context

`DEV_ANALYTICS.RAW.RAW_WEB_EVENTS` lands as a single VARIANT column, `event_data` (confirmed against `data_gen/load_raw.py`'s `JSON_SOURCE` spec, not assumed). `docs/synthetic_data_spec.md`'s "Web clickstream source" section calls out three injected data-quality issues to handle in staging: a mid-year `user_id` → `customer_global_id` key rename, ~5% web-pixel-retry duplicate events, and late-arriving events (out of scope here — see Phase note above). Before writing any SQL, all three were checked directly against the loaded raw data rather than assumed from the docs or generator:

- **Schema drift**: exactly matches `event_date < 2025-07-01` — 503 of 1,050 raw rows carry `user_id`, 547 carry `customer_global_id`, with zero rows carrying both and zero carrying neither.
- **Duplicates**: 1,000 distinct `event_id` values across 1,050 raw rows (50 extra rows, matching `data_gen/clickstream.py`'s `DUPLICATE_RATE = 0.05`). For a sample of every duplicated `event_id`, `count(distinct event_data)` across its copies is 1 — these are fully identical row copies (same `event_id` *and* every other field, produced by `dict(events[index])` in the generator), not distinct events that merely share a customer or session. `event_id` is therefore the correct canonical event identity, not a heuristic stand-in.
- **Malformed events**: none. 0 rows with a missing `event_id`/`event_timestamp`/`event_date`/`ingested_at`/`session_id`/`event_type`, and 0 rows where a direct `::timestamp_ntz`/`::date` cast on the raw VARIANT value fails, across all 1,050 rows.

### Decision: VARIANT parsing via colon syntax, not FLATTEN

Each raw row is already one event, so there is nothing to explode — `web_events_parsed()` (`dbt/macros/web_events_parsed.sql`) extracts every field with `event_data:field::type` colon syntax. `FLATTEN` is for unnesting arrays/nested objects into multiple rows per input row, which doesn't apply here.

### Decision: TRY_CAST for type-sensitive fields, not a plain `::type` cast

**Found and fixed during implementation**, the same way ADR-002 found the missing GBP rate: a plain `::type` cast on a VARIANT does not degrade to `NULL` for an unparseable value the way casting a native-typed column does — it raises a hard SQL error. Confirmed directly with a synthetic literal:

```sql
select parse_json('{"x": "not-a-date"}'):x::date as bad;
-- 100071 (22000): Failed to cast variant value "not-a-date" to DATE
```

Since the whole point of routing "VARIANT parsing failures" to quarantine is to keep one bad event from taking down the build, a plain `::type` cast on `event_timestamp`/`event_date`/`ingested_at`/`quantity` would have defeated the mechanism the first time a malformed event appeared — the model would fail to compile at all rather than quarantine the one bad row. `web_events_parsed()` uses `try_cast(event_data:field::varchar as <type>)` for these four fields instead, which yields `NULL` on an unparseable value (confirmed with the same literal: `try_cast(parse_json('{"x":"not-a-date"}'):x::varchar as date)` returns `NULL`, no error). (`event_timestamp`/`ingested_at`'s target `<type>` was later corrected from `TIMESTAMP_NTZ` to `TIMESTAMP_TZ` — amended 2026-09-10, see ADR-005.) `event_id`/`session_id`/`event_type`/`customer_id` stay on a plain `::varchar` cast — a VARIANT always has a string representation, so there's no failure mode there for `TRY_CAST` to guard against.

### Decision: coalesce `user_id`/`customer_global_id` into `customer_id`, not a schema-versioned dual column

**Amended 2026-09-11 (ADR-008):** this section's claim that "both keys draw from the exact same synthetic `CUST-#####` population" described a Phase 1 build gap, not the intended design -- `docs/synthetic_data_spec.md` already documented (in its "Why ERP↔CRM join is deterministic while web is heuristic" section) that web identity was supposed to be a heuristic email match, not a direct copy of ERP's key. ADR-008 fixes this: the coalesce mechanism and its "one canonical column, not a schema-versioned dual column" reasoning below are unchanged and still apply, but the columns are now named `user_email`/`customer_global_email` → `customer_email`, and the value is a sparse, sometimes-dirty captured email rather than an always-present exact key.

Two options were considered: expose both `user_id` and `customer_global_id` as separate nullable columns tagged by schema version, or coalesce them into one canonical `customer_id` column.

Coalesce was chosen. The rename is a one-time, permanent cutover in the source system, not two coexisting business concepts that downstream models need to reason about differently — both keys draw from the exact same synthetic `CUST-#####` population (confirmed: `stg_web__events.customer_id` is non-null for all 482 pre-cutoff and all 518 post-cutoff rows, with values spanning the same `CUST-00001`–`CUST-00350` range on both sides), and the two columns are mutually exclusive by construction (never both present on one row). A dual-column design would push the schema-version bookkeeping onto every downstream consumer — including Phase 5's cross-shard identity resolution — for a distinction that carries no actual business meaning once resolved. This mirrors ADR-002's ERP currency conversion: normalize divergent native representations into one canonical column at the staging boundary, rather than propagating the source system's internal versioning downstream. If a future schema drift changed the *meaning* of the identifier (not just its name), a dual-column/schema-versioned approach would be the right call — that's not the case here.

### Decision: dedup via `QUALIFY ROW_NUMBER()`, not `GROUP BY`

`stg_web__events` removes web-pixel-retry duplicates with:

```sql
qualify row_number() over (partition by event_id order by ingested_at asc) = 1
```

`GROUP BY event_id` was rejected even though it would produce the same row count here, because it doesn't generalize safely. `GROUP BY` forces every one of the model's ~10 other selected columns to either appear in the `GROUP BY` list or be wrapped in an aggregate — with the current data (duplicates are byte-identical copies) an aggregate like `MAX()` on each column happens to be a no-op, but that's an accident of today's generator, not a property the model enforces. If a future duplicate ever arrived as a genuine retry with a *different* `ingested_at` (the realistic pixel-retry scenario `docs/synthetic_data_spec.md` describes — a delayed re-send, not a byte-for-byte copy), a `GROUP BY`/`MAX()` approach would silently blend fields from different physical rows with no way to express "keep this whole row, not a field-by-field merge." `QUALIFY ROW_NUMBER()` instead picks one whole row deterministically per `event_id` via an explicit, auditable tie-break (`ingested_at asc` — the earliest-arriving copy is treated as canonical, consistent with a real retry scenario where the first pixel fire is the original event), and never merges fields across rows.

### Decision: quarantine routing, verified with an injected failure case

`web_quarantine_reason()` (`dbt/macros/web_quarantine_reason.sql`) checks for a missing `event_id`, a missing-or-unparseable `event_timestamp`/`event_date`/`ingested_at` (post-`TRY_CAST`, so this catches both "field absent" and "field present but malformed"), and a missing `session_id`/`event_type`/`customer_id`. Applied identically to `stg_web__events` (keeps only passing rows) and `stg_web__events_quarantine` (keeps only failing rows), exactly mirroring `erp_quarantine_reason()`'s split. **Amended 2026-09-11 (ADR-008):** the missing-`customer_id` check was removed -- once identity capture became sparse by design, a null identity field is the expected majority case, not a row-level validation failure. Every other check described here is unchanged.

The routing was not just inferred from the `TRY_CAST`-on-a-literal finding above — it was proven end-to-end against a real row. One event was inserted directly into `DEV_ANALYTICS.RAW.RAW_WEB_EVENTS` with a fixed, obviously-synthetic `event_id` (`TEST-ARTIFACT-QUARANTINE-VERIFICATION-0001`) and a deliberately unparseable `event_timestamp` (`"NOT-A-VALID-TIMESTAMP"`, exercising the exact cast-failure mode confirmed above), otherwise matching a real event's shape. Rerunning both staging models: the row appeared in `stg_web__events_quarantine` with `quarantine_reason = 'missing_or_unparseable_event_timestamp'`, `event_timestamp` correctly `NULL` (the model build did not crash), and every other field parsed normally (`event_date`, `ingested_at`, `session_id`, `event_type`, `customer_id`, `page_url` all present and correct); the same `event_id` returned 0 rows from `stg_web__events`, confirming the malformed row did not leak into the clean model. The row was then deleted from `RAW_WEB_EVENTS` and both models rerun again; `RAW_WEB_EVENTS`/`stg_web__events`/`stg_web__events_quarantine` counts returned to exactly 1,050/1,000/0, with zero residual rows matching the test `event_id` anywhere — confirming the injection left no trace.

Against the real synthetic dataset (no injected row), this still checks for something that doesn't exist: `stg_web__events_quarantine` holds 0 rows in steady state, because `docs/synthetic_data_spec.md`'s clickstream generator produces no genuinely malformed events (confirmed directly, not assumed — see "Context" above). That is the same situation ADR-002 hit with ERP's quarantine before the GBP rate was added: an empty quarantine table is the intended signal that the mechanism works and the current data has nothing to catch, not evidence the checks are unreachable — and unlike ADR-002's case, that claim is now backed by an actual injected-and-removed test row, not only by reasoning about cast behavior in isolation. Unlike ERP's quarantine, `stg_web__events_quarantine` also carries `raw_event_data` (the unparsed VARIANT), not just the parsed columns — for a `TRY_CAST` failure the parsed column is itself `NULL`, so without the raw payload a quarantined row would show only *that* something failed, not *what* the unparseable source value actually was.

Type-specific fields (`page_url`, `product_id`, `quantity`, `search_query`) are deliberately excluded from `web_quarantine_reason()` — each is legitimately null for two of the three `event_type`s (confirmed directly: `search_query` rows carry a null `page_url`/`product_id`), so a blanket `not_null` check on any of them would misclassify normal rows as failures.

### Consequences

- `stg_web__events` holds 1,000 rows (1,050 raw rows − 50 pixel-retry duplicates), matching the 1,000 distinct `event_id` values confirmed directly against the raw table before any model was built. `stg_web__events_quarantine` is empty (0 rows) in steady state, and that routing behavior has been exercised end-to-end with a real injected-and-removed failing row (see "Decision: quarantine routing" above), not just reasoned about from cast semantics.
- `event_id` carries `unique`/`not_null` tests on `stg_web__events` and passes against the deduplicated data; this is the model's actual grain guarantee, not a row-count assumption.
- `web_events_parsed()`'s `TRY_CAST` usage is the reusable pattern for any future VARIANT-sourced staging model in this project — a plain `::type` cast is only safe on a VARIANT field once you're certain (checked, not assumed) that the source never produces a value that fails to convert.
- `web_quarantine_reason()` is called from both staging models as `{{ web_quarantine_reason() | trim | indent(8) }}` at its multi-line (`case...end`) call site, so the compiled SQL is actually indented to match its surroundings — verified directly against `target/compiled/`, not assumed from the source. `indent()` alone was insufficient: the macro's own leading/trailing newlines (from `{% macro %}`/`{% endmacro %}` each sitting on their own line) made Jinja's filter treat an empty string as "line 1" and therefore skip indenting it, which left `when`/`end` one indent level short of the `case` line's own indent. `trim` first removes those newlines so `indent()`'s width lines up with the call site's actual 8-space indent. This project has a known, confirmed gap where `erp_quarantine_reason()`/`erp_convert_to_usd()` are called with no `indent()` at all and compile with `case`/`end` sitting at column 0 inside an indented `select` — left as-is there (Issue #16/#17 are closed), but not repeated in this PR's new macro.

## ADR-005: Web clickstream incremental model — microbatch strategy, 3-day lookback, backfill

**Status:** Accepted (2026-09-10); amended 2026-09-11 (see ADR-008) -- `stg_web__events.customer_id` (this model's pass-through `customer_id` column) is renamed `customer_email` and its underlying identity signal changes from an always-present exact key to a sparse, sometimes-dirty captured email. This ADR's own subject (microbatch strategy, lookback, backfill) is unaffected -- `event_timestamp`/`ingested_at`/batching logic don't depend on the identity column at all -- so nothing below needed correcting beyond this note.
**Phase:** Phase 3B, second half (Issue #31). Builds on `stg_web__events` (Issue #29, ADR-004) without changing its logic, aside from one type correction found during this work (see "Found and fixed" below).

### Context

Issue #29/ADR-004 deliberately deferred the incremental model, 3-day look-back window, and backfill/replay demonstration for late-arriving clickstream events. `docs/synthetic_data_spec.md` ("Late-arriving events") and `stg_web__events`'s own schema.yml already document that ~2% of canonical events have `ingested_at` landing 2-4 days after `event_timestamp`, "to support the Phase 3B backfill demonstration." Before writing any model, the real data was checked directly against `DEV_ANALYTICS.RAW.RAW_WEB_EVENTS` rather than assumed:

- `event_date` ranges 2025-01-05 to 2025-12-31 (confirmed directly).
- Exactly 20 real late-arriving events exist (`ingested_at` 2-4 days after `event_timestamp`), spread across the full year (roughly one every 2-3 weeks, not clustered at the start/end), with delays of 2, 3, or 4 days. This is a clean, illustrative sample of the exact "day-N-event-arrives-on-day-N+2" scenario the build plan describes — real data was used for the demonstration below, not a fabricated injection.
- `event_id` is already confirmed canonical and durably unique for this model's grain: ADR-004 established 1,000 distinct `event_id` values from 1,050 raw rows (50 exact pixel-retry duplicates), and `stg_web__events.event_id` already carries passing `unique`/`not_null` schema tests. No new key derivation was needed.

### Decision: `microbatch` incremental strategy, not hand-written `is_incremental()` + merge

Checked directly against dbt-core 1.12.4 (installed) source and current docs, not assumed from familiarity with the older pattern:

- `microbatch` is a core (adapter-independent) materialization strategy, introduced in dbt-core 1.9 and present unchanged in 1.12.4 (`dbt/materializations/incremental/microbatch.py` in the installed package). dbt-snowflake 1.12.0 executes it via a dedicated `delete+insert`-style strategy (`snowflake__get_incremental_microbatch_sql` in `dbt/include/snowflake/macros/materializations/incremental/merge.sql`) — this is real, current adapter support, not a strategy Snowflake merely tolerates.
- For this specific use case — a time-series event stream, a fixed-size trailing lookback window sized to catch late arrivals, and an explicit historical backfill — `microbatch` is a better fit than hand-writing `is_incremental()` + `merge` + `unique_key`, for reasons specific to this problem rather than "newer is better":
  1. **Lookback is a first-class config**, not a hand-rolled subquery. A manual approach would need something like `where event_timestamp >= (select dateadd('day', -3, max(event_timestamp)) from {{ this }})` inside an `is_incremental()` block — self-adjusting to the target's own watermark, but also a second, informal spec for "how far back do we look" that lives in SQL rather than in `dbt show`-able model config.
  2. **Native backfill CLI**, not a custom `--vars`-driven date-range flag. `--event-time-start`/`--event-time-end` (below) is a documented, versioned dbt feature; a hand-written equivalent would need its own `{% if var(...) %}` scaffolding and its own documentation of what the var means.
  3. **Delete+insert per batch window, not merge-on-key, is actually the better correctness property here.** A `merge` only *upserts* rows matching the query's current output — it can never remove a row that legitimately disappeared from a batch's result set (e.g. a row that was quarantined after initially passing). `microbatch`'s Snowflake execution deletes the *entire* target window for a batch and reinserts the query's current result for that exact window, so a reprocessed batch always reflects a full, correct recomputation of that window, not an incremental patch on top of a possibly-stale one.
  4. **Per-batch execution, logging, and retry.** Each batch is its own `START`/`OK` log line and its own unit of work for `dbt retry` (failed batches retry independently). A hand-written `merge` is one statement per invocation with no equivalent granularity.
- The one real trade-off, noted for honesty rather than glossed over: a hand-written `max(event_timestamp)`-based watermark self-heals if a run is skipped for several days (the window automatically widens to cover the gap), whereas `microbatch`'s lookback is a fixed batch count relative to the run's own end time, regardless of how long since the last successful run. In production this is addressed by sizing `lookback` to the realistic maximum gap between runs, or by an explicit `--event-time-start`/`--event-time-end` catch-up (exactly the mechanism demonstrated below) — not a reason to prefer the manual approach for this use case.

### Decision: materialization-default conflict, resolved via per-model config override

`dbt_project.yml` sets `intermediate: +materialized: ephemeral` — intentional, per its own comment, because intermediate models in this project have no direct consumer and this project's schemas are already per-PR-ephemeral. An incremental model is fundamentally incompatible with `ephemeral` (there is no persisted relation for `is_incremental()`/microbatch to check or write to). `int_web_events_incremental.sql` (`dbt/models/intermediate/web/`) resolves this with a model-level `{{ config(materialized='incremental', ...) }}` block, which dbt always resolves with higher precedence than a project-level default — documented as a code comment directly above the config block in the model file, not only here.

The model still lives under `models/intermediate/web/` (not a new top-level directory) because it is, conceptually, exactly what this project's intermediate layer is for — reshaping a staging model for consumption by a future marts-layer model — it just happens to need a persisted, incrementally-built table rather than an ephemeral CTE to do that reshaping correctly.

### Found and fixed during implementation: `TIMESTAMP_NTZ` silently produced wrong results at a UTC day boundary

The same discipline ADR-002 (missing GBP rate) and ADR-004 (TRY_CAST) applied — verify against real behavior, don't assume a cast is correct just because it compiles — caught a real bug in already-merged, closed work (ADR-004/Issue #29).

`web_events_parsed()` originally cast `event_timestamp`/`ingested_at` to `TIMESTAMP_NTZ`. The source values are UTC ISO-8601 strings (`...Z` suffix). A full backfill run (`--event-time-start 2025-01-01 --event-time-end 2026-01-01`) was verified directly against `stg_web__events` afterward — 999 distinct `event_id` values landed in `int_web_events_incremental` against 1,000 in `stg_web__events`, a real discrepancy, not a passing "the run succeeded" check. An anti-join found the missing row: `event_id = 'fff099c5-a6ff-4252-a074-722270518f93'`, `event_timestamp = 2025-12-31 20:05:26`.

Root cause, confirmed directly: dbt's microbatch execution always builds its batch-boundary predicates with `to_timestamp_tz(...)` (an explicit UTC offset). Comparing a `TIMESTAMP_NTZ` column against a `TIMESTAMP_TZ` literal forces Snowflake to promote the `TIMESTAMP_NTZ` value using the **session's `TIMEZONE` parameter** — confirmed via `show parameters like 'TIMEZONE' in session`, this project's Snowflake session defaults to `America/Los_Angeles` (UTC-8 in December), not UTC. A `TIMESTAMP_NTZ` value of `2025-12-31 20:05:26`, promoted as if it were `2025-12-31 20:05:26-08:00`, is `2026-01-01 04:05:26` UTC — 8 hours later than its true UTC instant, and past the backfill's `2026-01-01` upper boundary. Reproduced directly with a synthetic literal:

```sql
select
  try_cast('2025-12-31T20:05:26Z' as timestamp_tz) < to_timestamp_tz('2026-01-01 00:00:00+00:00')  -- true
;
-- vs. the NTZ column's actual promoted comparison in this session's timezone:
-- select '2025-12-31 20:05:26'::timestamp_ntz < to_timestamp_tz('2026-01-01 00:00:00+00:00')  -- false, off by the session's UTC offset
```

This is a genuine, general defect (not specific to the one dropped row): any `TIMESTAMP_NTZ` event whose true UTC instant falls in the last few hours of a UTC calendar day gets attributed to the *next* day's microbatch window instead of its own. For a batch in the middle of a contiguous backfill this only misattributes which batch's `INSERT` a row came through (the row's own stored column values are unaffected, and no duplicate is created); at the *outer edge* of any bounded run's date range, a misattributed row has nowhere to land and is silently dropped — exactly what happened to the one boundary row above.

**Fix:** `web_events_parsed()` now casts both fields to `TIMESTAMP_TZ` (`dbt/macros/web_events_parsed.sql`), which preserves the source's explicit UTC offset through every later comparison, session timezone notwithstanding. `stg_web__events`/`stg_web__events_quarantine` were rebuilt and all 36 pre-existing schema/singular tests re-run and confirmed passing (no regression). ADR-004's status line is amended to point here. The full backfill (below) was then re-run from a clean table and verified to reconcile exactly against `stg_web__events` (1,000 = 1,000, zero duplicates).

### Lookback semantics, precisely

Confirmed directly against `dbt/materializations/incremental/microbatch.py` (`MicrobatchBuilder`), not assumed from the conceptual docs summary alone:

- **Batch window is `[batch_start, batch_end)`** — inclusive start, exclusive end — at `batch_size` granularity (`day`, here). Confirmed both from `snowflake__get_incremental_microbatch_sql`'s generated predicates (`event_timestamp >= start and event_timestamp < end`) and from `MicrobatchBuilder.ceiling_timestamp`'s docstring.
- **`lookback: 3` means "reprocess the batch containing the run's end time, plus the 3 calendar-day batches before it" — 4 batches touched per normal incremental run, not 3.** This is not an edge case: `build_start_time()`'s checkpoint is always `build_end_time()`'s return value, which is always already `batch_size`-aligned (a `ceiling_timestamp` result), so the `if checkpoint == truncate_timestamp(checkpoint, batch_size): lookback += 1` branch fires on every normal run. Reproduced directly: with `lookback=3`, `batch_size=day`, and end truncated to `2025-09-17 00:00:00`, `build_start_time` returns `2025-09-13 00:00:00` — 4 daily batches (`Sept 13, 14, 15, 16`), confirmed by both hand-tracing the source and by the actual `dbt run` log below (`Batch 1 of 4` … `Batch 4 of 4`).
- **`--event-time-start`/`--event-time-end` bypass lookback entirely and are mutually required.** Passing an explicit start always wins over the lookback computation (`build_start_time` returns the explicit value before ever consulting `lookback`). Empirically confirmed: `dbt run --event-time-end "2025-09-17"` alone (no `--event-time-start`) fails fast with `DbtUsageException: The flag --event-time-end was specified, but --event-time-start was not.` This means there is no CLI-only way to trigger "a normal incremental run, lookback auto-computed" against historically-dated data without also fixing "now" to a real wall-clock instant — which for this project's fixed 2025 dataset (run in 2026) is never inside the data's range anyway. The demonstration below therefore passes `--event-time-start` explicitly, set to the exact value the lookback algorithm computes for the equivalent `--event-time-end` (shown above) — mechanically identical (same batches, same per-batch delete+insert SQL) to what an unattended scheduled run with `lookback: 3` would execute; only the origin of the `start` value (explicit flag vs. an internal computation) differs.

### Demonstration: late-arrival absorption without a full rebuild

All steps run against real data — one genuine late-arriving event (`event_id = 4548fea3-ce03-4d8f-8570-82590219db86`, `event_timestamp = 2025-09-13 01:12:44 UTC`, `ingested_at = 2025-09-16 01:12:44 UTC`, a 3-day delay) was temporarily removed from `DEV_ANALYTICS.RAW.RAW_WEB_EVENTS` and, after the initial run, reinserted byte-for-byte identical to simulate its real (delayed) arrival — mirroring ADR-003's precedent of a real, one-time manual DML operation against `RAW` to demonstrate a scenario the static synthetic dataset can't otherwise exhibit on its own (`RAW_WEB_EVENTS` is a fully-loaded, one-time extract, not a live trickling feed — there is no other way to make a row "not exist yet" and then "arrive"). This was confirmed with the user before running. No row's `event_id`, timestamps, or payload were altered — only its temporary presence/absence in `RAW`.

1. **Baseline removal**, confirmed directly: `delete from RAW_WEB_EVENTS where event_id = '4548fea3-...'` → 1 row deleted; `RAW_WEB_EVENTS` count 1,050 → 1,049 (999 distinct `event_id`).
2. **Initial run** (target relation does not exist yet — a cold start): `dbt run --select int_web_events_incremental --event-time-start "2025-09-01" --event-time-end "2025-09-16"`. Ran 15 daily batches, `Sept 1` through `Sept 15`. Verified directly: 45 total rows, 45 distinct `event_id` (matches `stg_web__events` for the same window exactly), late event absent (`count_if(event_id = '4548fea3-...') = 0`), `event_date = 2025-09-13` shows 3 rows (the 3 on-time events for that day, not 4).
3. **Reinsertion**, confirmed directly: the identical JSON payload reinserted into `RAW_WEB_EVENTS` → count back to 1,050 (1,000 distinct `event_id`).
4. **Second run**, exercising only the lookback window: `dbt run --select int_web_events_incremental --event-time-start "2025-09-13" --event-time-end "2025-09-17"`. Real log output:

   ```text
   Batch 1 of 4 START batch 2025-09-13 of RAW.int_web_events_incremental
   Batch 1 of 4 OK created batch 2025-09-13 of RAW.int_web_events_incremental
   Batch 2 of 4 START batch 2025-09-14 of RAW.int_web_events_incremental
   Batch 3 of 4 START batch 2025-09-15 of RAW.int_web_events_incremental
   Batch 2 of 4 OK created batch 2025-09-14 of RAW.int_web_events_incremental
   Batch 3 of 4 OK created batch 2025-09-15 of RAW.int_web_events_incremental
   Batch 4 of 4 START batch 2025-09-16 of RAW.int_web_events_incremental
   Batch 4 of 4 OK created batch 2025-09-16 of RAW.int_web_events_incremental
   ```

   4 batches only — not a rebuild of all 15 previously-processed batches, let alone a full-history rebuild — confirming the lookback window itself, not a broader rebuild, is what's responsible for the result below.
5. **Verification, direct queries, before vs. after:**
   - Total rows: 45 → 49 (+4: the 1 late event on `Sept 13`, plus 3 new rows for `Sept 16`, a day the initial run's narrower range never touched at all — not +1 alone, and not evidence of duplication).
   - Duplicate check (`group by event_id having count(*) > 1`): **0 rows**, both before and after.
   - Late event present exactly once: `count_if(event_id = '4548fea3-...') = 1`.
   - `event_date = 2025-09-13` now shows 4 rows (the 3 on-time events, unchanged, plus the late one) — matching `stg_web__events`'s own count for that date exactly.
   - Rows with `event_date < 2025-09-13` (outside the reprocessed window): unchanged at 37, both before and after — direct proof the days outside the lookback window were untouched.
   - **Effective event-time window processed by the second run:** `[2025-09-13 00:00:00 UTC, 2025-09-17 00:00:00 UTC)`, taken directly from the batch log above. The late event's `event_timestamp` (`2025-09-13 01:12:44 UTC`) falls inside the first batch of that window — within the configured lookback boundary, not merely inside a coincidentally-wide manual range.

### Backfill procedure

A full historical backfill was run against the complete confirmed data range (`2025-01-05` to `2025-12-31`), using dbt's documented backfill mechanism — `--event-time-start`/`--event-time-end` — not `--full-refresh` (the target relation was left in place from the steps above; `--full-refresh` was never invoked against this model in this demonstration):

```text
dbt run --select int_web_events_incremental --event-time-start "2025-01-01" --event-time-end "2026-01-01"
```

365 daily batches ran (`2025-01-01` through `2025-12-31`), all `OK`, `Completed successfully`, in 311 seconds wall-clock (Snowflake `TRANSFORM_XS`, 4 threads):

```text
Finished running 1 incremental model in 0 hours 5 minutes and 11.21 seconds (311.21s).
Completed successfully
Done. PASS=1 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=1
```

Verified directly afterward against `stg_web__events` (the canonical row count established in ADR-004): **1,000 rows in `int_web_events_incremental`, 1,000 distinct `event_id`, exactly matching `stg_web__events`'s 1,000 rows** — an anti-join (`stg_web__events` rows with no matching `event_id` in `int_web_events_incremental`) returns 0 rows, and the duplicate-check singular test's query (`group by event_id having count(*) > 1`) also returns 0 rows. `min(event_date)`/`max(event_date)` are `2025-01-05`/`2025-12-31`, matching the confirmed raw data range exactly. The previously-demonstrated late event remains present exactly once.

### Tests

- `unique`/`not_null` on `int_web_events_incremental.event_id`, plus `not_null` on every required field, mirroring `stg_web__events`'s own tests.
- `assert_int_web_events_incremental_no_duplicate_event_id` (`dbt/tests/`), a singular test mirroring `assert_erp_orders_status_snapshot_single_open_version` from Issue #17: `group by event_id having count(*) > 1` must return zero rows. This is the same reprocessing-safety invariant demonstrated manually above, made a durable, automated regression check rather than a one-time verification.

### Consequences

- `int_web_events_incremental` reconciles exactly with `stg_web__events` (1,000 = 1,000 rows, 0 duplicates, 0 missing) after the full backfill — the model is a correct, complete materialization of the staging view's current data, not merely "the run didn't error."
- The `TIMESTAMP_NTZ` → `TIMESTAMP_TZ` fix applies to every future consumer of `stg_web__events.event_timestamp`/`ingested_at`, not just this model — any future time-boundary comparison against these columns would have hit the same session-timezone-dependent bug. This is now closed for the whole project, not patched around locally in `int_web_events_incremental`.
- `event_id` is declared as this model's `unique_key` for documentation/intent even though Snowflake's microbatch `delete+insert` execution doesn't use it operationally; the real duplicate-safety property is event_timestamp's immutability per event_id (ADR-004) plus the `unique`/`not_null` schema tests and the new singular test. A future adapter change (or a future model on an adapter that uses `merge` for microbatch, e.g. dbt-postgres) would need `unique_key` for correctness on that adapter — it's cheap to declare now and correct to rely on later.
- Because dbt-core requires `--event-time-start`/`--event-time-end` together, there is no way to invoke "a normal scheduled incremental run" against this historically-dated dataset without either fixing wall-clock time or passing an explicit, lookback-equivalent start. Any future demo or CI job against this model should pass both flags explicitly (as done here) rather than relying on default "now"-based behavior, which would silently no-op against 2025-dated data run in any later year.
- `stg_web__events`'s `event_time` config (`event_timestamp`) is purely additive metadata for this microbatch consumer; `stg_web__events` itself is unchanged in materialization or row output (still a view, still 1,000 rows, still passing all of ADR-004's original tests).

## ADR-006: CRM staging — country standardization, and why "ghost accounts" is one mechanism, not two

**Status:** Accepted (2026-09-11).
**Phase:** Phase 3C (this PR — Issue #33). Raw loading already happened in Phase 3A's scaffold (`data_gen/load_raw.py` already loads `CRM_CUSTOMERS`/`CRM_TICKETS`); cross-system identity resolution against ERP via `contact_email` is Phase 5.

### Context

The build plan for this phase described the ghost-account mechanism as "soft-delete inference based on last-seen date." Before writing any model, that description was checked directly against `docs/synthetic_data_spec.md`, `data_gen/crm.py`, and the live `DEV_ANALYTICS.RAW.CRM_CUSTOMERS`/`CRM_TICKETS` tables — and it does not match what the source actually does. There is no soft-delete flag, no inactivity threshold, and no use of `last_seen_date` for anything at all in the generator. `data_gen/crm.py`'s `build_account()` populates `last_seen_date` with a plain `fake.date_between(created_date, today())` call and nothing downstream reads it. The only anomaly the generator actually injects is described in the spec under "Ghost accounts": `CRM_TICKETS.account_id` values, drawn from a pool numbered immediately past the real `ACCT-#####` range (`GHOST_ACCOUNT_RATE = 0.04`, `GHOST_ACCOUNT_POOL_SIZE = 25` in `data_gen/crm.py`), so that ~4% of tickets reference an account that was never written to `CRM_CUSTOMERS.csv` at all.

This was confirmed directly against the loaded raw tables, not assumed from the generator source alone:

- `CRM_CUSTOMERS`: 350 rows, 350 distinct `account_id` (unique, no duplicate-key condition to reconcile), 0 nulls in any of the six columns.
- `CRM_TICKETS`: 900 rows, 900 distinct `ticket_id` (unique).
- Anti-join of `CRM_TICKETS.account_id` against `CRM_CUSTOMERS.account_id`: **36 of 900 tickets (4.0%, matching `GHOST_ACCOUNT_RATE`) reference 19 distinct account_ids** that do not exist in `CRM_CUSTOMERS` at all. Sampled directly: the referenced ids (e.g. `ACCT-00359`, `ACCT-00365`, `ACCT-00368`, `ACCT-00374`) all fall in the `351`-`375` range immediately past the 350 real accounts, exactly matching `data_gen/crm.py`'s ghost pool construction.

**There is only one anomaly condition here, not two.** The build plan's phrasing implied a second, distinct condition -- an inactive-but-present customer record, inferred from a stale `last_seen_date` -- alongside the orphan-reference condition. That second condition does not exist anywhere in the synthetic data or its generator: `last_seen_date` carries no documented staleness semantics in `docs/synthetic_data_spec.md`, and `CRM_CUSTOMERS` has no inactive/soft-delete flag of any kind. Building a second, invented "is_stale" flag against an undocumented, arbitrarily-chosen threshold would have modeled a condition the source doesn't actually inject, and no test could ever establish a "verified count" for it the way ADR-002/ADR-004's found-and-fixed sections do for real defects. This PR therefore implements exactly one flag, for the one condition that is actually present.

### Decision: country standardization via a var + macro, not a seed

`docs/synthetic_data_spec.md` ("Dirty country formatting") and `data_gen/crm.py`'s `dirty_country()`/`US_COUNTRY_VARIANTS` were checked against the loaded raw data before deciding on an approach. Confirmed directly: `country` on `CRM_CUSTOMERS` is dirty on exactly one axis -- the United States is rendered as `US` (36 rows), `USA` (37), `United States` (33), or `u.s.a.` (28), while every other country is already a clean two-letter code (`DE` 58, `IT` 42, `FR` 41, `ES` 40, `NL` 35). `name` and `contact_email` carry no dirty text.

Four fixed string variants, all collapsing to a single canonical value, is a handful of mappings, not "many" -- this is a dbt var (`crm_us_country_variants`, `dbt_project.yml`) plus a macro (`crm_standardize_country()`, `dbt/macros/crm_standardize_country.sql`), matching this project's own precedent (`erp_convert_to_usd()` + `erp_eur_to_usd_rate`/`erp_gbp_to_usd_rate`, ADR-002) rather than a seed file -- a seed would be the right call for a large or open-ended mapping table, which this is not.

The var is a flat list (`["US", "USA", "United States", "u.s.a."]`), not a `variant -> canonical` dict, because every variant in the actual data maps to the same canonical value (`US`); the macro hardcodes `'US'` as that one target. This mirrors ERP's own pattern of separate scalar vars per currency rather than one dict-shaped var, and avoids designing a general many-to-many mapping structure this dataset doesn't need yet -- if a future country ever needed its own distinct canonical target, the var/macro would grow into a dict at that point, not before.

**Found and fixed during implementation:** the original design used a `variant -> canonical` dict var, iterated in the macro with `{% for variant, canonical in var(...).items() %}`. This compiles and runs correctly under `dbt`, but fails `sqlfluff lint`: sqlfluff's jinja templater (not the `dbt` templater -- see ADR-002's note that this project's `.sqlfluff` doesn't use it) mocks `var()` with a fixed `VarEmulator` string placeholder regardless of the variable name or the templater's context, and calling `.items()` on it raises `'item' is not safely callable` inside sqlfluff's sandboxed Jinja environment. The flat-list form sidesteps this: `for variant in var(...)` iterates the mock string's characters (a harmless, syntactically valid no-op under lint) instead of calling an unsupported method on it, so both `sqlfluff lint` and `dbt run` render successfully from the same macro source -- confirmed directly (see Consequences).

### Decision: `is_ghost_account`, a flag on `stg_crm__tickets`, not a drop or a separate quarantine model

`stg_crm__tickets` left-joins to `stg_crm__customers` on `account_id` and exposes `is_ghost_account` (`customers.account_id is null`) rather than either dropping orphaned tickets or routing them to a `quarantine`-style model like `stg_erp__order_items_quarantine`/`stg_web__events_quarantine`. Two reasons this differs from the quarantine pattern used elsewhere in this project:

1. **The row itself isn't invalid.** ERP/web quarantine catches row-level validation failures -- a missing business key, an unparseable timestamp -- where the row is genuinely malformed. A ghost ticket has every field populated and valid; only its foreign-key reference to `CRM_CUSTOMERS` fails to resolve. That's a cross-table reconciliation condition, not a row-level defect, and the ticket's own history (category, status, created_date) is real data worth keeping visible in the main model, not worth hiding in a side table.
2. **Not dropping is an explicit requirement here** (see the CRM issue's and this phase's build plan) -- a real analyst reconciling CRM tickets against the customer master would need to see and count orphaned account references like these, not have them silently vanish from `stg_crm__tickets`.

`stg_crm__customers` needs no equivalent flag or quarantine split of its own: it is the *target* of the anti-join, not the source of the anomaly, and (per Context above) contains no null/duplicate-key/malformed rows to route anywhere -- confirmed directly, 0 nulls across all 350 rows and 350 distinct `account_id`.

### Test: flag correctness, not a hardcoded count

`assert_stg_crm__tickets_ghost_flag_matches_antijoin` (singular test, `dbt/tests/`) independently recomputes the anti-join between `stg_crm__tickets` and `stg_crm__customers` and asserts `is_ghost_account` agrees with it on every row. This checks the mechanism itself as a regression guard -- it would fail if a future refactor of `stg_crm__tickets.sql` broke the join logic -- rather than asserting a fixed row count (`36`) that would silently need updating on every regeneration of the synthetic data with a different `--tickets`/`--seed`. The 36-tickets/19-distinct-accounts figures above are the actual, verified count for the current default-seeded dataset (recorded here, not encoded as a brittle test threshold), matching this project's precedent of documenting real counts in the ADR narrative (ADR-002, ADR-003) rather than only in test assertions.

### Consequences

- `stg_crm__customers` holds all 350 rows (no drop/quarantine at this layer); `country` is fully standardized to `{US, DE, FR, NL, ES, IT}` -- confirmed directly post-build: 134 `US` rows (37+36+33+28, the four collapsed variants) plus the five untouched EU codes at their original counts.
- `stg_crm__tickets` holds all 900 rows; `is_ghost_account` is `true` for exactly 36 (4.0%) and `false` for 864, matching the raw anti-join exactly (confirmed via `assert_stg_crm__tickets_ghost_flag_matches_antijoin`, which passes with 0 mismatched rows).
- Any future model that joins CRM tickets to CRM customers (or resolves CRM identity against ERP in Phase 5) has a ready-made, tested signal for "this ticket's account reference doesn't resolve" without needing to re-derive the anti-join itself.
- `.sqlfluff` needed no new configuration for this PR beyond what ADR-002 already established (`load_macros_from_path = dbt/macros`); the flat-list var design was chosen specifically so that stayed true, rather than adding a `sqlfluff:templater:jinja:context` override or a lint-only macro shim to work around a dict-shaped var.
- Because the build plan's "soft-delete inference based on last-seen date" premise doesn't hold, `last_seen_date` is carried through `stg_crm__customers` unchanged and untested beyond `not_null` -- it remains available for a future model that has an actual documented reason to use it, but nothing in this PR treats it as a staleness signal.

## ADR-007: Source database targeting, freshness thresholds, dbt_utils integration

**Status:** Accepted (2026-09-11).
**Phase:** Phase 4 (this PR). Follows Phases 3A/3B/3C (Issues #16/#17, #29/#31, #33) -- the 1:1 staging views themselves are unchanged here; this PR is cross-cutting hardening across all three sources' `_*__sources.yml` files plus one genuine staging-model improvement found while investigating.

### Context: the `database: DEV_ANALYTICS` bug

A peer review flagged that `dbt/models/staging/{erp,web,crm}/_*__sources.yml` all hardcoded `database: DEV_ANALYTICS` at the source level. This directly contradicts `docs/workflow.md`'s Environment Promotion section: a STAGE/PROD run (once Phase 8 auto-deploy exists) would still read `DEV_ANALYTICS.RAW.*` regardless of which environment it actually ran against, silently defeating the entire promotion story -- STAGE and PROD would never see their own raw data, they'd just re-read DEV's.

### Decision: omit `database:` entirely, don't template it

Checked directly against the installed dbt-core 1.12.4 parser source (`dbt/parser/sources.py:155-159`) rather than assumed from docs -- the docs themselves are ambiguous here (fetched both `docs/build/sources` and `reference/source-properties`; neither states the omitted-`database` default plainly):

```python
default_database = self.root_project.credentials.database
...
database=(source.database or default_database),
```

Omitting `database:` from a source resolves it to `target.database` -- the exact per-environment value this project needs (`DEV_ANALYTICS`/`STAGE_ANALYTICS`/`PROD_ANALYTICS`, per `docs/dbt_profile_setup.md` and `terraform/databases.tf`). All three `_*__sources.yml` files now omit the key rather than writing `database: "{{ target.database }}"` -- both resolve identically, but the explicit Jinja is redundant given the confirmed default, and omission is the more idiomatic (per current dbt-core 1.12 behavior) expression of the same intent, consistent with this project's own precedent (ADR-003) of preferring the documented current pattern over a familiar-but-unnecessary one.

**Verified against the actual compiled/resolved relation, not just the YAML.** A temporary `stage` output was added to `~/.dbt/profiles.yml` (`database: STAGE_ANALYTICS` -- a real database, provisioned by Terraform in Phase 2 per `terraform/databases.tf`, just with no `RAW` tables loaded into it yet) and removed again after verification; it is not committed anywhere. `dbt compile --target stage` produced:

```text
from STAGE_ANALYTICS.RAW.US_ORDERS
from STAGE_ANALYTICS.RAW.EU_ORDERS
from STAGE_ANALYTICS.RAW.CRM_CUSTOMERS
from STAGE_ANALYTICS.RAW.RAW_WEB_EVENTS
```

against the same models that compile to `DEV_ANALYTICS.RAW.*` under the normal `dev` target -- direct proof the fix changes the resolved database per target, not just that the YAML looks right. A repo-wide grep afterward confirms no `database:` key anywhere in the three source files still names `DEV_ANALYTICS`; the one remaining textual match is this ADR's own prose describing the bug, and each source file's description prose explaining the fix.

### Decision: freshness thresholds and the column each is keyed on

Freshness needed a real ingestion-recency signal per source, not a business timestamp used merely because it exists. Checked what each source actually has, against `docs/loading_notes.md`'s documented raw table shapes:

- **ERP (`us_orders`/`eu_orders`) and CRM (`crm_customers`/`crm_tickets`) have no per-row ingestion timestamp column at all** -- only business dates (`order_date`/`placed_on`, `created_date`/`last_seen_date`). Using one of those would answer "when did this order/account happen," not "is this table's data current" -- the wrong question, and exactly the mistake this task was scoped to avoid.
- **Web (`raw_web_events`) has an `ingested_at` field, but despite its name it is not a valid freshness signal either.** The original version of this ADR (accepted earlier the same day, 2026-09-11) used it as one and was wrong to: `ingested_at` is synthetic business data, not a load-time capture column. `data_gen/clickstream.py` embeds it into each row at generation time to simulate pipeline-arrival lag for the late-arrival demo (ADR-004/ADR-005, distinguishing it from `event_timestamp` = when the click happened). Like `erp.py`/`crm.py`'s `AS_OF_DATE`, it is pinned to a fixed 2025 window (`EVENT_START`/`EVENT_END`) and never advances toward wall-clock "now" -- so a freshness check keyed on it reports the row's simulated arrival time, not whether Snowflake actually loaded the table recently. This is a category error: a field being named and shaped like a timestamp doesn't make it a valid freshness signal, only checking what real-world event it represents does. That lesson generalizes past this one column, which is why the fix below now applies uniformly to all three sources rather than treating web as a special case.

For all three sources, the fix is Snowflake's warehouse-metadata freshness method (`loaded_at_field` omitted everywhere, web included), not any per-row column. Confirmed directly against the installed packages, not assumed from a web search that returned inconsistent claims about which dbt version/edition supports this:

- `dbt/task/freshness.py` falls back to `self.adapter.supports(Capability.TableLastModifiedMetadata)` when no `loaded_at_field`/`loaded_at_query` is configured, calling `adapter.calculate_freshness_from_metadata(...)`.
- `dbt/adapters/snowflake/impl.py`'s `_capabilities` dict declares `Capability.TableLastModifiedMetadata: CapabilitySupport(support=Support.Full)` -- full, real support in dbt-snowflake 1.12.0, present since dbt-core 1.7, not a Fusion/"v2"-only feature as some search results implied.
- The actual query, read from `dbt/include/snowflake/macros/metadata.sql`'s `snowflake__get_relation_last_modified`: a single `select ... last_altered as last_modified ... from {database}.information_schema.tables where table_schema = ... and table_name = ...`, one row per relation. `dbt/adapters/base/impl.py`'s `calculate_freshness_from_metadata` (via `_create_freshness_response`) computes `age` as `snapshotted_at - max_loaded_at` straight from that `last_altered` value -- no per-row scan, just the table object's own metadata.
- `data_gen/load_raw.py` does a full `CREATE OR REPLACE TABLE` on every run for all five raw tables, `RAW_WEB_EVENTS` included -- `main()` calls the same `load_table()` function in the same loop for `JSON_SOURCE` as for every `CSV_SOURCES` entry (verified by reading the loader itself rather than assumed from ERP/CRM's symmetry). So `LAST_ALTERED` exactly equals the last load time for all three sources, web included -- an honest ingestion-recency signal with no dedicated column needed anywhere.

**Thresholds:** `warn_after: 24 hours`, `error_after: 48 hours`, uniformly across all three sources. No real refresh SLA is documented anywhere in this project for this synthetic batch loader, so a single nightly-batch-cadence assumption (missed one cycle = warn, missed two = error) is used everywhere rather than inventing three different, equally-unfounded SLAs.

**Verified end-to-end against real data, not just configured.** First `dbt source freshness` run for real against `DEV_ANALYTICS`, at the point this ADR still keyed web on `ingested_at`:

```text
Pulling freshness from warehouse metadata tables for 4 sources
4 of 5 WARN freshness of erp.us_orders
1 of 5 WARN freshness of crm.crm_customers
3 of 5 WARN freshness of erp.eu_orders
2 of 5 WARN freshness of crm.crm_tickets
5 of 5 ERROR STALE freshness of web.raw_web_events
```

`target/sources.json` confirmed this against the raw tables' actual timestamps at the time: `erp.us_orders`/`eu_orders` and `crm.crm_customers`/`crm_tickets` all showed `max_loaded_at` ~25-26 hours before the check ran (these tables were reloaded the same day as this PR's prior commit, `e256d43`'s date-determinism fix verification) -- correctly landing in the WARN band (24-48h). `web.raw_web_events` showed `max_loaded_at = 2025-12-31T20:07:20Z`, ~254 days before the check ran -- not a real staleness signal, but `ingested_at`'s frozen synthetic value, i.e. exactly the category error identified above.

**Re-verified the same day, after correcting web to warehouse metadata.** `data_gen/load_raw.py` was rerun in full (reloads all five raw tables via `CREATE OR REPLACE TABLE`, `RAW_WEB_EVENTS` included), immediately followed by `dbt source freshness`:

```text
Pulling freshness from warehouse metadata tables for 5 sources
2 of 5 PASS freshness of crm.crm_tickets
4 of 5 PASS freshness of erp.us_orders
1 of 5 PASS freshness of crm.crm_customers
3 of 5 PASS freshness of erp.eu_orders
5 of 5 PASS freshness of web.raw_web_events
```

`target/sources.json` shows `web.raw_web_events`'s `max_loaded_at = 2026-09-11T19:54:44.545Z`, `max_loaded_at_time_ago_in_s = 7.514` -- a value distinct from, and independent of, each of the other four sources' own `max_loaded_at` (each landed a few seconds apart, matching the loader's per-table `CREATE OR REPLACE TABLE` sequence), and nowhere near the old frozen `2025-12-31` value. This is the actual proof the mechanism tracks live state, not merely that the status improved: an improved status (WARN or PASS instead of ERROR) could in principle happen by coincidence, but `max_loaded_at` moving to track a real reload cannot -- it demonstrates the same live behavior ERP/CRM's `LAST_ALTERED`-based freshness already had.

**No longer a standing consequence -- this was the bug, not an accepted tradeoff.** The original version of this section reasoned that because `data_gen/clickstream.py` pins `ingested_at` to a fixed 2025 window (`EVENT_START`/`EVENT_END`, matching `erp.py`/`crm.py`'s own `AS_OF_DATE` determinism fix), `dbt source freshness` against `raw_web_events` would report ERROR under real wall-clock time indefinitely, and it accepted that as a documented, deliberate consequence rather than a defect. That framing was itself wrong: the embedded-timestamp problem wasn't an acceptable tradeoff to document, it was proof `ingested_at` was the wrong column for this check. The fix was not to inflate `error_after` until the status went green -- that would still be hiding a real signal (in this case, "this column doesn't measure load recency") behind a threshold chosen to avoid a red status, which the original reasoning correctly rejected -- the fix was to stop using `ingested_at` for freshness at all, matching ERP/CRM's warehouse-metadata approach, which has no equivalent problem since it measures the table object's real modification time, not a value frozen inside the synthetic rows. `ingested_at` itself is unchanged by this fix: it still drives `stg_web__events`' late-arrival logic and `int_web_events_incremental`'s lookback window exactly as before (ADR-004/ADR-005) -- only its (mistaken) use for source freshness was removed.

### Decision: dbt_utils, version selection

Checked the current release directly against GitHub's releases API (`api.github.com/repos/dbt-labs/dbt-utils/releases`) rather than assuming whatever `dbt deps`/the dbt Hub UI would resolve as latest: **1.4.1**, published 2026-06-28. Its own `dbt_project.yml` declares `require-dbt-version: [">=1.3.0", "<3.0.0"]`, which covers this project's pinned `dbt-core==1.12.4`. Pinned to the exact version (`packages.yml`: `version: 1.4.1`), matching `requirements.txt`'s existing exact-pin convention for `dbt-core`/`dbt-snowflake` rather than a range. `dbt deps` succeeded (`dbt/package-lock.yml`, sha1 `8b27037b26f3f630c6661194d2470e720c49f6ee`).

### Decision: `generate_surrogate_key()` replaces manual key concatenation

`stg_erp__order_items.order_item_key` and `stg_erp__orders.order_key` were both manual `||`-concatenation composite keys -- `region || '-' || source_order_id || '-' || source_line_item_id::varchar` and `region || '-' || order_id` respectively (ADR-002). This is exactly `dbt_utils.generate_surrogate_key()`'s purpose, and a genuine two-model reuse case within the same source (not a single call site dressed up as a "pattern"). Both were replaced:

```sql
{{ dbt_utils.generate_surrogate_key(['region', 'source_order_id', 'source_line_item_id']) }}
    as order_item_key
```

**Why this is a real fit, not a forced one:** grepped the whole repo for `order_item_key`/`order_key` before changing anything -- both are only ever consumed via `unique`/`not_null` schema tests, and `erp_orders_status_snapshot`'s own `unique_key: [region, order_id]` config uses the raw columns directly, not the derived key column at all (ADR-003 already noted this). Nothing anywhere parses the literal delimited-string format, so switching it to an md5 hash breaks nothing. It's also a strict correctness improvement, not just a style change: `generate_surrogate_key()` coalesces each field to a distinct null sentinel (`_dbt_utils_surrogate_key_null_`) before concatenating, so a future nullable field wouldn't silently collapse the whole key to `NULL` the way `a || b` does today. Current data has no nulls in these fields (ADR-002 confirmed this directly), so the manual version isn't *currently* wrong -- but it offered no protection if that ever changed, and the replacement costs nothing to get for free.

**What was considered and rejected as a dbt_utils fit:** `erp_convert_to_usd()`/`crm_standardize_country()` (var-driven `CASE` mappings with no dbt_utils equivalent -- genuinely source-specific business logic) and `erp_quarantine_reason()`/`web_quarantine_reason()` (multi-condition validation `CASE` statements with a reason string per branch -- dbt_utils ships generic single-condition test macros, not this OR'd-conditions-with-a-label shape; forcing it would trade two clear, source-specific statements for a harder-to-follow generic one, achieving nothing). Neither was forced into a dbt_utils macro.

**sqlfluff needed a real fix, not just a re-lint.** `sqlfluff lint` initially failed on the new call sites: `TMP | Undefined jinja template variable: 'dbt_utils'`, cascading into parse errors on the rest of the line. sqlfluff's `jinja` templater (not the `dbt` templater -- ADR-002 already established why this project uses `jinja`) has no concept of an installed dbt package; `apply_dbt_builtins` mocks `ref()`/`source()`/`var()`/etc. individually, but a package-namespaced macro call is just an undefined name to it. Fixed via `.sqlfluff_libs/dbt_utils.py`, a lint-only Python stub (`generate_surrogate_key()` returns a harmless placeholder string literal) exposed through sqlfluff's own `library_path` config option (`.sqlfluff`) -- confirmed this is a real, documented sqlfluff mechanism (`_extract_libraries_from_config` in `sqlfluff/core/templaters/jinja.py`: every top-level module under `library_path` is imported and exposed in the Jinja context by its filename), not an invented workaround. After the fix: `sqlfluff lint dbt/models dbt/macros dbt/snapshots dbt/tests` passes clean, and `dbt run`/`dbt test` both passed for real against `DEV_ANALYTICS` (8/8 models, 64/64 tests -- including fresh `unique`/`not_null` passes on both surrogate-key columns under their new hashed values).

### Decision: no additional custom macro

Investigated whether a second custom macro (beyond `generate_surrogate_key`) has genuine cross-source reuse value. It doesn't, and none was added:

- **Unifying `erp_quarantine_reason()`/`web_quarantine_reason()`** was considered and rejected. Their checks differ in kind, not just column names -- ERP's currency-support check has no web equivalent, web's per-field `TRY_CAST`-null check has no ERP equivalent (ERP's source columns are natively typed; web's are VARIANT-extracted). A shared abstraction over two structurally different validation rulesets would trade two clear, source-specific `CASE` statements for one harder-to-follow config-driven one, removing no real duplication. This mirrors ADR-006's own reasoning for keeping `crm_us_country_variants` a flat list instead of preemptively generalizing to a dict "in case" a future need arrives.
- **Extracting the quarantine-table boilerplate** (`materialized='table'` + `current_timestamp() as quarantined_at` + a `quarantine_reason is not null` filter, repeated across `stg_erp__order_items_quarantine`/`stg_web__events_quarantine`) was considered and rejected. It's one line of real logic (`current_timestamp()`) surrounded by config and a column list that's different in every model -- not enough shared surface to justify a macro, and dbt has no clean way to macro-ize model-level config plus a divergent select list.

No new macro exists in this PR beyond what dbt_utils already provides. This matches ADR-006's own precedent: don't build an abstraction the current model set doesn't actually need.

### Deferred: no Tableau exposure

No dbt `exposures:` entry is added for a Tableau dashboard. Per this project's own build plan, Tableau doesn't exist until Phase 10A -- defining an exposure now would document a downstream consumer that doesn't exist yet, which is worse than no exposure at all (a stale, aspirational entry a future reader might mistake for a real, live dependency). This is an explicit deferral to Phase 10A, recorded here so it isn't later mistaken for an oversight in this PR.

### Consequences

- STAGE/PROD runs (once Phase 8's auto-deploy exists) will read their own environment's raw tables, not DEV's -- the environment-promotion story in `docs/workflow.md` is no longer silently broken. Verified against a real compiled relation under a `stage`-targeted compile, not just inferred from the YAML.
- `dbt source freshness` is a real, working check for all three sources today, not scaffolding for later -- run against real `DEV_ANALYTICS` data in this PR, with results independently verified against each raw table's actual `LAST_ALTERED` metadata, not just "the command exited." All three sources use the same warehouse-metadata mechanism; none is keyed on a per-row column.
- Web's `raw_web_events` freshness initially read `ERROR STALE` permanently under real wall-clock time, because this ADR originally keyed it on `ingested_at` -- a frozen synthetic column, not a real load timestamp (a category error, not an accepted tradeoff). Corrected the same day: web now uses the same `LAST_ALTERED` warehouse-metadata mechanism as ERP/CRM, re-verified end-to-end against a real reload (see above). No source in this project has a permanently-stale freshness check any more.
- `stg_erp__order_items.order_item_key` and `stg_erp__orders.order_key` are now md5 hashes, not readable delimited strings. No consumer anywhere depended on the old format (verified by grep before changing it); any future model reading these columns should treat them as opaque surrogate keys, same as before.
- `dbt_utils` is now a project dependency (`packages.yml`, pinned `1.4.1`) and `.sqlfluff_libs/` exists as a place to add a lint-time stub for any future dbt_utils macro this project calls by namespace -- extend it there rather than reaching for `sqlfluff:templater:jinja:context` overrides.

## ADR-008: Web clickstream identity — from a direct `CUST-#####` copy to sparse, tiered-dirty captured email

**Status:** Accepted (2026-09-11).
**Phase:** Phase 5A pre-work (Issue #40), discovered during Phase 5A's own pre-work, ahead of building the actual identity-resolution model. Amends ADR-004 and ADR-005 (see their Status lines). The identity-resolution model itself (fuzzy-matching web against ERP/CRM) is explicitly out of scope here -- that's Phase 5A's own branch.

### Context

`docs/synthetic_data_spec.md` already documented, in its "Why ERP↔CRM join is deterministic while web is heuristic" section, that web identity was supposed to require heuristic/fuzzy matching -- unlike ERP↔CRM's genuine deterministic email join. But `data_gen/clickstream.py` never actually implemented that: it embedded `f"CUST-{rng.randint(1, 350):05d}"` -- the exact same value ERP US's `customer_id` uses -- directly into `user_id`/`customer_global_id` on every single event. That made web's identity a trivial exact-key match today, not a heuristic problem: `CUST-00042` in a clickstream event equals `CUST-00042` in ERP US byte-for-byte, on 100% of rows. This is a Phase 1 build gap relative to Phase 5's stated design, the same category as ADR-002's missing GBP rate and ADR-005's `TIMESTAMP_TZ` bug -- caught during Phase 5A's pre-work rather than after Phase 5A's model was already built against the trivial version, and fixed at the source rather than worked around downstream.

The build plan's own framing matters here: the generator must not be designed to demonstrate heuristic matching -- it must simulate what a real web analytics pipeline would realistically, imperfectly capture, and let the matching problem fall out of that as a natural consequence. Two properties follow directly from that framing, neither of which the old direct-copy design had: (1) most web traffic is anonymous, so identity should be captured on a minority of events, not all of them; (2) a real pixel doesn't have privileged access to a CRM-grade internal ID -- the only plausible signal it can pick up is something a person or their browser actually surfaces, i.e. an email, and that signal is exactly as reliable as the mechanism that captured it (an autofilled account record vs. a hand-typed checkout field).

### Decision: capture the customer's email, not a synthetic ID -- sparsely, at an event-type-dependent rate

The captured signal is now an email pulled from `data_gen/identity_pool.py` (the same shared pool ERP/CRM already use), not a `CUST-#####` copy. Whether an event captures it at all is a per-event-type probability, reasoned rather than uniform:

| Event type      | Capture rate | Why |
| ---------------- | ------------ | --- |
| `page_view`       | 8%           | Ordinary browsing only carries identity when it happens inside an already-authenticated session -- most page views don't. |
| `search_query`     | 8%           | Same population as `page_view` -- searching doesn't require authentication. |
| `cart_addition`    | 55%          | Materially more likely: closest to checkout, where an account-linked cart or a guest-checkout email capture is common -- but still well under certainty. |

Against the default 1,000-event run: **140/1,000 canonical events (14.0%) carry an identity signal** -- confirmed directly against both the local `data/CLICKSTREAM_IDENTITY_TRUTH.csv` sidecar and, independently, `DEV_ANALYTICS.RAW.stg_web__events.customer_email is not null` (both give 140/1,000).

### Decision: tiered dirtiness among captured signals, mirroring ADR-006's CRM country pattern

Among captured signals, the observed value isn't always the customer's canonical email -- mirroring ADR-006's reasoned, tiered approach to CRM's country dirtiness rather than uniform randomized noise:

- **Exact (~55% of captured events, target)** -- byte-for-byte identical. Represents identity captured programmatically from an account record.
- **Tier 1, trivial normalization (~25% of captured events, target)** -- casing/whitespace noise (`apply_trivial_normalization_noise()`: uppercased, capitalized local-part, or leading/trailing whitespace), resolved by any reasonable lowercase-and-trim step. Represents a human typing/pasting into a guest-checkout field.
- **Tier 2, genuine typo (~20% of captured events, target)** -- a single-character edit on the local part only (`apply_single_character_typo()`: adjacent-character transposition, a dropped character, or a keyboard-adjacent substitution), requiring real fuzzy matching. Bounded to one edit on the local part (domain never touched), with a check-and-retry collision guard against the full identity-pool email set so a distortion can never land on a *different* real customer's email -- confirmed directly: 0 of 28 realized tier-2 distortions triggered the guard's fallback, at this dataset's scale.

Measured against the actual generated data (`data/CLICKSTREAM_IDENTITY_TRUTH.csv`, 140 captured events): **69 exact (49.3% of captured events), 43 tier 1 (30.7% of captured events), 28 tier 2 (20.0% of captured events)** -- close to the target 55/25/20 split of captured events (single-seed sampling variance, not a bug). All 28 tier-2 events map to 28 distinct customers (no repeats), so this isn't a handful of edge cases concentrated on one or two people.

### Decision: field renamed `user_email`/`customer_global_email`, not kept as `user_id`/`customer_global_id`

The mid-year schema-drift mechanism (a field-name change at the fixed 2025-07-01 cutoff) is preserved unchanged in mechanism -- a source system renaming a field mid-year is orthogonal to what type of value that field holds, so there's no reason to drop it. But keeping the old `user_id`/`customer_global_id` names while their content became an email would be actively misleading (an "id" field holding an email reads as a bug to a future reader), so the fields are renamed `user_email` (pre-cutoff) → `customer_global_email` (post-cutoff), coalesced at staging into a single `customer_email` column -- same coalesce mechanism ADR-004 established, same cutoff date, new names reflecting the new content. Every downstream reference was updated: `web_events_parsed()`, `stg_web__events(_quarantine)`, `int_web_events_incremental`, their `.yml` schemas, `docs/synthetic_data_spec.md`, and `docs/loading_notes.md`.

### Decision: `customer_email` is no longer `not_null`-tested, and is removed from `web_quarantine_reason()`

Before this fix, every event carried an identity value, so `not_null`/a quarantine check on it was a legitimate row-level validation. Now that capture is sparse by design, a null `customer_email` is the expected majority case (86% of rows), not a data-quality failure -- the same distinction `page_url`/`product_id`/`search_query` already draw (event-type-conditional nullability, not tested). `web_quarantine_reason()`'s `missing_customer_id` branch was removed; every other check (missing `event_id`, unparseable timestamps, missing `session_id`/`event_type`) is unchanged. `stg_web__events_quarantine` remains empty (0 rows) against the current dataset -- confirmed directly, not assumed.

### Decision: `seed_match_truth.csv`/`build_match_truth.py` -- extend, don't replace, and add a generation-time sidecar

**Investigation finding:** `build_match_truth.py` built web's ground truth by scanning `CLICKSTREAM_EVENTS.json` for literal `CUST-#####` values -- which worked only because the raw output *was* the exact truth. Once the signal is sparse and tier-2 values are genuinely, irreversibly distorted, scanning the dirtied output can no longer recover which customer a given captured value truly belongs to; that information only exists at the moment of generation, before dirtying.

**Recommendation, and what was built:** extend `seed_match_truth.csv` (same person-grain file, same script) rather than introduce a new event-grain seed. `data_gen/clickstream.py` now writes `data/CLICKSTREAM_IDENTITY_TRUTH.csv`, a per-event sidecar (`event_id, customer_index, canonical_email, captured, dirt_tier, observed_value`) built before dirtying, in the same run, from the same in-memory objects -- not a second, drift-prone re-implementation of the dirtying logic. It's gitignored alongside the other raw extracts in `data/` (not loaded into `RAW_WEB_EVENTS` -- it's generation-time bookkeeping, not source data). `build_match_truth.py` now reads it (`scan_clickstream_truth()`) instead of scanning `CLICKSTREAM_EVENTS.json`, and aggregates it to person grain in `seed_match_truth.csv`: `clickstream_key` (a single exact key, no longer meaningful) is replaced with `clickstream_true_event_count`, `clickstream_captured_event_count`, and `clickstream_captured_variants` (the distinct, possibly-dirty strings a fuzzy matcher will actually see for that person).

A full event-grain ground-truth seed (scored match confidence per event, say) was considered and deliberately not built here. Phase 5A hasn't yet decided what grain or representation its own scoring needs -- event, session, or captured-signal grain -- and locking one in now would be designing an abstraction for a consumer that doesn't exist yet, which this project's own precedent (ADR-006's `crm_us_country_variants`, ADR-007's "no additional custom macro") explicitly avoids. The person-grain summary above is sufficient for Phase 5A to check, per person, whether its fuzzy matcher recovered the right set of captured variants; if Phase 5A's actual scoring needs finer grain, `CLICKSTREAM_IDENTITY_TRUTH.csv` (regenerable from the same seed) or a purpose-built extension of it is the natural next step at that point, not a hypothetical this branch should pre-build.

### Regression check: outcome-level and mechanism-level

Adding RNG draws to `build_event()` (the capture roll, the dirt-tier roll, and the tier-1/tier-2 distortion draws) changes how many `random` calls each event consumes, which shifts the entire downstream draw sequence for the same seed -- every event after the first captured/dirtied one gets different specific field values than the pre-fix run, even though nothing about *those* fields' own logic changed. That shift is expected and was checked for directly, not assumed away:

**Outcome-level** (before → after, both measured directly against real data, not inferred):

| Metric | Before | After | 
| ------- | ------ | ----- |
| Raw rows (`RAW_WEB_EVENTS`) | 1,050 | 1,050 |
| Distinct `event_id` | 1,000 | 1,000 |
| Duplicate rows (pixel-retry) | 50 (5.0%) | 50 (5.0%) |
| Late-arriving events (canonical, 2-4 day lag) | 20 (2.0%) | 20 (2.0%) |
| `stg_web__events` rows | 1,000 | 1,000 |
| `stg_web__events_quarantine` rows | 0 | 0 |
| `int_web_events_incremental` rows | 1,000 (ADR-005) | 1,000, matches `stg_web__events` exactly |
| Date range | 2025-01-05 – 2025-12-31 | 2025-01-05 – 2025-12-31 |
| Schema-drift cutoff split (event count) | 503 pre / 547 post (raw, always-present field) | 491 pre / 509 post (canonical `stg_web__events`, confirmed 0 schema-drift violations) |
| `event_type` distribution (raw, 1,050 rows, incl. duplicates) | page_view 758 / cart_addition 138 / search_query 154 | page_view 738 / cart_addition 161 / search_query 151 |

Duplicate count, late count, and total row count are all formula-driven (`round(event_count * RATE)`, fixed list-position slicing for late events) rather than derived from specific RNG output, so they hold exactly regardless of the sequence shift -- confirmed, not assumed, by rerunning and re-measuring. The cutoff split and `event_type` distribution *are* RNG-content-dependent (`event_date`/`event_type` are themselves draws), so they shift by a small, expected amount within the same weights/date range -- 491/509 is still a full-year, roughly-even split, and 738/161/151 (70.3%/15.3%/14.4%) still tracks the 70/15/15 target weights closely. Neither is a hidden defect; both are the expected footprint of an intentionally different deterministic sequence for the same seed.

**Mechanism-level:** the shift itself was directly confirmed, not just the aggregate outcomes -- diffing the pre-fix and post-fix `event_type` distributions (758/138/154 vs. 738/161/151, both against the full raw 1,050-row extract) shows individual events landing in different type buckets than before, proving the RNG call-count change did resequence per-event content exactly as expected, while every rate/formula-driven invariant above held anyway.

### Match-rate breakdown: proof the fix created a genuine heuristic-matching problem

All four numbers below were confirmed twice -- once against the local `data/CLICKSTREAM_IDENTITY_TRUTH.csv` sidecar, and independently against live `DEV_ANALYTICS` data (`stg_web__events.customer_email` joined to `seed_match_truth.canonical_email`, with no reference to the generator's own tier labels) -- and the two sources agree exactly:

| Metric | Count | Rate |
| ------- | ----- | ---- |
| Identity capture rate (of 1,000 canonical events) | 140 | 14.0% |
| Raw exact-match rate (of 140 captured, byte-for-byte vs. canonical email) | 69 | 49.3% |
| Normalized exact-match rate (of 140 captured, after lowercase+trim) | 112 | 80.0% |
| **Residual requiring genuine heuristic resolution (of 140 captured)** | **28** | **20.0%** |

The critical result is the last row: 28 events, spanning 28 distinct customers, that no amount of case-folding or whitespace-trimming resolves -- an actual fuzzy-matching problem (bounded single-character-edit typos against a 350-person candidate pool) for Phase 5A's identity-resolution model to solve, not zero, and not just a couple of edge cases either. Before this fix, the equivalent residual was 0: the raw exact-match rate was 100% on every one of 1,000 events, because the "identity signal" was a direct copy of the join key. Phase 5A now has a real problem to solve.

### Consequences

- `customer_email`'s content (real-looking email addresses, same as ERP/CRM's existing `customer_email`/`contact_email` columns) is unmasked at the RAW/staging layer, same as ERP/CRM already are -- this is not a new PII-handling gap this fix introduces. `EMAIL_MASK` (ADR-001) is deliberately not yet attached to any column project-wide; it's scoped to attach to `dim_customer_360.contact_email` in Phase 6, once that gold model exists.
- `data_gen/load_raw.py` gained a repeatable `--table` flag so a single source (here, `RAW_WEB_EVENTS`) can be reloaded without touching the other four tables' `CREATE OR REPLACE TABLE` -- useful any time only one generator changes, not just this fix.
- Phase 5A's identity-resolution model was deliberately not touched in this branch -- it now has a genuine, measured, non-trivial matching problem to solve when it starts, instead of an exact key masquerading as one.

## ADR-009: Revenue reconciliation is unbuildable without it -- ERP lifecycle/refunds, web purchase events, CRM refund tickets, and the shared order pool

**Status:** Accepted (2026-09-11).
**Phase:** Phase 5B pre-work (Issue #42), the same category of pre-work ADR-008 was for Phase 5A: this branch fixes the three synthetic-data generators so a genuine, multi-dimension revenue-reconciliation problem exists in the raw data, ahead of Phase 5B's own reconciliation fact and Phase 6's bridge mart, neither of which is built here. No dbt staging model is touched in this branch -- exposing the new raw fields in `stg_erp__orders`/`stg_web__events`/`stg_crm__tickets` is the next branch.

### Context

The build plan's stated business problem is "Finance can't produce a trusted revenue number without a manual reconciliation exercise every close." That problem was unbuildable on the data as it stood: only ERP carried any dollar figure at all, CRM and web had none, and ERP itself had no return/cancellation lifecycle, no recognition timestamp distinct from order creation, and no refund fields. This is the same class of gap ADR-008 found and fixed for web identity (a Phase 1 build gap relative to the stated Phase 5 design) -- just for revenue instead of identity.

### Decision: a shared, deterministic order pool (`data_gen/order_pool.py`), not a file-read dependency

This work requires ERP, web, and CRM to reference the *same* order/transaction identifiers -- something none of the three needed before (they only ever shared *customer* identity, via `identity_pool.py`, never *order-level* identity). Two mechanisms were considered:

1. **One script writes an intermediate file the others read** (e.g. `erp.py` runs first and emits an order manifest; `clickstream.py`/`crm.py` read it). Rejected: this introduces a real execution-order dependency the three generator scripts have never had (today they run independently, in any order) -- and it would make `clickstream.py`/`crm.py` silently wrong if run against a stale manifest from a previous `erp.py` run with a different `--orders-per-region`.
2. **A shared, deterministic pool** (`data_gen/order_pool.py`, exposing `build_order(region, order_number)`), analogous to `identity_pool.py`. Chosen: this matches the project's own existing architecture precedent exactly, and preserves the property that all three scripts remain independently runnable in any order.

`build_order()` is a pure function of `(region, order_number)` plus the fixed `ORDER_POOL_SEED` (20260911) -- not each script's own `--seed`, for the same reason `identity_pool.py` decouples customer identity from `--seed`: three scripts needing to agree on content can't have that content depend on which one happened to run, or with what seed. It returns the order's full deterministic truth in one call: customer, item lines (with any promo discount already applied and quantized), lifecycle status, `ship_date`/`refund_date`/`refund_amount`, the checkout promo outcome, and whether it has a matching web purchase event. `erp.py`, `clickstream.py`, and `crm.py` each call it directly and read only the fields they need; none of them mutates or persists it for another script to read.

**A consequence, accepted deliberately:** `erp.py` no longer accepts a `--seed` argument at all. Every order-level fact that used to be `erp.py`'s own private randomness now lives in the pool, and there is no remaining script-local randomness left to seed -- keeping a dead `--seed` parameter around would be exactly the kind of leftover surface this project's own conventions avoid. `clickstream.py` and `crm.py` keep their own `--seed` (it still controls each script's independent decisions -- which specific duplicates/late events, which specific ghost slot, etc.) but both gained `--orders-per-region`, which **must be passed identically to `erp.py`'s own value** for order references to actually correlate; passing mismatched values silently breaks correlation from an inconsistent universe size, not from any of the deliberate mechanisms below.

`order_id_for(region, order_number)` is a pure string formula (`f"{region}-{order_number:06d}"`) requiring no randomness at all -- any script can compute it without calling `build_order()`. `erp.py` only ever emits rows for `order_number in 1..orders_per_region`; any higher number is, by construction, never a real order. `ghost_order_number(orders_per_region, rng)` picks one of those never-real numbers from a 200-wide per-region range, using the *caller's own* locally-seeded `rng` (not pool-deterministic) -- a web orphan transaction id and a CRM nonexistent order reference are independent phenomena that don't need to correlate with each other, so there's no reason to force them through the same seeded pool the way `build_order()`'s content is. **Found and fixed during implementation:** the range was first sized at 25 (matching `crm.py`'s existing `GHOST_ACCOUNT_POOL_SIZE`), and web's ~45-50 independent orphan draws per default run collided with each other via the birthday paradox -- checked directly, not assumed: 382 purchase events, only 368 distinct `transaction_id` values. Widened to 200; re-verified with 0 collisions (382 purchase events, 382 distinct `transaction_id`).

### Decision: ERP status lifecycle, `ship_date`, `refund_date`/`refund_amount`

Status becomes `pending` → `shipped` → `delivered` → `returned`, with `cancelled` reachable at any point before shipping as a distinct terminal state (never fulfilled at all, not fulfilled-then-reversed). `ship_date` (1-5 days after `order_date`) is the recognition timestamp; it's `null` for `pending`/`cancelled` orders. `refund_date` (5-45 days after `ship_date`) and `refund_amount` (the order's actual recognized total) are set only for `returned` orders.

**Rates are real, cited figures.** NRF/Happy Returns' "2025 Retail Returns Landscape" puts the 2025 online-retail return rate at 19.3% (up from 17.6% in 2024) -- the most-cited current benchmark for general-merchandise online returns and considerably higher than a flat 5-10% guess. Used as a single blended rate rather than a per-category rate: this dataset has no product-category concept, and inventing one solely to size a return rate would be the same premature abstraction ADR-006 explicitly avoided for CRM's country var. Cancellation uses a separate, smaller, independently-documented rate (4%), inside the 2-8% band industry sources report for healthy ecommerce operations (Amazon, held up as the operational benchmark, targets under 2.5%).

Verified directly against the loaded default dataset (1,000 orders, 500/region): **cancelled 39 (3.9%), pending 7 (0.7%), shipped 134 (13.4%), delivered 625 (62.5%), returned 195 (19.5%)** -- both rates land almost exactly on their targets (19.3%/4.0%). `pending` is deliberately small but non-vacuous: only orders placed close enough to `AS_OF_DATE` (2025-12-31) that they wouldn't have shipped yet as of the snapshot land there, giving the recognition policy's "excludes unshipped orders" clause a real, if small, population to exclude.

### Decision: refund_date is allowed to extend past AS_OF_DATE

`docs/synthetic_data_spec.md`'s existing fixed-window discipline (`AS_OF_DATE = 2025-12-31`, no `date.today()` drift) governs `order_date`/`ship_date`, but clamping `refund_date` to the same bound would silently erase the later-period reversal this whole branch exists to create -- a return's refund is the tail of a process that can genuinely land after the snapshot that captured the order that started it. `refund_date` is instead bounded by `order_pool.REFUND_WINDOW_END` (`AS_OF_DATE` + 45 days = 2026-02-14), the maximum possible `RETURN_LAG_DAYS`. This is a deliberate, documented widening of the fixed-window discipline, not a reintroduction of `date.today()` -- both bounds are still fixed constants, verified directly: `min(refund_date) = 2025-01-22`, `max(refund_date) = 2026-01-31`, comfortably inside the declared bound. CRM refund tickets' `created_date` uses the same extended bound (see below).

### Decision: revenue-recognition policy, written before finalizing the timestamp logic

**`recognized_net_revenue` for period P = ERP line-item amounts (`unit_price * quantity + tax_amount`) whose `ship_date` falls in P, minus `refund_amount` for any order whose `refund_date` falls in P. Cancelled orders, and orders that have not yet shipped (`pending`), are excluded entirely.**

Recognition is keyed on `ship_date`, not `order_date`: order-create-based recognition would shrink or eliminate the cutoff problem this dataset exists to create. This was written as this branch's actual contract *before* finalizing `order_pool.build_order()`'s lifecycle logic (per the build plan's own instruction), and the generated data was then verified to actually conform to it, not assumed to: of the 195 returned orders, **146 (74.9%) have `refund_date` falling in a different calendar month than `ship_date`** -- a substantial, real cross-period reversal population, not a handful of edge cases.

### Decision: checkout promo -- a real arithmetic mechanism, not noise

18% of orders (`PROMO_RATE`) have a checkout-time 10% promotional discount applied (`PROMO_DISCOUNT_RATE`). Of those, 30% (`PROMO_VALIDATION_FAILURE_RATE`) fail ERP's backend eligibility validation: the discount is honored at checkout (what `checkout_total` reflects) but ERP charges full price (what the stored line items -- and therefore `refund_amount`, if later returned -- actually reflect). The remaining 70% validate, and the discount is baked into the stored line items themselves (each line's `unit_price` scaled by `1 - PROMO_DISCOUNT_RATE` before tax is computed on it), so ERP's recognized amount and web's checkout total genuinely agree, down to per-line rounding.

This was deliberately built as real, computable arithmetic rather than sampled noise, and verified as such: against the full 1,000-order pool, 165 orders (16.5%) have a promo applied, of which 45 (27.3% of promo'd orders) fail validation -- and **exactly 45 of 1,000 orders (4.5%) show a "real" (>\$1) divergence between `checkout_total` and the recognized total**, matching the validation-failure count precisely. A small residual of sub-\$0.02 differences exists among *validated* promo orders too (per-line quantization: each line's discounted `unit_price` is independently rounded to the cent, so the sum of quantized lines can differ from an aggregate-computed total by a cent or two) -- this is legitimate, expected rounding messiness, not a second mechanism, and is why "amounts agree" below uses a \$0.02 tolerance rather than exact equality.

### Decision: web purchase events, and why the default event count grew ~10x

`purchase` is a new, deliberately rare event type. Its volume is *emergent* from real ERP order coverage, not an independently dialed rate: `WEB_MATCH_RATE` (35% of real orders get a matching purchase event) against 1,000 default orders is ~350 matched events, plus a 12% orphan share brings the total to ~400. To keep that ~400 a "low single digits" share of the *combined* event total (not anywhere near an even split with `page_view`/`cart_addition`/`search_query`, per the build plan), `DEFAULT_EVENT_COUNT` was raised from 1,000 to 9,600 -- landing purchase at ~4.0% of the combined ~10,000, close to Contentsquare's cited 2.5-3% global ecommerce conversion rate for Q3 2025 (this generator approximates session-level conversion at event level, so an exact match isn't the goal). This ~10x default bump was a direct, necessary consequence of needing a realistic purchase-event share *and* realistic order coverage simultaneously at this dataset's existing order volume (1,000 orders) -- not a scale increase pursued for its own sake.

`checkout_total`/`transaction_id`/`currency` are the new fields; the purchase timestamp uses the order's own `order_date` (checkout time), never `ship_date` -- the timing gap between checkout and ERP's ship-based recognition, not amount drift, is the actual cutoff signal. Identity capture on purchase events uses the same tiered exact/normalization/typo mechanism as the other three event types (extracted into a shared `roll_identity_capture()` helper rather than duplicated), at a distinctly higher capture rate (90%, vs. 55% for `cart_addition`) -- a completed checkout collects a contact email for the receipt almost every time. Purchase events are **not** subject to the pixel-retry-duplicate or late-arrival mechanics -- both remain scoped, unchanged, to the original three event types; extending them to purchase events wasn't asked for and would have added complexity with no corresponding need.

### Decision: order-coverage gap rates (both directions)

- **35% of real ERP orders have a matching purchase event** (`WEB_MATCH_RATE`); the other 65% don't. Deliberately the *majority* of orders, not a small tail: a real general-merchandise omnichannel retailer's phone/in-store/ad-blocked-or-declined-pixel share of order volume is realistically substantial, and modeling web coverage as near-universal would understate how much revenue a real analyst can never see in web analytics at the order-id grain.
- **12% of purchase events are orphans** (`WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS`) -- a transaction_id with no real ERP order, representing a checkout that failed payment before the order ever persisted in ERP.

Verified directly against the loaded default dataset: of 1,000 real ERP orders, **336 (33.6%) have a matching web purchase event, 664 (66.4%) don't**. Of 382 total purchase events, **336 (88.0%) resolve to a real ERP order, 46 (12.0%) are orphans** -- the orphan share lands almost exactly on its 12% target.

### Verification: the full breakdown (Step 8 of the build plan)

All numbers below are direct queries against the loaded `DEV_ANALYTICS.RAW` tables (or, where noted, the deterministic generator itself), not inferred from the generator's logic alone.

**Order coverage** (both directions, real counts): 336 of 1,000 ERP orders have a matching web purchase; 664 don't. 336 of 382 web purchases have a matching ERP order; 46 don't.

**Timing** (of the 336 matched order/purchase pairs): **28 (8.3%) have their checkout event and ERP recognition (`ship_date`) falling in different calendar months** -- the actual cutoff population this branch exists to create. A further 19 (5.7%) of matched pairs have no `ship_date` at all yet (the ERP order is still `pending` as of the snapshot, even though a web checkout already happened) -- a second, related timing gap worth noting: revenue that's already visible in web but not yet recognizable in ERP at all.

**Amount** (of the 336 matched pairs, using a \$0.02 tolerance to separate genuine mechanism-driven divergence from per-line rounding dust -- see "Checkout promo" above): 313 (93.2%) agree, 23 (6.8%) diverge, of which 16 (4.8% of matched pairs) are a "real" (>\$1) divergence. Sampled directly, confirming these are promo-validation-failure cases, not noise:

| transaction_id | checkout_total | recognized (ERP) total | order status |
| -------------- | --------------- | ------------------------ | -------------- |
| US-000160 | \$234.20 | \$260.21 | shipped |
| EU-000245 | \$263.06 | \$292.29 | delivered |
| US-000029 | \$373.17 | \$414.63 | delivered |
| US-000183 | \$738.87 | \$820.97 | delivered |
| US-000330 | \$1,139.00 | \$1,265.55 | shipped |

Each recognized (ERP) total is checkout_total / 0.9, exactly the arithmetic a failed 10%-off promo produces (e.g. \$234.20 / 0.9 = \$260.22, matching \$260.21 to the cent after per-line tax rounding) -- confirming the mechanism, not sampled noise.

**CRM** (224 refund tickets against the loaded data): a raw anti-join+status check finds 214 (95.5%) whose `order_reference` resolves to a real, *returned* order, 4 (1.8%) resolve to a real order that *isn't* returned, and 6 (2.7%) don't resolve at all. Replaying the generator with the reference-tail decision instrumented (its true intent isn't persisted in the CSV output, by design) gives the actual mechanism breakdown: **207 correct (92.4%), 11 wrong-but-valid (4.9%), 6 nonexistent (2.7%)** -- close to the 90/6/4 targets (small-sample variance at n=224). The gap between the two counts (11 true wrong-but-valid vs. only 4 detectable as such) is itself the point: **7 of the 11 wrong-but-valid tickets happen to reference a *different* order that is also `returned`**, making them indistinguishable from a correct reference by a simple anti-join+status check alone -- exactly the "confident wrong match, not an obvious miss" danger the build plan called for.

Open/posted split: 64 (28.6%) open/pending, 160 (71.4%) resolved/closed -- close to the 35/65 target (a ~2-standard-deviation draw at this sample size, not a bug). Of the 160 posted tickets, 156 have an `order_reference` resolving to a real order; of those, 153 reference an order that's actually `returned` (and therefore has a comparable `refund_amount` -- the other 3 reference a real-but-non-returned order, another wrong-but-valid case surfacing here). Of the 153 comparable pairs: **136 (88.9%) match, 17 (11.1%) diverge** -- close to the 10% `REFUND_POSTED_MISMATCH_RATE` target, each by a flat \$5.99-\$12.99 shipping/tax-like adjustment (verified directly, e.g. `TCKT-000984` vs. `US-000418`: claimed \$2,103.23 against ERP's \$2,097.24, a \$5.99 difference).

### Regression check: outcome-level and mechanism-level (ADR-008's discipline, applied again)

Adding new RNG draws earlier in each generator's call sequence resequences everything after them for the same seed -- expected, and checked directly rather than assumed away, exactly as ADR-008 did for web identity.

**ERP** (unaffected in kind, shifted in specifics): row counts 1,237 US / 1,236 EU order-item rows (previously 1,254/1,273 under ADR-002 -- a small shift from the same item-count/quantity RNG now being sourced from the order pool instead of `erp.py`'s own `random.Random(seed)`, not a change in the `rng.randint(1, 4)` item-count logic itself). EU currency split: 676 EUR / 560 GBP (previously 650/623) -- still both currencies present, still no unsupported-currency rows, `erp_convert_to_usd()`'s existing rate coverage is unaffected.

**Web** (ADR-008's fix must survive untouched):

| Metric | Before this branch | After |
| ------- | ------- | ----- |
| Duplicate rate (of canonical events) | 5.0% | 5.0% (480/9,600, exact) |
| Late-arrival rate (of canonical events) | 2.0% | 2.0% (192/9,600 flagged; 206 total rows show the 2-4 day gap once ~14 duplicate copies of late events are counted too -- expected: duplicates are exact copies, including of late events, and ~9.6 such overlaps were expected by chance at this rate) |
| Schema-drift cutoff split (all events, by `event_date`) | ~49%/51% | 5,107 (48.8%) pre / 5,355 (51.2%) post -- still a full-year, roughly-even split |
| Identity capture rate, `page_view` | ~8% | 547/6,766 = 8.1% |
| Identity capture rate, `search_query` | ~8% | 117/1,432 = 8.2% |
| Identity capture rate, `cart_addition` | ~55% | 750/1,402 = 53.5% |

All three original per-event-type capture rates land within normal sampling variance of their unchanged targets (`IDENTITY_CAPTURE_RATE_BY_EVENT_TYPE`'s existing values were not touched -- only a fourth key, `purchase`, was added to the same dict) -- confirming ADR-008's fix is intact. `purchase`'s own rate (345/382 = 90.3%) matches its new 90% target.

**CRM** (unaffected in kind): ghost-account rate on general (non-refund) tickets, 36/900 (4.0%) -- exactly matching ADR-006's established rate, confirming the pre-existing mechanism (unmodified in this branch) still behaves identically. Country standardization unaffected: US variants still split across `US`/`USA`/`United States`/`u.s.a.` (36/37/33/28), five EU codes unchanged in kind (`DE` 58, `FR` 41, `NL` 35, `ES` 40, `IT` 42).

### Consequences

- The stated business problem ("Finance can't produce a trusted revenue number without a manual reconciliation exercise every close") is now buildable: ERP, web, and CRM each carry a genuine dollar figure, correlated via the shared order pool, with real, documented, non-uniform coverage gaps and at least one real (non-noise) amount-divergence mechanism -- not three sources that happen to agree perfectly, and not one source with the only numbers.
- `erp.py` no longer takes `--seed`; `clickstream.py`/`crm.py` both gained `--orders-per-region`, which must match `erp.py`'s own value for correlation to hold. `docs/synthetic_data_spec.md` documents this requirement at every relevant call site.
- Phase 5B's reconciliation fact (the full-outer-key-union detail table) and Phase 6's bridge mart were deliberately not built here -- this branch's job was correct, realistic, well-correlated raw source data for those to consume. No dbt staging model (`stg_erp__orders`/`stg_web__events`/`stg_crm__tickets`) was touched; exposing the new raw fields there is the next branch.
- `dbt/seeds/seed_match_truth.csv` was regenerated (via `data_gen/build_match_truth.py`, itself unmodified) against the new `US_ORDERS.csv`/`EU_ORDERS.csv`/`CRM_CUSTOMERS.csv`/`CLICKSTREAM_IDENTITY_TRUTH.csv` -- its content shifted (the same RNG-resequencing effect as everywhere else in this branch) but its own logic and shape are unaffected.

## ADR-010: Exposing ADR-009's revenue fields through staging, and fixing the accepted_values tests ADR-009 left stale

**Status:** Accepted (2026-09-11).
**Phase:** Phase 5B pre-work continuation (Issue #42's own "next branch" note) -- ADR-009 added ERP lifecycle/refund fields, a web `purchase` event type, and CRM refund tickets to the raw generators but deliberately stopped short of staging. This branch is that next branch. Phase 5B's reconciliation fact and Phase 6's bridge mart are still not built here.

### Context

ADR-009 was explicit that it touched no dbt staging model. That left the repo in a state a peer review caught before any of this branch's own work started: `stg_erp__orders`/`stg_erp__order_items`'s `order_status` accepted_values test still listed only `["shipped", "delivered"]` (pre-ADR-009's two-state world), `stg_web__events`/`int_web_events_incremental`'s `event_type` test still listed only the original three event types, and `stg_crm__tickets`'s `category` test still listed only the original five categories -- none of them widened for ADR-009's `pending`/`cancelled`/`returned` statuses, `purchase` event type, or `refund` category. A fresh regenerate+load+test cycle therefore failed today, on `main`, before this branch changed anything.

### Decision: reproduce the failure for real before fixing it

Rather than assume the review's claim, the exact failure was reproduced first: `data_gen/erp.py`/`clickstream.py`/`crm.py` regenerated with a matching `--orders-per-region 500`, loaded fresh via `data_gen/load_raw.py`, then `dbt build --select staging.*` run against the untouched schema definitions.

**Real, confirmed pre-fix failures** (dbt's `accepted_values` test groups by distinct value, so `dbt`'s own "Got N results" line reports distinct disallowed values, not row counts -- the row counts below are direct queries against the actually-failing data, not inferred from that line):

| Test | Disallowed distinct values | Rows affected | Total rows |
| ---- | ---------------------------- | --------------- | ------------ |
| `accepted_values_stg_erp__orders_order_status` | 3 (`pending`, `cancelled`, `returned`) | 241 (39 cancelled + 7 pending + 195 returned) | 1,000 orders |
| `accepted_values_stg_erp__order_items_order_status` | 3 (same) | 605 (96 + 16 + 493) | 2,473 item rows |
| `accepted_values_stg_web__events_event_type` | 1 (`purchase`) | 382 | 9,982 events |
| `accepted_values_stg_crm__tickets_category` | 1 (`refund`) | 224 | 1,124 tickets |

All four widened to the full current value sets (`pending`/`cancelled`/`shipped`/`delivered`/`returned`; the four event types including `purchase`; the six categories including `refund`) -- `int_web_events_incremental`'s `event_type` test (a pass-through of `stg_web__events`) was fixed identically, since it carries the exact same stale list. Re-running the identical build: 56/56 staging-layer tests pass, 0 errors. This fix was committed and verified on its own, before any of the field-exposure work below, so it's independently reviewable and bisectable.

### Decision: `ship_date`/`refund_date`/`refund_amount` are order-grain, not exposed on `stg_erp__order_items`

Checked directly against `data_gen/order_pool.py` and `data_gen/erp.py` before writing any SQL, per this project's own established discipline (ADR-002's GBP rate, ADR-004's TRY_CAST): `order_pool.build_order()` sets `ship_date`/`refund_date`/`refund_amount` exactly once per order, and `erp.py`'s `build_order_rows()` stamps that same value onto every line-item row of the order. `refund_amount` specifically is `recognized_total` -- the order's full recognized amount, not a per-line figure. These are order-level facts that happen to be physically repeated across item-grain rows in the raw table, not line-item facts.

Exposing them only on `stg_erp__orders` (via a corresponding `erp_orders_unioned()` change) rather than also on `stg_erp__order_items` follows the same grain discipline ADR-003 already established for `order_status`/`customer_id` -- order-level facts belong on the order-grain model, not duplicated onto every item row of the order for a consumer to accidentally sum N times. `stg_erp__order_items` is unchanged: none of ADR-009's three new ERP fields are item-level concepts.

`refund_amount` is exposed in the order's native (unconverted) currency -- `stg_erp__orders` has never done currency conversion (ADR-003) and doesn't expose a `currency` column at all, so a USD-normalized refund figure requires joining `stg_erp__order_items` for currency context. This is a known, documented limitation of this branch, not an oversight; building that join is downstream work (Phase 5B's reconciliation fact), not staging's job.

### Decision: `stg_web__events`'s new purchase-only fields follow ADR-004's TRY_CAST precedent

`transaction_id`/`checkout_total`/`currency` are absent on every non-purchase event, the same event-type-conditional nullability already established for `page_url`/`product_id`/`quantity`/`search_query`. `checkout_total` is a numeric value written as a JSON string by `data_gen/clickstream.py` (`f"{order['checkout_total']:.2f}"`), so it gets the same `TRY_CAST(... as number(12,2))` treatment `web_events_parsed()` already uses for `quantity` -- not a plain cast, per ADR-004's finding that a plain `::type` cast on a VARIANT raises a hard error rather than yielding `NULL`. `transaction_id`/`currency` stay on a plain `::varchar` cast, matching `event_id`/`session_id`/`event_type` -- both are always string-shaped when present, with no failure mode for `TRY_CAST` to guard against.

### Decision: two new singular tests encode ADR-009's actual cross-column invariants

Column-level `not_null`/`accepted_values` tests can't express "these two columns must agree" -- the same reasoning ADR-003's `assert_erp_orders_status_snapshot_single_open_version` and ADR-006's `assert_stg_crm__tickets_ghost_flag_matches_antijoin` already established for this project. Two new tests follow that precedent:

- `assert_stg_erp__orders_refund_fields_match_status`: `refund_date`/`refund_amount` are populated if and only if `order_status = 'returned'`.
- `assert_stg_web__events_purchase_has_transaction_id`: every `event_type = 'purchase'` row has a non-null `transaction_id`.

Ghost order references (CRM `order_reference`) and orphan `transaction_id`s were checked directly rather than given a redundant new test: no staging-layer anti-join test for either exists yet (there's nothing to anti-join against at this layer until Phase 5B's reconciliation fact exists), so this branch confirmed the documented rates hold by direct query instead of asserting them as a schema invariant that would need to be loosened every time the generator's seed shifts the exact count -- the same reasoning ADR-006 already gave for not asserting `is_ghost_account`'s count at a fixed value.

### Verification: newly-exposed columns reproduce ADR-009's own documented figures exactly

Queried directly against the same freshly regenerated/loaded/built dataset used for the accepted_values reproduction above:

- `stg_erp__orders` lifecycle: all 195 `returned` orders have `ship_date`/`refund_date`/`refund_amount` populated; all 805 non-returned orders have `refund_date`/`refund_amount` null (and `pending`/`cancelled` orders also have `ship_date` null) -- exactly the invariant `assert_stg_erp__orders_refund_fields_match_status` encodes, confirmed before that test was even the thing being checked.
- The five promo-validation-failure orders ADR-009's own worked table cites reproduce their exact checkout/recognized-total figures once `stg_web__events.checkout_total` and `stg_erp__orders` are joined on `transaction_id = order_id`: `US-000160` \$234.20, `EU-000245` \$263.06, `US-000029` \$373.17, `US-000183` \$738.87, `US-000330` \$1,139.00 -- all five match ADR-009's table to the cent.
- CRM `order_reference` resolution against `stg_erp__orders`: 214 resolve to a real, returned order; 4 resolve to a real, non-returned order; 6 resolve to nothing -- matching ADR-009's own "214 (95.5%)/4 (1.8%)/6 (2.7%)" anti-join+status breakdown exactly.
- Web purchase-event coverage: 382 total purchase events, 336 resolve to a real ERP order, 46 are orphans -- matching ADR-009's "336 (88.0%)/46 (12.0%)" exactly.

This isn't a coincidence -- `data_gen/order_pool.build_order()` is a deterministic pure function of `(region, order_number)` plus the fixed `ORDER_POOL_SEED`, so any fresh regenerate at the same `--orders-per-region` reproduces the same content. It does confirm, empirically rather than just architecturally, that this branch's staging exposure didn't introduce any transcription error against the raw fields.

### Verification: full build, lint, and generator contract tests

- `sqlfluff lint dbt/models dbt/macros dbt/snapshots dbt/tests`: clean.
- `dbt parse` / `dbt run` / `dbt test` (dev target, full project, not just staging): `dbt run` 8/8 models including the snapshot and `int_web_events_incremental`'s microbatch; `dbt test` 64/64 data tests, 0 errors.
- `scripts/bootstrap.sh` (new -- a single script for the gen→load→`dbt build` sequence `docs/loading_notes.md` previously only documented as separate manual steps) run end-to-end from a clean state: 74/74 (`dbt build`'s combined model+test count) `PASS`, 0 errors.
- `tests/test_generators.py` (new -- plain `unittest`, no new dependency): 6/6 pass, covering the `--orders-per-region` mismatch's actual (non-exception) failure mode, `ghost_order_number()`'s structural non-collision-with-real guarantee, and a closed-form regression bound on ADR-009's own "found and fixed" birthday-paradox pool-sizing issue.

### Consequences

- The accepted_values fix and the field-exposure work are independently reviewable commits (accepted_values fix first, verified passing before any other change was made) -- if the field-exposure work needs to be reverted or reworked, the test fix stands on its own.
- `stg_erp__order_items` still has no revenue-lifecycle fields at all, by design -- any future consumer needing per-line refund allocation (e.g. a partial return) would need a new mechanism entirely, not a column added here; the generator itself only models whole-order returns today.
- A USD-normalized `refund_amount` still requires joining `stg_erp__order_items` for currency -- `stg_erp__orders` remains currency-conversion-free, consistent with ADR-003. Building that join is Phase 5B's reconciliation fact, not this branch.
- `scripts/bootstrap.sh` and `tests/test_generators.py` are net-new project infrastructure (a runnable end-to-end setup script, and this repo's first Python test suite) that outlive this specific branch's field-exposure purpose.
