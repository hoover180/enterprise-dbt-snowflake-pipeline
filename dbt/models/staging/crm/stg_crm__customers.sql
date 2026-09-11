select
    account_id,
    contact_email,
    name,
    {{ crm_standardize_country('country') }} as country,
    created_date,
    last_seen_date
from {{ source('crm', 'crm_customers') }}
