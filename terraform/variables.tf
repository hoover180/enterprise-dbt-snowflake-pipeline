variable "snowflake_account" {
  description = "Snowflake account identifier in ORGANIZATION-ACCOUNT format (e.g. \"YOUR_ACCOUNT_LOCATOR\"). Supply via terraform.tfvars."
  type        = string
}

variable "snowflake_user" {
  # Uppercase, not lowercase: login authentication itself is
  # case-insensitive, but this value is also consumed as a SQL identifier by
  # resources like snowflake_grant_account_role.user_name, which the
  # provider quotes verbatim (see its identifiers_rework_design_decisions.md
  # guide) -- it must match Snowflake's actual stored identifier exactly,
  # which is uppercase if the user was created unquoted. Confirm via
  # `SHOW GRANTS TO USER <your_user>` and use the returned grantee_name.
  description = "Snowflake username used for key-pair (JWT) authentication, in its canonical (uppercase) stored form. Supply via terraform.tfvars."
  type        = string
}

variable "snowflake_private_key_path" {
  description = "Absolute path to the PEM-encoded RSA private key (.p8) used for key-pair authentication. User-specific: no default, and must never be hardcoded or committed."
  type        = string
  sensitive   = true
}
