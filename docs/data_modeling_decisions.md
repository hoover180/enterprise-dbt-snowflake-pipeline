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

**Status:** Accepted (2026-09-10); amended 2026-09-10 (see ADR-005) to cast `event_timestamp`/`ingested_at` to `TIMESTAMP_TZ` instead of `TIMESTAMP_NTZ` -- the original `TIMESTAMP_NTZ` choice silently dropped the source's UTC marker and caused incorrect results once a UTC-boundary-comparing consumer (ADR-005's microbatch model) was built on top of this model.
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

Two options were considered: expose both `user_id` and `customer_global_id` as separate nullable columns tagged by schema version, or coalesce them into one canonical `customer_id` column.

Coalesce was chosen. The rename is a one-time, permanent cutover in the source system, not two coexisting business concepts that downstream models need to reason about differently — both keys draw from the exact same synthetic `CUST-#####` population (confirmed: `stg_web__events.customer_id` is non-null for all 482 pre-cutoff and all 518 post-cutoff rows, with values spanning the same `CUST-00001`–`CUST-00350` range on both sides), and the two columns are mutually exclusive by construction (never both present on one row). A dual-column design would push the schema-version bookkeeping onto every downstream consumer — including Phase 5's cross-shard identity resolution — for a distinction that carries no actual business meaning once resolved. This mirrors ADR-002's ERP currency conversion: normalize divergent native representations into one canonical column at the staging boundary, rather than propagating the source system's internal versioning downstream. If a future schema drift changed the *meaning* of the identifier (not just its name), a dual-column/schema-versioned approach would be the right call — that's not the case here.

### Decision: dedup via `QUALIFY ROW_NUMBER()`, not `GROUP BY`

`stg_web__events` removes web-pixel-retry duplicates with:

```sql
qualify row_number() over (partition by event_id order by ingested_at asc) = 1
```

`GROUP BY event_id` was rejected even though it would produce the same row count here, because it doesn't generalize safely. `GROUP BY` forces every one of the model's ~10 other selected columns to either appear in the `GROUP BY` list or be wrapped in an aggregate — with the current data (duplicates are byte-identical copies) an aggregate like `MAX()` on each column happens to be a no-op, but that's an accident of today's generator, not a property the model enforces. If a future duplicate ever arrived as a genuine retry with a *different* `ingested_at` (the realistic pixel-retry scenario `docs/synthetic_data_spec.md` describes — a delayed re-send, not a byte-for-byte copy), a `GROUP BY`/`MAX()` approach would silently blend fields from different physical rows with no way to express "keep this whole row, not a field-by-field merge." `QUALIFY ROW_NUMBER()` instead picks one whole row deterministically per `event_id` via an explicit, auditable tie-break (`ingested_at asc` — the earliest-arriving copy is treated as canonical, consistent with a real retry scenario where the first pixel fire is the original event), and never merges fields across rows.

### Decision: quarantine routing, verified with an injected failure case

`web_quarantine_reason()` (`dbt/macros/web_quarantine_reason.sql`) checks for a missing `event_id`, a missing-or-unparseable `event_timestamp`/`event_date`/`ingested_at` (post-`TRY_CAST`, so this catches both "field absent" and "field present but malformed"), and a missing `session_id`/`event_type`/`customer_id`. Applied identically to `stg_web__events` (keeps only passing rows) and `stg_web__events_quarantine` (keeps only failing rows), exactly mirroring `erp_quarantine_reason()`'s split.

