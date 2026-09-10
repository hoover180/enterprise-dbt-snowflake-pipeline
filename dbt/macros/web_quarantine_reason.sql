{% macro web_quarantine_reason() %}
case
    when event_id is null then 'missing_event_id'
    when event_timestamp is null then 'missing_or_unparseable_event_timestamp'
    when event_date is null then 'missing_or_unparseable_event_date'
    when ingested_at is null then 'missing_or_unparseable_ingested_at'
    when session_id is null then 'missing_session_id'
    when event_type is null then 'missing_event_type'
    when customer_id is null then 'missing_customer_id'
end
{% endmacro %}
