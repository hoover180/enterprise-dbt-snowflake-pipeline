#!/usr/bin/env bash
# Full local bootstrap: generate all three synthetic sources (correlated via
# the shared order pool), load them into DEV_ANALYTICS.RAW, then run a full
# dbt build against the dev target.
#
# Usage: scripts/bootstrap.sh [orders-per-region]
#   orders-per-region defaults to 500 (data_gen/order_pool.py's own
#   DEFAULT_ORDERS_PER_REGION) and is passed identically to erp.py,
#   clickstream.py, and crm.py -- see docs/data_modeling_decisions.md
#   ADR-009: a mismatched value here silently breaks ERP/web/CRM order
#   correlation instead of raising an error (see tests/test_generators.py's
#   TestOrdersPerRegionMismatchIsDetectable for why this matters).
#
# Requires: dependencies from requirements.txt installed (this repo uses a
# .venv/ virtualenv -- resolved below, falling back to plain `python`/`dbt`
# on PATH if no .venv/ is present), and ~/.dbt/profiles.yml set up per
# docs/dbt_profile_setup.md (real Snowflake key-pair auth -- there is no
# mock/local Snowflake target, so this genuinely builds against Snowflake).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

ORDERS_PER_REGION="${1:-500}"

if [[ -x .venv/Scripts/python ]]; then
    VENV_BIN=.venv/Scripts    # Windows venv layout
elif [[ -x .venv/bin/python ]]; then
    VENV_BIN=.venv/bin        # POSIX venv layout
else
    VENV_BIN=""
fi

if [[ -n "$VENV_BIN" ]]; then
    PYTHON="$VENV_BIN/python"
    DBT="$VENV_BIN/dbt"
else
    PYTHON=python
    DBT=dbt
fi

echo "== Generating synthetic sources (--orders-per-region ${ORDERS_PER_REGION}) =="
"$PYTHON" data_gen/erp.py --orders-per-region "$ORDERS_PER_REGION"
"$PYTHON" data_gen/clickstream.py --orders-per-region "$ORDERS_PER_REGION"
"$PYTHON" data_gen/crm.py --orders-per-region "$ORDERS_PER_REGION"

echo "== Loading into DEV_ANALYTICS.RAW =="
"$PYTHON" data_gen/load_raw.py

echo "== dbt deps =="
"$DBT" deps --project-dir dbt --profiles-dir ~/.dbt

echo "== dbt build (dev target) =="
"$DBT" build --project-dir dbt --profiles-dir ~/.dbt

echo "== Bootstrap complete =="
