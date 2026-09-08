variable "snowflake_account" {
  description = "Snowflake account identifier in ORGANIZATION-ACCOUNT format (e.g. \"YOOJGIC-KW80562\")."
  type        = string
  default     = "YOOJGIC-KW80562"
}

variable "snowflake_user" {
  # Uppercase, not "hoover365": login authentication itself is
  # case-insensitive, but this value is also consumed as a SQL identifier by
  # resources like snowflake_grant_account_role.user_name, which the
  # provider quotes verbatim (see its identifiers_rework_design_decisions.md
  # guide) -- it must match Snowflake's actual stored identifier exactly,
  # which is uppercase because the user was created unquoted. Confirmed via
  # `SHOW GRANTS TO USER hoover365` returning grantee_name = HOOVER365.
  description = "Snowflake username used for key-pair (JWT) authentication, in its canonical (uppercase) stored form."
  type        = string
  default     = "HOOVER365"
}

variable "snowflake_private_key_path" {
  description = "Absolute path to the PEM-encoded RSA private key (.p8) used for key-pair authentication. User-specific: no default, and must never be hardcoded or committed."
  type        = string
  sensitive   = true
}
