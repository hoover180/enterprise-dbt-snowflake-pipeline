locals {
  # snowflakedb/snowflake v2.x configures the account via organization_name +
  # account_name rather than a single "account" field (that field still
  # exists but is gated behind an experimental feature flag). Derive both
  # from the single snowflake_account variable, e.g. "YOUR_ACCOUNT_LOCATOR".
  snowflake_account_parts = split("-", var.snowflake_account)
}

provider "snowflake" {
  organization_name = local.snowflake_account_parts[0]
  account_name      = local.snowflake_account_parts[1]
  user              = var.snowflake_user

  authenticator = "SNOWFLAKE_JWT"
  private_key   = file(var.snowflake_private_key_path)
}
