-- is_ghost_account must agree exactly with a direct anti-join of
-- stg_crm__tickets against stg_crm__customers on account_id: true iff
-- no matching account_id exists. This is the mechanism itself, checked
-- independently of stg_crm__tickets.sql's own join logic, as a
-- regression guard rather than a re-statement of the model's SQL.
with antijoin as (

    select tickets.ticket_id
    from {{ ref('stg_crm__tickets') }} as tickets
    left join {{ ref('stg_crm__customers') }} as customers
        on tickets.account_id = customers.account_id
    where customers.account_id is null

)

select tickets.ticket_id
from {{ ref('stg_crm__tickets') }} as tickets
left join antijoin
    on tickets.ticket_id = antijoin.ticket_id
where
    (tickets.is_ghost_account and antijoin.ticket_id is null)
    or (not tickets.is_ghost_account and antijoin.ticket_id is not null)
