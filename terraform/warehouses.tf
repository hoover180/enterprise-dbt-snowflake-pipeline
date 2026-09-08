resource "snowflake_warehouse" "transform_xs" {
  name                = "TRANSFORM_XS"
  warehouse_size      = "XSMALL"
  auto_suspend        = 60
  auto_resume         = true
  initially_suspended = true
  comment             = "Default transform warehouse for dbt runs. Auto-suspends after 60s idle to control trial credit spend."
}

resource "snowflake_warehouse" "transform_m" {
  name                = "TRANSFORM_M"
  warehouse_size      = "MEDIUM"
  auto_suspend        = 60
  auto_resume         = true
  initially_suspended = true
  comment             = "Scaled-up transform warehouse for heavier dbt builds. Auto-suspends after 60s idle to control trial credit spend."
}
