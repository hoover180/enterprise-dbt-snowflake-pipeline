terraform {
  required_version = ">= 1.5.0"

  required_providers {
    # Provider was renamed from Snowflake-Labs/snowflake to the official
    # snowflakedb/snowflake in mid-2024; this pins the current official one.
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = "~> 2.20"
    }
  }
}
