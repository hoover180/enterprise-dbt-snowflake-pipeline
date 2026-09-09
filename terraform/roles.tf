resource "snowflake_account_role" "transformer" {
  name    = "TRANSFORMER_ROLE"
  comment = "Least-privilege role for dbt transform runs: warehouse USAGE plus schema-scoped CREATE across DEV/STAGE/PROD analytics databases."
}

resource "snowflake_account_role" "read_only_analyst" {
  name    = "READ_ONLY_ANALYST"
  comment = "Read-only role for BI/analyst consumption of marts. Scoped at the database level until marts schemas exist; narrow to schema-level grants once they do."
}

locals {
  transformer_databases = {
    dev   = snowflake_database.dev_analytics.name
    stage = snowflake_database.stage_analytics.name
    prod  = snowflake_database.prod_analytics.name
  }

  # Marts schemas don't exist yet (Phase 2 is infra-only), so the analyst
  # role is scoped at the database level for now, on STAGE/PROD only, not
  # DEV. Narrow this to schema-level grants on the marts schema once it
  # is materialized by dbt.
  analyst_databases = {
    stage = snowflake_database.stage_analytics.name
    prod  = snowflake_database.prod_analytics.name
  }

  analyst_select_grants = {
    for pair in setproduct(keys(local.analyst_databases), ["TABLES", "VIEWS"]) :
    "${pair[0]}_${lower(pair[1])}" => {
      database    = local.analyst_databases[pair[0]]
      object_type = pair[1]
    }
  }
}

# --- TRANSFORMER_ROLE ---

resource "snowflake_grant_privileges_to_account_role" "transformer_warehouse_xs" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.transform_xs.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_warehouse_m" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.transform_m.name
  }
}

# Database USAGE (and CREATE SCHEMA, so dbt can provision its own target
# schemas) is a prerequisite for the schema-scoped CREATE grants below to be
# usable at all.
resource "snowflake_grant_privileges_to_account_role" "transformer_database_usage" {
  for_each          = local.transformer_databases
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE", "CREATE SCHEMA"]
  on_account_object {
    object_type = "DATABASE"
    object_name = each.value
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_schema_create_existing" {
  for_each          = local.transformer_databases
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["CREATE TABLE", "CREATE VIEW"]
  on_schema {
    all_schemas_in_database = each.value
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_schema_create_future" {
  for_each          = local.transformer_databases
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["CREATE TABLE", "CREATE VIEW"]
  on_schema {
    future_schemas_in_database = each.value
  }
}

# The privilege grants above are moot unless var.snowflake_user can actually
# assume TRANSFORMER_ROLE -- a role's privileges only apply to a session
# that has USE ROLE'd into it. This is the role-to-user grant dbt's own
# connection (see docs/dbt_profile_setup.md) depends on.
resource "snowflake_grant_account_role" "transformer_to_user" {
  role_name = snowflake_account_role.transformer.name
  user_name = var.snowflake_user
}

# USAGE on GOVERNANCE.SECURITY makes the PII tag and EMAIL_MASK policy
# visible/referenceable to TRANSFORMER_ROLE, but is NOT sufficient to attach
# either to a column -- that requires the separate APPLY TAG / APPLY MASKING
# POLICY privileges, deliberately deferred to Phase 6 when there's a real
# column to attach to (see docs/data_modeling_decisions.md ADR-001).
# READ_ONLY_ANALYST needs no grant here: masking is evaluated at query time
# against the mart table, not by reading the policy object itself.
resource "snowflake_grant_privileges_to_account_role" "transformer_governance_database_usage" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.governance.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_governance_schema_usage" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_schema {
    schema_name = "\"${snowflake_database.governance.name}\".\"${snowflake_schema.governance_security.name}\""
  }
}

# --- READ_ONLY_ANALYST ---

resource "snowflake_grant_privileges_to_account_role" "analyst_database_usage" {
  for_each          = local.analyst_databases
  account_role_name = snowflake_account_role.read_only_analyst.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = each.value
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_schema_usage_existing" {
  for_each          = local.analyst_databases
  account_role_name = snowflake_account_role.read_only_analyst.name
  privileges        = ["USAGE"]
  on_schema {
    all_schemas_in_database = each.value
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_schema_usage_future" {
  for_each          = local.analyst_databases
  account_role_name = snowflake_account_role.read_only_analyst.name
  privileges        = ["USAGE"]
  on_schema {
    future_schemas_in_database = each.value
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_select_existing" {
  for_each          = local.analyst_select_grants
  account_role_name = snowflake_account_role.read_only_analyst.name
  privileges        = ["SELECT"]
  on_schema_object {
    all {
      object_type_plural = each.value.object_type
      in_database        = each.value.database
    }
  }
}

resource "snowflake_grant_privileges_to_account_role" "analyst_select_future" {
  for_each          = local.analyst_select_grants
  account_role_name = snowflake_account_role.read_only_analyst.name
  privileges        = ["SELECT"]
  on_schema_object {
    future {
      object_type_plural = each.value.object_type
      in_database        = each.value.database
    }
  }
}
