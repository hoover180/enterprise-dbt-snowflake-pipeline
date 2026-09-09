# PII classification tag. Snowflake tags are database/schema-scoped objects,
# but (unlike masking policies) they are routinely referenced across
# databases: any role with USAGE on the tag's database/schema can apply it
# to a column in a *different* database by its fully-qualified name. This
# tag is defined once in GOVERNANCE.SECURITY -- a database dedicated to
# shared governance vocabulary, not owned by any one of DEV/STAGE/PROD (see
# docs/data_modeling_decisions.md ADR-001) -- and is the shared definition
# DEV/STAGE/PROD columns will reference too -- there is deliberately no
# per-environment for_each here (contrast with roles.tf's
# transformer_databases/analyst_databases maps), because the classification
# vocabulary (which columns are EMAIL/PHONE/NAME) must stay identical
# everywhere, not be redefined three times.
#
# No PII-bearing column exists yet -- gold models land in Phase 6 (see
# docs/synthetic_data_spec.md and the build plan). Once one does, tag it
# with e.g.:
#
#   ALTER TABLE DEV_ANALYTICS.<schema>.DIM_CUSTOMER_360
#     MODIFY COLUMN CONTACT_EMAIL
#     SET TAG GOVERNANCE.SECURITY.PII = 'EMAIL';
#
# Attaching a tag requires the APPLY TAG privilege on top of the USAGE grant
# in roles.tf -- that grant is deliberately deferred to Phase 6, when there
# is a real column to attach to (see docs/data_modeling_decisions.md
# ADR-001, "Note on attachment timing").
#
# See masking_policies.tf for the first policy this tag's EMAIL value is
# paired with, and docs/data_modeling_decisions.md for the ADR.
resource "snowflake_tag" "pii" {
  name     = "PII"
  database = snowflake_database.governance.name
  schema   = snowflake_schema.governance_security.name

  # allowed_values is deprecated in snowflakedb/snowflake v2.20 in favor of
  # ordered_allowed_values (functionally identical here; order isn't
  # semantically meaningful for this tag, but this avoids the provider
  # deprecation warning).
  ordered_allowed_values = ["EMAIL", "PHONE", "NAME"]

  comment = "Classifies a column's PII category. Shared across DEV/STAGE/PROD by fully-qualified reference; attached to individual columns starting Phase 6 once gold models exist."
}
