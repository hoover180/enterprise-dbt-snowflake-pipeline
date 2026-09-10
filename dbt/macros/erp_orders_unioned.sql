{% macro erp_orders_unioned() %}

{#
    Order-grain counterpart to erp_order_items_unioned() -- same config-driven
    per-shard mapping pattern (see docs/data_modeling_decisions.md ADR-002),
    reduced to the columns that actually vary at order grain: region,
    canonical order id, customer id, order_status, order_date. No currency
    conversion here -- monetary fields are item-level only and don't exist
    at this grain.

    order_status/fulfillment_state and customer_id/client_ref are confirmed
    identical across every line item of a given order (verified directly
    against DEV_ANALYTICS.RAW, not assumed), so `select distinct` per shard
    is a safe, non-lossy way to collapse item-grain rows down to one row per
    order without picking an arbitrary line item.
#}
{%- set shards = [
    {
        "region": "US",
        "relation": source("erp", "us_orders"),
        "order_id": "order_id",
        "customer_id": "customer_id",
        "order_date": "order_date",
        "order_status": "order_status",
    },
    {
        "region": "EU",
        "relation": source("erp", "eu_orders"),
        "order_id": "order_no",
        "customer_id": "client_ref",
        "order_date": "placed_on",
        "order_status": "fulfillment_state",
    },
] -%}

-- noqa: disable=LT02
{% for shard in shards %}
select distinct
    '{{ shard.region }}' as region,
    {{ shard.order_id }}::varchar as order_id,
    {{ shard.customer_id }}::varchar as customer_id,
    {{ shard.order_status }}::varchar as order_status,
    {{ shard.order_date }}::date as order_date
from {{ shard.relation }}
{% if not loop.last %}
union all
{% endif %}
{% endfor %}
-- noqa: enable=LT02

{% endmacro %}
