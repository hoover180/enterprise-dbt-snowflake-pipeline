{% macro web_events_parsed() %}

{#
    RAW_WEB_EVENTS lands as a single VARIANT column (event_data) -- see
    docs/synthetic_data_spec.md, "Web clickstream source". Every field is
    extracted with colon syntax (event_data:field::type); there is nothing
    to FLATTEN since each raw row is already one event.

    user_id / customer_global_id is a mid-year schema drift (cutoff
    2025-07-01) confirmed directly against DEV_ANALYTICS.RAW.RAW_WEB_EVENTS:
    503 rows carry user_id, 547 carry customer_global_id, with zero rows
    carrying both or neither. The two are coalesced into a single
    customer_id column here rather than exposed as two columns -- see
    ADR-004 in docs/data_modeling_decisions.md for why.

    event_timestamp / event_date / ingested_at / quantity use TRY_CAST
    rather than a plain ::type cast. Confirmed directly (not assumed): a
    plain ::type cast on a VARIANT raises a hard SQL error for a value that
    doesn't convert (e.g. `parse_json('{"x":"not-a-date"}'):x::date` fails
    the whole query with "Failed to cast variant value... to DATE") instead
    of yielding NULL. A plain cast would therefore crash the entire model
    build the first time a single malformed event showed up, defeating the
    point of quarantining on VARIANT parsing failures. TRY_CAST yields NULL
    for an unparseable value instead, which web_quarantine_reason() then
    catches. The current synthetic dataset has no malformed events
    (confirmed directly: 0 failed casts across all 1,050 rows), so this
    path isn't exercised today, but it is real, not decorative.

    event_id / session_id / event_type / customer_id stay on a plain
    ::varchar cast -- a VARIANT always has a string representation, so
    there is no failure mode for TRY_CAST to guard against there.
#}
select
    event_data as raw_event_data,
    event_data:event_id::varchar as event_id,
    try_cast(event_data:event_timestamp::varchar as timestamp_ntz) as event_timestamp,
    try_cast(event_data:event_date::varchar as date) as event_date,
    try_cast(event_data:ingested_at::varchar as timestamp_ntz) as ingested_at,
    event_data:session_id::varchar as session_id,
    event_data:event_type::varchar as event_type,
    coalesce(event_data:user_id::varchar, event_data:customer_global_id::varchar) as customer_id,
    event_data:page_url::varchar as page_url,
    event_data:product_id::varchar as product_id,
    try_cast(event_data:quantity::varchar as number) as quantity,
    event_data:search_query::varchar as search_query
from {{ source('web', 'raw_web_events') }}

{% endmacro %}
