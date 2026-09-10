{% macro erp_order_items_unioned() %}

{#
    Regional ERP shards use independently-maintained column names for the
    same fields (see docs/synthetic_data_spec.md). This config-driven loop
    is the single place that maps each region's native columns onto the
    canonical staging columns, so adding a future shard means adding an
    entry here rather than writing a near-duplicate SELECT.

    Native customer identifiers (customer_id / client_ref) are passed
    through unchanged -- cross-shard identity resolution is Phase 5, not
    staging.

    order_date / placed_on are DATE columns with no time-of-day component
    and no regional offset present in the source data (confirmed against
    DEV_ANALYTICS.RAW directly, not assumed from the generator). There is
    therefore no timezone conversion to perform; the cast below is a
    documented no-op, not a silent pass-through.
#}
{%- set shards = [
    {
        "region": "US",
        "relation": source("erp", "us_orders"),
        "order_id": "order_id",
        "line_item_id": "line_item_no",
        "customer_id": "customer_id",
        "customer_email": "customer_email",
        "order_date": "order_date",
        "order_status": "order_status",
        "product_id": "sku",
        "quantity": "quantity",
        "unit_price": "unit_price",
        "tax_amount": "sales_tax",
        "currency": "currency",
        "ship_to_country": "ship_to_country",
    },
    {
        "region": "EU",
        "relation": source("erp", "eu_orders"),
        "order_id": "order_no",
        "line_item_id": "item_seq",
        "customer_id": "client_ref",
        "customer_email": "contact_email",
        "order_date": "placed_on",
        "order_status": "fulfillment_state",
        "product_id": "product_code",
        "quantity": "units",
        "unit_price": "price_each",
        "tax_amount": "vat_amount",
        "currency": "currency_code",
        "ship_to_country": "ship_to_country",
    },
] -%}

-- noqa: disable=LT02
{% for shard in shards %}
select
    '{{ shard.region }}' as region,
    {{ shard.order_id }}::varchar as source_order_id,
    {{ shard.line_item_id }}::number as source_line_item_id,
    {{ shard.customer_id }}::varchar as customer_id,
    {{ shard.customer_email }}::varchar as customer_email,
    {{ shard.order_date }}::date as order_date,
    {{ shard.order_status }}::varchar as order_status,
    {{ shard.product_id }}::varchar as product_id,
    {{ shard.quantity }}::number as quantity,
    {{ shard.currency }}::varchar as currency_original,
    {{ shard.unit_price }}::number(12, 2) as unit_price_original,
    {{ shard.tax_amount }}::number(12, 2) as tax_amount_original,
    {{ erp_convert_to_usd(shard.unit_price, shard.currency) }} as unit_price_usd,
    {{ erp_convert_to_usd(shard.tax_amount, shard.currency) }} as tax_amount_usd,
    {{ shard.ship_to_country }}::varchar as ship_to_country
from {{ shard.relation }}
{% if not loop.last %}
union all
{% endif %}
{% endfor %}
-- noqa: enable=LT02

{% endmacro %}
