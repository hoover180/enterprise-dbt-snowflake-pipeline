with parsed as (

    {{ web_events_parsed() }}

),

validated as (

    select
        *,
        {{ web_quarantine_reason() | trim | indent(8) }} as quarantine_reason
    from parsed

),

deduplicated as (

    select *
    from validated
    where quarantine_reason is null
    -- ~5% of raw rows are exact pixel-retry copies (same event_id, fully
    -- identical payload -- confirmed directly, not assumed; see ADR-004).
    -- Earliest ingested_at wins as the canonical copy.
    qualify row_number() over (
        partition by event_id
        order by ingested_at asc
    ) = 1

)

select
    event_id,
    event_timestamp,
    event_date,
    ingested_at,
    session_id,
    event_type,
    customer_id,
    page_url,
    product_id,
    quantity,
    search_query
from deduplicated
