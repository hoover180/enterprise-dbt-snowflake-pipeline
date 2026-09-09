resource "snowflake_database" "dev_analytics" {
  name    = "DEV_ANALYTICS"
  comment = "Development environment analytics database."
}

resource "snowflake_database" "stage_analytics" {
  name    = "STAGE_ANALYTICS"
  comment = "Staging environment analytics database, auto-deployed from main."
}

resource "snowflake_database" "prod_analytics" {
  name    = "PROD_ANALYTICS"
  comment = "Production analytics database, promoted via tagged release and approval gate."
}

# Shared governance vocabulary (PII tags, masking policies) lives here rather
# than in any one of DEV/STAGE/PROD_ANALYTICS -- see docs/data_modeling_decisions.md
# ADR-001 for why. GOVERNANCE.SECURITY (not PUBLIC) is the one fully-qualified
# name used everywhere a tag/policy is defined or attached; see tags.tf and
# masking_policies.tf.
resource "snowflake_database" "governance" {
  name    = "GOVERNANCE"
  comment = "Shared governance vocabulary (PII tags, masking policies) referenced by fully-qualified name from DEV/STAGE/PROD_ANALYTICS. Not a peer analytics environment."
}

resource "snowflake_schema" "governance_security" {
  database = snowflake_database.governance.name
  name     = "SECURITY"
  comment  = "Holds PII classification tags and masking policies. See docs/data_modeling_decisions.md ADR-001."
}
