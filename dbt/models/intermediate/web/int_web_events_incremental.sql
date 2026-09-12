-- dbt_project.yml defaults intermediate/ to ephemeral (models never queried
-- directly, no persisted object needed -- see the comment there). An
-- incremental model must be a persisted table, so this config block
-- overrides that default for this model only. See ADR-005 in
-- docs/data_modeling_decisions.md.
--
-- unique_key is declared for documentation purposes even though Snowflake's
-- microbatch execution (delete+insert per event_time batch, not a
-- merge-on-key) does not use it for matching -- duplicate-safety instead
-- comes from event_timestamp being immutable per event_id (ADR-004) plus
-- the schema/singular tests below.
--
-- full_refresh=false: recommended for microbatch models so a plain
-- --full-refresh doesn't silently blow away accumulated history. A
-- deliberate full historical rebuild uses --event-time-start/
-- --event-time-end instead (see ADR-005's backfill procedure).
--
-- lookback=4, not 3: data_gen/clickstream.py's build_ingested_at() draws
-- late-arrival lag as rng.randint(2, 4) -- up to 4 days, not 3. Since
-- microbatch's lookback reaches back `lookback` batches before the batch
-- containing the run's own end time, a lookback of 3 cannot recover an
-- event whose original event-day batch is 4 days back on the day it
-- finally arrives -- confirmed as a real, reproduced drop, not just a
-- theoretical gap (see ADR-005's amendment in
-- docs/data_modeling_decisions.md).
{{
    config(
        materialized='incremental',
        incremental_strategy='microbatch',
        event_time='event_timestamp',
        batch_size='day',
        begin='2025-01-01',
        lookback=4,
        unique_key='event_id',
        full_refresh=false
    )
}}

-- Event-grain pass-through of stg_web__events, materialized incrementally
-- so late-arriving events (ingested_at up to 4 days after event_timestamp,
-- per data_gen/clickstream.py's build_ingested_at()) are absorbed by dbt's
-- microbatch lookback instead of requiring a full rebuild of the whole
-- history on every run.
select
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
    search_query
from {{ ref('stg_web__events') }}
