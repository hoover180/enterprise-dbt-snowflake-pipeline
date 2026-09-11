{% macro web_quarantine_reason() %}
-- customer_email is deliberately not checked here: it's legitimately null
-- on most rows (identity capture is sparse -- see
-- docs/synthetic_data_spec.md and ADR-008 in
-- docs/data_modeling_decisions.md), not a row-level validation failure.
case
    when event_id is null then 'missing_event_id'
    when event_timestamp is null then 'missing_or_unparseable_event_timestamp'
    when event_date is null then 'missing_or_unparseable_event_date'
    when ingested_at is null then 'missing_or_unparseable_ingested_at'
    when session_id is null then 'missing_session_id'
    when event_type is null then 'missing_event_type'
end
{% endmacro %}
