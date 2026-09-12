-- ADR-009: refund_date/refund_amount must be populated if and only if
-- order_status = 'returned' -- a cross-column invariant column-level
-- not_null/accepted_values tests can't express (a column-level test on
-- refund_amount alone can't see order_status, and vice versa). Returns
-- offending rows; an empty result set means the test passes.
select
    region,
    order_id,
    order_status,
    ship_date,
    refund_date,
    refund_amount
from {{ ref('stg_erp__orders') }}
where
    (order_status = 'returned' and (refund_date is null or refund_amount is null))
    or (order_status != 'returned' and (refund_date is not null or refund_amount is not null))
