# EMAIL_MASK: role-based masking policy for email columns. TRANSFORMER_ROLE
# (the dbt execution role -- see roles.tf) sees the real value, since dbt
# runs, tests, and identity-resolution transforms need to operate on real
# email addresses. Every other role, including READ_ONLY_ANALYST, sees a
# fixed placeholder. See docs/data_modeling_decisions.md for the ADR on why
# this is role-based rather than a blanket restriction.
#
# No PII-bearing table exists yet -- gold models (e.g. dim_customer_360)
# land in Phase 6 (see docs/synthetic_data_spec.md and the build plan), so
# this resource only defines the policy. It is deliberately NOT attached to
# any table/column here. Once dim_customer_360 (or equivalent) exists with
# its contact_email/customer_email column (the CRM/ERP fields documented in
# docs/synthetic_data_spec.md), attach it and tag the column with:
#
#   ALTER TABLE PROD_ANALYTICS.<schema>.DIM_CUSTOMER_360
#     MODIFY COLUMN CONTACT_EMAIL
#     SET MASKING POLICY PROD_ANALYTICS.PUBLIC.EMAIL_MASK;
#
#   ALTER TABLE PROD_ANALYTICS.<schema>.DIM_CUSTOMER_360
#     MODIFY COLUMN CONTACT_EMAIL
#     SET TAG PROD_ANALYTICS.PUBLIC.PII = 'EMAIL';
resource "snowflake_masking_policy" "email_mask" {
  name     = "EMAIL_MASK"
  database = snowflake_database.prod_analytics.name
  schema   = "PUBLIC"

  argument {
    name = "VAL"
    type = "VARCHAR"
  }
  return_data_type = "VARCHAR(16777216)"

  body = <<-SQL
    case
      when current_role() in ('${snowflake_account_role.transformer.name}') then val
      else '***MASKED***'
    end
  SQL

  comment = "Role-based mask for email columns: TRANSFORMER_ROLE sees the real value, every other role (including READ_ONLY_ANALYST) sees a fixed placeholder. Not yet attached to a column -- see the ALTER TABLE ... SET MASKING POLICY statement above, to run once dim_customer_360.contact_email exists (Phase 6)."
}
