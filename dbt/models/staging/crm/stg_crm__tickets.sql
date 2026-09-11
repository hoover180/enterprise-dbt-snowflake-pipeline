with tickets as (

    select
        ticket_id,
        account_id,
        category,
        created_date,
        status
    from {{ source('crm', 'crm_tickets') }}

),

customers as (

    select account_id
    from {{ ref('stg_crm__customers') }}

)

select
    tickets.ticket_id,
    tickets.account_id,
    tickets.category,
    tickets.created_date,
    tickets.status,
    -- ~3-5% of tickets reference an account_id with no corresponding
    -- record in stg_crm__customers at all -- an orphaned account
    -- reference (see ADR-006). Flagged, not dropped: the ticket itself
    -- is otherwise valid and its history is real.
    customers.account_id is null as is_ghost_account
from tickets
left join customers on tickets.account_id = customers.account_id
