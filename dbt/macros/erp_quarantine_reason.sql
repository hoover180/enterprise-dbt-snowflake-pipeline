{% macro erp_quarantine_reason() %}
case
    when source_order_id is null or source_line_item_id is null then 'missing_business_key'
    when customer_id is null then 'missing_customer_id'
    when quantity is null or quantity <= 0 then 'invalid_quantity'
    when unit_price_original is null or unit_price_original < 0 then 'invalid_unit_price'
    when currency_original is null then 'missing_currency'
    when unit_price_usd is null then 'unsupported_currency:' || currency_original
end
{% endmacro %}
