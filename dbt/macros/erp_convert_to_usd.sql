{% macro erp_convert_to_usd(amount_column, currency_column) %}
case
    when {{ currency_column }} = 'USD' then {{ amount_column }}
    when {{ currency_column }} = 'EUR' then {{ amount_column }} * {{ var('erp_eur_to_usd_rate') }}
    when {{ currency_column }} = 'GBP' then {{ amount_column }} * {{ var('erp_gbp_to_usd_rate') }}
    else null
end
{% endmacro %}
