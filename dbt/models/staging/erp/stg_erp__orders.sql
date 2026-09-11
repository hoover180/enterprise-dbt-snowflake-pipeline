with unioned as (

    {{ erp_orders_unioned() }}

),

keyed as (

    select
        *,
        {{ dbt_utils.generate_surrogate_key(['region', 'order_id']) }}
            as order_key
    from unioned

)

select
    region,
    order_id,
    order_key,
    customer_id,
    order_status,
    order_date
from keyed
