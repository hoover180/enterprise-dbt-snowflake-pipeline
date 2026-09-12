"""Contract tests for data_gen's synthetic generators, order_pool.py especially.

Uses plain unittest (stdlib) rather than pytest -- this project has no
existing Python test infrastructure, and pytest isn't a declared dependency
(requirements.txt), so adding it just for this suite would be a new
dependency for a handful of cheap, deterministic checks that don't need a
fixture/plugin ecosystem. Run directly:

    .venv/Scripts/python -m unittest discover -s tests -v

or, if pytest happens to be installed locally, `pytest tests/` also
discovers and runs these (pytest natively collects unittest.TestCase
subclasses), without requiring it.

These are contract tests on data_gen's *generation logic* -- they call
order_pool.build_order()/ghost_order_number() directly and check
properties that must hold for ERP/web/CRM correlation to work at all, per
ADR-009 in docs/data_modeling_decisions.md. They do not touch Snowflake or
require a loaded dataset.
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_gen"))

import order_pool  # noqa: E402  (path must be extended before this import)


def expected_birthday_collisions(draws: int, pool_size: int) -> float:
    """Closed-form expected number of colliding pairs among `draws` uniform draws from `pool_size` slots."""
    return draws * (draws - 1) / (2 * pool_size)


class TestOrdersPerRegionMismatchIsDetectable(unittest.TestCase):
    """ADR-009's own "consequences" section: erp.py/clickstream.py/crm.py's
    --orders-per-region values "must be passed identically... for order
    references to actually correlate," and "passing mismatched values
    silently breaks correlation from an inconsistent universe size, not
    from any of the deliberate mechanisms below." Confirmed directly (not
    assumed) that this is genuinely silent: none of erp.py/clickstream.py/
    crm.py/order_pool.py raises for a mismatch -- there is no ValueError or
    any other cross-script validation anywhere in data_gen/ for this. This
    test proves the resulting breakage is real and measurable, not just a
    theoretical possibility implied by the ADR's prose.
    """

    def test_mismatched_orders_per_region_produces_phantom_web_matches(self):
        erp_orders_per_region = 400
        mismatched_downstream_orders_per_region = 500  # what clickstream.py/crm.py would wrongly be told

        real_order_numbers = set(range(1, erp_orders_per_region + 1))

        # Order numbers erp.py (at its own, correct size) never emits at
        # all, but that clickstream.py's matched-purchase loop would still
        # iterate over and treat as real, purely because of the mismatched
        # flag -- build_order() computes a full plausible order for ANY
        # (region, order_number) pair regardless of which numbers erp.py's
        # own generation loop actually reaches.
        phantom_range = range(erp_orders_per_region + 1, mismatched_downstream_orders_per_region + 1)
        phantom_matches = [
            order_number
            for order_number in phantom_range
            if order_pool.build_order("US", order_number)["has_web_purchase"]
        ]

        self.assertGreater(
            len(phantom_matches),
            0,
            "expected a mismatched --orders-per-region to produce at least one phantom "
            "web-purchase reference to an order erp.py never generated -- if this ever "
            "fails, something now guards against the mismatch and this test should be "
            "revisited, not just loosened",
        )
        for order_number in phantom_matches:
            self.assertNotIn(
                order_number,
                real_order_numbers,
                "a phantom match landed inside the real order range -- test setup is wrong",
            )


class TestGhostOrderNumberNeverCollidesWithReal(unittest.TestCase):
    """ghost_order_number() is documented to be "numbered immediately past
    the real per-region range... so order_id_for(region, ...) on the result
    can never collide with a real order for the same orders_per_region."
    Structurally guaranteed by construction (orders_per_region + a
    positive offset), but locked in here as a permanent regression guard:
    a future refactor of the arithmetic could silently break this.
    """

    def test_ghost_number_always_exceeds_orders_per_region(self):
        rng = random.Random(20260911)
        for orders_per_region in (1, 10, 500, 1000):
            real_order_numbers = set(range(1, orders_per_region + 1))
            for _ in range(2_000):
                ghost_number = order_pool.ghost_order_number(orders_per_region, rng)
                self.assertGreater(ghost_number, orders_per_region)
                self.assertNotIn(ghost_number, real_order_numbers)


class TestGhostOrderPoolSizeAvoidsBirthdayParadoxCollisions(unittest.TestCase):
    """ADR-009's "found and fixed during implementation": GHOST_ORDER_POOL_SIZE
    was originally 25 (matching crm.py's GHOST_ACCOUNT_POOL_SIZE), and web's
    ~45-50 independent orphan draws per default run (split across 2 regions,
    so ~20-25 per region) collided with each other via the birthday paradox
    -- checked directly against real generator output at the time: 382
    purchase events, only 368 distinct transaction_id. Widened to 200.

    Expressed as a closed-form expected-collision bound rather than a
    single seeded random draw, which could pass or fail by chance either
    way at a given seed regardless of whether the pool is actually sized
    correctly.
    """

    def test_current_pool_size_keeps_expected_collisions_low(self):
        # Comfortably above the ~20-25 per-region draws this project's
        # default run produces (WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS against
        # DEFAULT_ORDERS_PER_REGION), so this bound holds even if orphan
        # volume grows somewhat before this pool size is revisited.
        realistic_draws_per_region = 30
        expected = expected_birthday_collisions(realistic_draws_per_region, order_pool.GHOST_ORDER_POOL_SIZE)
        self.assertLess(
            expected,
            2.5,
            f"expected ~{expected:.2f} colliding pairs at {realistic_draws_per_region} draws against "
            f"GHOST_ORDER_POOL_SIZE={order_pool.GHOST_ORDER_POOL_SIZE} -- pool may be too small again",
        )

    def test_original_pool_size_would_not_have_kept_collisions_low(self):
        # Confirms the bound above is actually discriminating (would have
        # failed at the original, too-small pool size) rather than being
        # loose enough to pass at any pool size.
        realistic_draws_per_region = 30
        original_pool_size = 25
        expected_at_original_size = expected_birthday_collisions(realistic_draws_per_region, original_pool_size)
        self.assertGreater(expected_at_original_size, 10)


class TestBuildOrderIsPureAndDeterministic(unittest.TestCase):
    """The entire shared-order-pool design (ADR-009's "Decision" section)
    depends on build_order(region, order_number) being a pure function of
    its two arguments plus the fixed ORDER_POOL_SEED -- independent of call
    order and of any script's own --seed. If this regresses, ERP/web/CRM
    would silently stop agreeing on order content with no other symptom.
    """

    def test_repeated_calls_are_identical(self):
        first = order_pool.build_order("US", 217)
        second = order_pool.build_order("US", 217)
        self.assertEqual(first, second)

    def test_call_order_does_not_matter(self):
        order_4_first = order_pool.build_order("EU", 4)
        order_217_first = order_pool.build_order("EU", 217)
        order_4_second = order_pool.build_order("EU", 4)
        order_217_second = order_pool.build_order("EU", 217)
        self.assertEqual(order_4_first, order_4_second)
        self.assertEqual(order_217_first, order_217_second)


if __name__ == "__main__":
    unittest.main()
