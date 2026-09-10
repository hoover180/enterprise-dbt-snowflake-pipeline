with unioned as (

    {{ erp_order_items_unioned() }}

),

validated as (

    select
        *,
        region || '-' || source_order_id || '-' || source_line_item_id::varchar
            as order_item_key,
        {{ erp_quarantine_reason() }} as quarantine_reason
    from unioned

)

select
    region,
    source_order_id,
    source_line_item_id,
    order_item_key,
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
    ship_to_country
from validated
where quarantine_reason is null
