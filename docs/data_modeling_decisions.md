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

**Status:** Accepted (2026-09-10).
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

Since the whole point of routing "VARIANT parsing failures" to quarantine is to keep one bad event from taking down the build, a plain `::type` cast on `event_timestamp`/`event_date`/`ingested_at`/`quantity` would have defeated the mechanism the first time a malformed event appeared — the model would fail to compile at all rather than quarantine the one bad row. `web_events_parsed()` uses `try_cast(event_data:field::varchar as <type>)` for these four fields instead, which yields `NULL` on an unparseable value (confirmed with the same literal: `try_cast(parse_json('{"x":"not-a-date"}'):x::varchar as date)` returns `NULL`, no error). `event_id`/`session_id`/`event_type`/`customer_id` stay on a plain `::varchar` cast — a VARIANT always has a string representation, so there's no failure mode there for `TRY_CAST` to guard against.

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
