"""Generate the shared synthetic identity pool referenced by every source system."""

from __future__ import annotations

from typing import Any

from faker import Faker

POOL_SIZE = 350
IDENTITY_POOL_SEED = 20260904


def build_identity_pool(seed: int = IDENTITY_POOL_SEED) -> list[dict[str, Any]]:
    """Build the 350-person identity pool shared across all synthetic sources.

    Callers should always pass IDENTITY_POOL_SEED, not their own --seed, so
    every source (ERP, CRM, ...) resolves CUST-00001 .. CUST-00350 to the same
    name and email regardless of the row-generation seed a user supplies.
    """
    fake = Faker()
    fake.seed_instance(seed)

    people = []
    for index in range(1, POOL_SIZE + 1):
        people.append(
            {
                "index": index,
                "name": fake.name(),
                "email": fake.unique.email(),
            }
        )
    return people


if __name__ == "__main__":
    for person in build_identity_pool():
        print(person)
