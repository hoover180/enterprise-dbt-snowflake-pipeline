-- ADR-009: every purchase event must carry a transaction_id (the
-- order_pool order_id, real or ghost, that the checkout referenced).
-- transaction_id is otherwise event-type-conditional (like
-- page_url/product_id/search_query), so a blanket not_null schema test
-- would misclassify every non-purchase row as a failure -- this test
-- instead scopes the invariant to exactly the rows it applies to. Returns
-- offending rows; an empty result set means the test passes.
select
    event_id,
    event_type,
    transaction_id
from {{ ref('stg_web__events') }}
where event_type = 'purchase' and transaction_id is null
