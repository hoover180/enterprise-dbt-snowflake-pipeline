{{
    config(
        materialized='table'
    )
}}

-- Materialized as a table, not the staging default view: quarantined_at
-- uses current_timestamp(), which a view would re-evaluate on every SELECT
-- instead of fixing it to when the row was actually quarantined.

with parsed as (

    {{ web_events_parsed() }}

),

validated as (

    select
        *,
        {{ web_quarantine_reason() | trim | indent(8) }} as quarantine_reason
    from parsed

)

select
    raw_event_data,
    event_id,
    event_timestamp,
    event_date,
    ingested_at,
    session_id,
    event_type,
    customer_email,
    page_url,
    product_id,
    quantity,
    search_query,
    quarantine_reason,
    current_timestamp() as quarantined_at
from validated
where quarantine_reason is not null
