{{
    config(
        materialized='table'
    )
}}

-- Materialized as a table, not the staging default view: quarantined_at
-- uses current_timestamp(), which a view would re-evaluate on every SELECT
-- instead of fixing it to when the row was actually quarantined.

with unioned as (

    {{ erp_order_items_unioned() }}

),

validated as (

    select
        *,
        {{ erp_quarantine_reason() }} as quarantine_reason
    from unioned

)

select
    region,
    source_order_id,
    source_line_item_id,
    customer_id,
    customer_email,
    order_date,
    order_status,
    product_id,
    quantity,
    currency_original,
    unit_price_original,
    tax_amount_original,
    unit_price_usd,
    tax_amount_usd,
    ship_to_country,
    quarantine_reason,
    current_timestamp() as quarantined_at
from validated
where quarantine_reason is not null