The routing was not just inferred from the `TRY_CAST`-on-a-literal finding above — it was proven end-to-end against a real row. One event was inserted directly into `DEV_ANALYTICS.RAW.RAW_WEB_EVENTS` with a fixed, obviously-synthetic `event_id` (`TEST-ARTIFACT-QUARANTINE-VERIFICATION-0001`) and a deliberately unparseable `event_timestamp` (`"NOT-A-VALID-TIMESTAMP"`, exercising the exact cast-failure mode confirmed above), otherwise matching a real event's shape. Rerunning both staging models: the row appeared in `stg_web__events_quarantine` with `quarantine_reason = 'missing_or_unparseable_event_timestamp'`, `event_timestamp` correctly `NULL` (the model build did not crash), and every other field parsed normally (`event_date`, `ingested_at`, `session_id`, `event_type`, `customer_id`, `page_url` all present and correct); the same `event_id` returned 0 rows from `stg_web__events`, confirming the malformed row did not leak into the clean model. The row was then deleted from `RAW_WEB_EVENTS` and both models rerun again; `RAW_WEB_EVENTS`/`stg_web__events`/`stg_web__events_quarantine` counts returned to exactly 1,050/1,000/0, with zero residual rows matching the test `event_id` anywhere — confirming the injection left no trace.

Against the real synthetic dataset (no injected row), this still checks for something that doesn't exist: `stg_web__events_quarantine` holds 0 rows in steady state, because `docs/synthetic_data_spec.md`'s clickstream generator produces no genuinely malformed events (confirmed directly, not assumed — see "Context" above). That is the same situation ADR-002 hit with ERP's quarantine before the GBP rate was added: an empty quarantine table is the intended signal that the mechanism works and the current data has nothing to catch, not evidence the checks are unreachable — and unlike ADR-002's case, that claim is now backed by an actual injected-and-removed test row, not only by reasoning about cast behavior in isolation. Unlike ERP's quarantine, `stg_web__events_quarantine` also carries `raw_event_data` (the unparsed VARIANT), not just the parsed columns — for a `TRY_CAST` failure the parsed column is itself `NULL`, so without the raw payload a quarantined row would show only *that* something failed, not *what* the unparseable source value actually was.

Type-specific fields (`page_url`, `product_id`, `quantity`, `search_query`) are deliberately excluded from `web_quarantine_reason()` — each is legitimately null for two of the three `event_type`s (confirmed directly: `search_query` rows carry a null `page_url`/`product_id`), so a blanket `not_null` check on any of them would misclassify normal rows as failures.

### Consequences

- `stg_web__events` holds 1,000 rows (1,050 raw rows − 50 pixel-retry duplicates), matching the 1,000 distinct `event_id` values confirmed directly against the raw table before any model was built. `stg_web__events_quarantine` is empty (0 rows) in steady state, and that routing behavior has been exercised end-to-end with a real injected-and-removed failing row (see "Decision: quarantine routing" above), not just reasoned about from cast semantics.
- `event_id` carries `unique`/`not_null` tests on `stg_web__events` and passes against the deduplicated data; this is the model's actual grain guarantee, not a row-count assumption.
- `web_events_parsed()`'s `TRY_CAST` usage is the reusable pattern for any future VARIANT-sourced staging model in this project — a plain `::type` cast is only safe on a VARIANT field once you're certain (checked, not assumed) that the source never produces a value that fails to convert.
- `web_quarantine_reason()` is called from both staging models as `{{ web_quarantine_reason() | trim | indent(8) }}` at its multi-line (`case...end`) call site, so the compiled SQL is actually indented to match its surroundings — verified directly against `target/compiled/`, not assumed from the source. `indent()` alone was insufficient: the macro's own leading/trailing newlines (from `{% macro %}`/`{% endmacro %}` each sitting on their own line) made Jinja's filter treat an empty string as "line 1" and therefore skip indenting it, which left `when`/`end` one indent level short of the `case` line's own indent. `trim` first removes those newlines so `indent()`'s width lines up with the call site's actual 8-space indent. This project has a known, confirmed gap where `erp_quarantine_reason()`/`erp_convert_to_usd()` are called with no `indent()` at all and compile with `case`/`end` sitting at column 0 inside an indented `select` — left as-is there (Issue #16/#17 are closed), but not repeated in this PR's new macro.

## ADR-005: Web clickstream incremental model — microbatch strategy, 3-day lookback, backfill

**Status:** Accepted (2026-09-10).
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
