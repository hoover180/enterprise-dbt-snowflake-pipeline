"""Generate the shared synthetic identity pool referenced by every source system."""

from __future__ import annotations

import random
from typing import Any

from faker import Faker

POOL_SIZE = 350
IDENTITY_POOL_SEED = 20260904

# --- Designed email-collision pairs ("common-name confusion") -------------
# A small, documented, bounded subset of the pool where two DIFFERENT real
# people's canonical emails are a single bounded edit apart -- the same
# transposition primitive data_gen/clickstream.py's own tier-2 typo
# mechanism uses. This exists so a fraction of web's distorted identity
# signals (see clickstream.py's roll_identity_capture / COLLISION_RATE)
# land on a DIFFERENT real customer's actual canonical email instead of a
# synthetic near-miss nobody owns -- the realistic "confident wrong match"
# case a fuzzy identity resolver must be able to catch, not just ordinary
# typos it can safely fuzzy-match back to the right person.
#
# Two independently Faker-generated emails are vanishingly unlikely to be
# edit-distance-1 apart by chance at this pool size, so this is built
# deliberately, as a post-process step after the base 350 people exist: for
# each designated pair, the "target" person's own canonical email is
# overwritten to be a transposition of their paired "source" person's email.
# Both people remain entirely real, distinct, independently-Faker-named
# synthetic customers -- only the target's email string is chosen
# deliberately rather than drawn fresh from Faker. Uses its own RNG,
# independent of the Faker instance that built `people`, so it never
# perturbs the base name/email sequence for any index not chosen as a
# collision target. See the freeze-baseline ADR in
# docs/data_modeling_decisions.md for the realized pair count.
COLLISION_PAIR_COUNT = 40


def _transpose_local_part(email: str, rng: random.Random) -> str | None:
    """One adjacent-character transposition on the email's local part, or None if too short."""
    local, _, domain = email.partition("@")
    if len(local) < 2:
        return None
    position = rng.randrange(len(local) - 1)
    chars = list(local)
    chars[position], chars[position + 1] = chars[position + 1], chars[position]
    candidate_local = "".join(chars)
    if candidate_local == local:
        return None
    return f"{candidate_local}@{domain}"


def _apply_collision_pairs(people: list[dict[str, Any]], seed: int) -> None:
    """Mutate `people` in place, designating up to COLLISION_PAIR_COUNT collision pairs.

    For each pair, the source person gets `collision_target_index` pointing
    at the target person, and the target person's `email` is overwritten to
    be a transposition of the source's real email. Walks a single shuffled
    pass over the pool rather than re-sampling, so this always terminates
    and never revisits the same person twice as source or target.
    """
    rng = random.Random(f"{seed}:collision-pairs")
    taken_emails = {person["email"] for person in people}
    order = list(range(len(people)))
    rng.shuffle(order)

    used_positions: set[int] = set()
    pair_count = 0
    for cursor, source_pos in enumerate(order):
        if pair_count >= COLLISION_PAIR_COUNT:
            break
        if source_pos in used_positions:
            continue
        source = people[source_pos]
        candidate_email = _transpose_local_part(source["email"], rng)
        if candidate_email is None or candidate_email in taken_emails:
            continue
        target_pos = next(
            (pos for pos in order[cursor + 1 :] if pos not in used_positions and pos != source_pos),
            None,
        )
        if target_pos is None:
            break
        target = people[target_pos]
        target["email"] = candidate_email
        taken_emails.add(candidate_email)
        used_positions.add(source_pos)
        used_positions.add(target_pos)
        source["collision_target_index"] = target["index"]
        pair_count += 1


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
                "collision_target_index": None,
            }
        )
    _apply_collision_pairs(people, seed)
    return people


if __name__ == "__main__":
    for person in build_identity_pool():
        print(person)
