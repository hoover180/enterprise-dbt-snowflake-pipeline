"""Build the ground-truth identity-resolution answer key for Phase 5.

Scans the already-generated source extracts (US_ORDERS.csv, EU_ORDERS.csv,
CRM_CUSTOMERS.csv) and, for each of the 350 people in the shared identity
pool, records the literal key each source uses to refer to that person. This
is a scan of what the generators actually wrote, not a re-derivation of the
generation logic, so it stays correct even if a source file was regenerated
with different row-level randomness (the identity pool itself is fixed by
IDENTITY_POOL_SEED).

Web is handled differently. Its identity signal (captured email) is sparse
and, for its "tier 2" rows, a genuine, irreversible character-level typo --
that's the point (see ADR-008 in docs/data_modeling_decisions.md): Phase 5A
needs a real fuzzy-matching problem, not a hidden exact key. That means
web's ground truth can no longer be recovered by scanning
CLICKSTREAM_EVENTS.json for literal keys the way the other three sources
still can -- a distorted tier-2 email can't be reverse-mapped to "whose
email was this" from the output alone. Instead, this script reads
data_gen/clickstream.py's own per-event identity-truth sidecar
(CLICKSTREAM_IDENTITY_TRUTH.csv), which is generated in the same run,
before dirtying, and is the only place the truth actually exists.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from identity_pool import IDENTITY_POOL_SEED, build_identity_pool

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTPUT_PATH = REPO_ROOT / "dbt" / "seeds" / "seed_match_truth.csv"

US_ORDERS_PATH = DATA_DIR / "US_ORDERS.csv"
EU_ORDERS_PATH = DATA_DIR / "EU_ORDERS.csv"
CLICKSTREAM_TRUTH_PATH = DATA_DIR / "CLICKSTREAM_IDENTITY_TRUTH.csv"
CRM_CUSTOMERS_PATH = DATA_DIR / "CRM_CUSTOMERS.csv"


def scan_erp_customer_ids(path: Path, email_column: str, customer_id_column: str) -> dict[str, str]:
    """Map normalized email -> the first customer identifier seen for it in this shard."""
    email_to_id: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            email = row[email_column].strip().lower()
            email_to_id.setdefault(email, row[customer_id_column])
    return email_to_id


def scan_crm_account_ids(path: Path) -> dict[str, list[str]]:
    """Map normalized email -> all account_id values seen for it, in file order."""
    email_to_accounts: dict[str, list[str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            email = row["contact_email"].strip().lower()
            accounts = email_to_accounts.setdefault(email, [])
            if row["account_id"] not in accounts:
                accounts.append(row["account_id"])
    return email_to_accounts


def scan_clickstream_truth(path: Path) -> dict[int, dict[str, Any]]:
    """Aggregate clickstream.py's per-event identity-truth sidecar by customer_index.

    Returns, per customer_index: how many canonical events truly belong to
    that person, how many of those actually captured an identity signal
    (sparse and event-type-dependent -- see docs/synthetic_data_spec.md),
    and the distinct observed (possibly dirty) values a fuzzy matcher would
    actually see for that person.
    """
    by_index: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"true_event_count": 0, "captured_event_count": 0, "captured_variants": []}
    )
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            entry = by_index[int(row["customer_index"])]
            entry["true_event_count"] += 1
            if row["captured"] == "True":
                entry["captured_event_count"] += 1
                if row["observed_value"] not in entry["captured_variants"]:
                    entry["captured_variants"].append(row["observed_value"])
    return by_index


def build_truth_rows() -> list[dict[str, object]]:
    people = build_identity_pool(IDENTITY_POOL_SEED)

    us_email_to_id = scan_erp_customer_ids(US_ORDERS_PATH, "customer_email", "customer_id")
    eu_email_to_id = scan_erp_customer_ids(EU_ORDERS_PATH, "contact_email", "client_ref")
    crm_email_to_accounts = scan_crm_account_ids(CRM_CUSTOMERS_PATH)
    clickstream_truth_by_index = scan_clickstream_truth(CLICKSTREAM_TRUTH_PATH)

    rows = []
    for person in people:
        index = person["index"]
        email = person["email"]
        normalized_email = email.strip().lower()

        erp_us_id = us_email_to_id.get(normalized_email)
        erp_eu_ref = eu_email_to_id.get(normalized_email)
        crm_accounts = crm_email_to_accounts.get(normalized_email, [])
        clickstream_truth = clickstream_truth_by_index.get(
            index, {"true_event_count": 0, "captured_event_count": 0, "captured_variants": []}
        )

        rows.append(
            {
                "person_index": index,
                "canonical_name": person["name"],
                "canonical_email": email,
                "erp_us_customer_id": erp_us_id or "",
                "erp_eu_client_ref": erp_eu_ref or "",
                "crm_account_id": ";".join(crm_accounts),
                "clickstream_true_event_count": clickstream_truth["true_event_count"],
                "clickstream_captured_event_count": clickstream_truth["captured_event_count"],
                "clickstream_captured_variants": ";".join(clickstream_truth["captured_variants"]),
                "appears_in_erp": bool(erp_us_id or erp_eu_ref),
                "appears_in_crm": bool(crm_accounts),
                "appears_in_clickstream": clickstream_truth["true_event_count"] > 0,
            }
        )
    return rows


def write_truth_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "person_index",
        "canonical_name",
        "canonical_email",
        "erp_us_customer_id",
        "erp_eu_client_ref",
        "crm_account_id",
        "clickstream_true_event_count",
        "clickstream_captured_event_count",
        "clickstream_captured_variants",
        "appears_in_erp",
        "appears_in_crm",
        "appears_in_clickstream",
    ]
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    write_truth_csv(build_truth_rows(), OUTPUT_PATH)
