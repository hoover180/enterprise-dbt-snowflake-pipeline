# Data Modeling Decisions

An append-only log of architecture decision records (ADRs) for this project. Per the PR template, any PR that changes an architectural decision links back to an entry here.

## ADR-001: EMAIL_MASK is role-based, not a blanket restriction

**Status:** Accepted (2026-09-08)
**Phase:** Defined in Phase 2 (this PR — Terraform PII tagging/masking infra); attached to a gold-model column in Phase 6.

### Context

`terraform/masking_policies.tf` defines `EMAIL_MASK`, a Snowflake masking policy, and `terraform/tags.tf` defines the paired `PII` tag. Neither Phase 2 (infra) nor any phase before it produces a table with an email column — the future consumer is `dim_customer_360.contact_email` (naming per the ERP/CRM `contact_email`/`customer_email` fields documented in `docs/synthetic_data_spec.md`), which doesn't exist until the Phase 6 gold models are built. The policy and tag are written now so the pattern is established and reviewable before there's a real column to attach it to, rather than being designed under time pressure once Phase 6 lands.

The two options considered for the policy body:

1. **Blanket restriction** — mask the column for every querying role, with no exception.
2. **Role-based** — unmask for the role that legitimately needs the real value, mask for everyone else.

### Decision

`EMAIL_MASK` is role-based: `CURRENT_ROLE()` in `('TRANSFORMER_ROLE')` returns the real value; every other role, including `READ_ONLY_ANALYST`, gets a fixed placeholder (`'***MASKED***'`).

A blanket restriction was rejected because `TRANSFORMER_ROLE` (see `terraform/roles.tf`) is the identity dbt itself runs as. Identity resolution — matching CRM `contact_email` against ERP `customer_email`/`contact_email` via the shared identity pool (`docs/synthetic_data_spec.md`, "Why ERP↔CRM join is deterministic") — is a join *on* the real email value. If dbt's own role saw a masked placeholder, every row would collapse to the same masked string and the join would match everything to everything. A masking policy has no notion of "the pipeline that's currently building the table" separate from "a role querying it" — the warehouse role is the only trust boundary Snowflake gives us, so the exception has to be expressed as a role, not as a build-time/query-time distinction.

### Consequences

- dbt transform runs (as `TRANSFORMER_ROLE`) operate on real email values end-to-end, so identity resolution keeps working once it's built in Phase 5.
- `READ_ONLY_ANALYST` — and any future role — never sees a real email address by default; a new role only gains visibility by being added to the `CURRENT_ROLE()` allow-list in the policy body, which is a reviewable one-line Terraform diff, not a per-table grant that's easy to miss.
- The trust boundary is enforced at the role Snowflake already authenticates the session against, not by trusting the application/BI layer to filter columns correctly.
- If a future role needs the real value (e.g. a support/ops role handling GDPR data-subject requests), it's added to the same allow-list rather than the policy being redesigned.

### Note on attachment timing

`EMAIL_MASK` and the `PII` tag are defined in this PR but **not attached to any table or column** — none exists yet. They will be attached in Phase 6, once a gold model materializes the email column, via:

```sql
ALTER TABLE PROD_ANALYTICS.<schema>.DIM_CUSTOMER_360
  MODIFY COLUMN CONTACT_EMAIL
  SET MASKING POLICY PROD_ANALYTICS.PUBLIC.EMAIL_MASK;

ALTER TABLE PROD_ANALYTICS.<schema>.DIM_CUSTOMER_360
  MODIFY COLUMN CONTACT_EMAIL
  SET TAG PROD_ANALYTICS.PUBLIC.PII = 'EMAIL';
```

That `ALTER TABLE` is expected to be added to the Phase 6 dbt model (a post-hook or a `snowflake__copy_grants`-style migration) rather than run by hand, so the attachment survives a full `dbt build` from a clean database.
