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
