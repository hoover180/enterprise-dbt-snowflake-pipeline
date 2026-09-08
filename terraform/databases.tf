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
