{% macro web_events_parsed() %}

{#
    RAW_WEB_EVENTS lands as a single VARIANT column (event_data) -- see
    docs/synthetic_data_spec.md, "Web clickstream source". Every field is
    extracted with colon syntax (event_data:field::type); there is nothing
    to FLATTEN since each raw row is already one event.

    user_email / customer_global_email is a mid-year schema drift (cutoff
    2025-07-01, same cutoff as the original user_id/customer_global_id
    naming -- only the field names and the content they carry changed, see
    ADR-008 in docs/data_modeling_decisions.md). Unlike the original ID
    fields, these are legitimately absent on most rows: a real web pixel
    only captures a customer identity signal for a minority of traffic
    (event-type-dependent capture rate -- see
    docs/synthetic_data_spec.md), so the two are coalesced into a single,
    nullable customer_email column here rather than exposed as two columns
    -- same coalesce pattern ADR-004 established, now carrying a sparse,
    imperfect email signal instead of an always-present exact key.

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

    event_timestamp / ingested_at cast to TIMESTAMP_TZ, not TIMESTAMP_NTZ
    (found and fixed during Phase 3B's incremental-model work -- see ADR-005
    in docs/data_modeling_decisions.md). The source values are UTC
    ISO-8601 strings ('...Z' suffix). Casting to TIMESTAMP_NTZ silently
    drops that UTC marker and stores a timezone-naive wall-clock value;
    Snowflake then resolves any later NTZ-vs-TZ comparison (exactly what
    dbt's microbatch strategy does for its batch boundaries) using the
    *session's* TIMEZONE parameter, not UTC. Confirmed directly this
    silently drops/misclassifies rows near a UTC day boundary whenever the
    session timezone isn't UTC (this project's Snowflake session defaults
    to America/Los_Angeles). TIMESTAMP_TZ preserves the 'Z' as an explicit
    UTC offset, so every later comparison is unambiguous.

    event_id / session_id / event_type / customer_email stay on a plain
    ::varchar cast -- a VARIANT always has a string representation, so
    there is no failure mode for TRY_CAST to guard against there. A missing
    key (the common case for customer_email -- see above) extracts to
    VARIANT NULL, which ::varchar carries through as NULL, not an error.
#}
select
    event_data as raw_event_data,
    event_data:event_id::varchar as event_id,
    try_cast(event_data:event_timestamp::varchar as timestamp_tz) as event_timestamp,
    try_cast(event_data:event_date::varchar as date) as event_date,
    try_cast(event_data:ingested_at::varchar as timestamp_tz) as ingested_at,
    event_data:session_id::varchar as session_id,
    event_data:event_type::varchar as event_type,
    coalesce(event_data:user_email::varchar, event_data:customer_global_email::varchar) as customer_email,
    event_data:page_url::varchar as page_url,
    event_data:product_id::varchar as product_id,
    try_cast(event_data:quantity::varchar as number) as quantity,
    event_data:search_query::varchar as search_query
from {{ source('web', 'raw_web_events') }}

{% endmacro %}
