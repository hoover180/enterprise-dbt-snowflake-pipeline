variable "snowflake_account" {
  description = "Snowflake account identifier in ORGANIZATION-ACCOUNT format (e.g. \"YOOJGIC-KW80562\")."
  type        = string
  default     = "YOOJGIC-KW80562"
}

variable "snowflake_user" {
  description = "Snowflake username used for key-pair (JWT) authentication."
  type        = string
  default     = "hoover365"
}

variable "snowflake_private_key_path" {
  description = "Absolute path to the PEM-encoded RSA private key (.p8) used for key-pair authentication. User-specific: no default, and must never be hardcoded or committed."
  type        = string
  sensitive   = true
}
