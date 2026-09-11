-- Reprocessing-safety invariant for the microbatch lookback (ADR-005,
-- mirroring Issue #17's assert_erp_orders_status_snapshot_single_open_version):
-- each event_id must appear exactly once no matter how many times its batch
-- has been deleted and reinserted by a later run. Returns offending rows --
-- an empty result set means the test passes.
select
    event_id,
    count(*) as row_count
from {{ ref('int_web_events_incremental') }}
group by event_id
having count(*) > 1
