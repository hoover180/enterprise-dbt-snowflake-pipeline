-- The real SCD2 correctness invariant: an order can have arbitrarily many
-- closed historical versions, but never more than one currently-open
-- (dbt_valid_to IS NULL) row at the same time. Returns offending rows --
-- an empty result set means the test passes.
select
    order_key,
    count(*) as open_versions
from {{ ref('erp_orders_status_snapshot') }}
where dbt_valid_to is null
group by order_key
having count(*) > 1
