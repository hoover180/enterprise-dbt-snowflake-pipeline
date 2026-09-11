{% macro crm_standardize_country(column) %}
case
{%- for variant in var('crm_us_country_variants') %}
    when {{ column }} = '{{ variant }}' then 'US'
{%- endfor %}
    else {{ column }}
end
{% endmacro %}
