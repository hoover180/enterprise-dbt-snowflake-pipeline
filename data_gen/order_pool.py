"""Generate the shared synthetic order/transaction pool referenced by ERP, web, and CRM.

Phase 5B pre-work (Issue #42) needs ERP, web, and CRM to reference the SAME
order/transaction identifiers -- something none of the three previously needed
to do (they only ever shared *customer* identity, via identity_pool.py, never
*order-level* identity). This module is the order-level analogue of
identity_pool.py: `build_order(region, order_number)` is a pure function of
its arguments plus the fixed ORDER_POOL_SEED below, so ERP, web, and CRM can
each independently derive the exact same order -- its items, dollar amounts,
lifecycle status, ship/refund timing, checkout promo outcome, and whether it
has a corresponding web purchase event -- with no file-read coupling between
the three generator scripts, and regardless of whichever run of the three
happens to execute first, or of any script's own `--seed` argument.

Just as identity_pool.py decouples customer identity from each script's own
`--seed` (so CUST-00042 always resolves to the same name/email), this module
decouples every order-level fact from each script's own `--seed` too. This is
a deliberate, larger version of the same precedent: order content used to be
each script's own private randomness; once three scripts need to agree on it,
it has to move to a shared, seed-independent pool the same way customer
identity already did. See ADR-009 in docs/data_modeling_decisions.md for why
a shared deterministic pool (rather than, say, one script writing an
intermediate file the others read) is the right mechanism here.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

ORDER_POOL_SEED = 20260911  # fixed, independent of any script's own --seed
REGIONS = ("US", "EU")
DEFAULT_ORDERS_PER_REGION = 500  # matches erp.py's own default

# Fixed "as-of" reference date, matching erp.py/clickstream.py/crm.py's existing
# AS_OF_DATE determinism fix -- order_date/checkout_date never fall after this.
AS_OF_DATE = date(2025, 12, 31)
ORDER_DATE_START = AS_OF_DATE - timedelta(days=365)
ORDER_DATE_END = AS_OF_DATE

# refund_date is deliberately allowed to extend past AS_OF_DATE -- see
# docs/data_modeling_decisions.md ADR-009's "Recognition-timing policy"
# section. A return's refund is the tail of a process that started before
# the as-of snapshot but whose paperwork can genuinely land after it; clamping
# every refund_date to AS_OF_DATE would silently erase the same-order,
# later-period reversal this whole branch exists to create. The extension is
# bounded to the maximum return lag below, not open-ended.
RETURN_LAG_DAYS = (5, 45)
REFUND_WINDOW_END = AS_OF_DATE + timedelta(days=max(RETURN_LAG_DAYS))

TAX_RATE_BY_REGION = {"US": Decimal("0.08"), "EU": Decimal("0.20")}
EU_CURRENCIES = ("EUR", "GBP")
EU_SHIP_COUNTRIES = ("DE", "FR", "NL", "ES", "IT")

# --- Lifecycle rates -----------------------------------------------------
# Return rate: NRF / Happy Returns "2025 Retail Returns Landscape" puts the
# 2025 online-retail return rate at 19.3% (up from 17.6% in 2024) -- this is
# the most-cited benchmark for general-merchandise online returns, and
# considerably higher than a flat 5-10% guess. Used as a single blended rate
# rather than a per-category rate: this dataset has no product-category
# concept, and inventing one solely to size a return rate would be exactly
# the kind of premature abstraction this project's own ADRs (e.g. ADR-006's
# flat-list country var) explicitly avoid building ahead of need.
RETURN_RATE = 0.193

# Cancellation rate: industry benchmarks put healthy ecommerce cancellation
# rates in the 2-8% band (Amazon, held up as the operational gold standard,
# targets below 2.5%). 4% sits in the middle of that band and is
# deliberately smaller than, and independent of, RETURN_RATE -- a
# cancellation is a distinct terminal state (never fulfilled at all), not a
# fulfilled-then-reversed order.
CANCEL_RATE = 0.04

# Of shipped, non-returned orders, the fraction that have progressed all the
# way to "delivered" rather than still being "shipped" (in transit) as of
# AS_OF_DATE -- unchanged from this project's original destructive-update
# rate (erp.py, pre-this-branch).
DELIVERED_RATE = 0.82

SHIP_LAG_DAYS = (1, 5)  # order_date -> ship_date (recognition timestamp)

# --- Checkout promo / amount-divergence mechanism -------------------------
# A documented, real arithmetic mechanism, not noise: a checkout-time
# promotional discount that sometimes fails ERP's backend eligibility
# validation. See ADR-009 for the worked example.
PROMO_RATE = 0.18
PROMO_DISCOUNT_RATE = Decimal("0.10")
PROMO_VALIDATION_FAILURE_RATE = 0.30

# --- Web/ERP order-coverage gap -------------------------------------------
# Fraction of real ERP orders that have a corresponding web purchase event at
# all. Deliberately well under half: a general-merchandise omnichannel
# retailer's phone/in-store/ad-blocked-or-declined-pixel share of orders is
# realistically the *majority* of volume, not a small tail -- see ADR-009.
WEB_MATCH_RATE = 0.35

# Fraction of all web purchase events (matched + orphan) that reference a
# transaction_id with no real ERP order at all -- representing a checkout
# that failed payment before the order ever persisted in ERP.
WEB_ORPHAN_SHARE_OF_PURCHASE_EVENTS = 0.12

# --- Second, independent amount-divergence mechanism: checkout-time shipping
# estimate -----------------------------------------------------------------
# The checkout promo mechanism above is one real divergence source, but it's
# thin on its own (peer review, freeze-gate synthesis): a single mechanism
# means every web/ERP amount mismatch traces back to the same root cause.
# This is a structurally different one -- not a second copy of the promo
# logic. checkout_total is what the customer's browser computed and showed
# at checkout, which for this dataset bakes in a flat, client-estimated
# shipping charge; ERP's recognized_total has no shipping line item at all
# (out of scope for this dataset's schema), so an order flagged here
# diverges from checkout_total by exactly that flat estimate. Both figures
# are locally correct under their own definition of "the total" -- this is
# the genuine "both sides right, different scope" reconciliation case, not
# noise and not a validation failure. Independent of PROMO_RATE: an order
# can have neither, either, or both mechanisms applied (see
# docs/data_modeling_decisions.md's freeze-baseline ADR for the measured
# overlap).
SHIP_ESTIMATE_RATE = 0.20
SHIP_ESTIMATE_FLAT = {"US": Decimal("8.99"), "EU": Decimal("11.99")}

# Per region. Deliberately much larger than crm.py's GHOST_ACCOUNT_POOL_SIZE
# (25): web draws roughly 45-50 orphan purchase events per default run (see
# clickstream.py), and a pool that small would make independent random draws
# collide with each other via the birthday paradox, silently shrinking the
# intended distinct-orphan population. 200 keeps collisions rare at this
# dataset's scale (checked directly, not assumed -- see ADR-009).
GHOST_ORDER_POOL_SIZE = 200


def order_id_for(region: str, order_number: int) -> str:
    """The order identifier any script can compute without generating anything."""
    return f"{region}-{order_number:06d}"


def ghost_order_number(orders_per_region: int, rng: random.Random) -> int:
    """An order_number guaranteed to never correspond to a real order.

    Numbered immediately past the real per-region range, exactly mirroring
    crm.py's ghost_account_ids construction, so order_id_for(region, ...)
    on the result can never collide with a real order for the same
    orders_per_region. build_order() itself doesn't distinguish "real" from
    "ghost" -- it happily computes a full, plausible order for ANY
    (region, order_number) pair -- so a ghost number still yields a
    realistic dollar figure via build_order(), it's simply a number erp.py's
    own generation loop (range(1, orders_per_region + 1)) never reaches, and
    therefore a transaction_id/order reference that never resolves to a real
    ERP row. Which specific ghost number gets used for a given event/ticket
    is each calling script's own, locally-seeded choice (there's no need for
    web's and CRM's ghost references to correlate with each other -- a
    failed web payment and a bogus CRM ticket reference are independent
    phenomena), so this takes the caller's own `rng`, unlike build_order()'s
    per-order-pool-seeded determinism.
    """
    return orders_per_region + rng.randint(1, GHOST_ORDER_POOL_SIZE)


def _order_rng(region: str, order_number: int) -> random.Random:
    """Deterministic per-order RNG: a pure function of (region, order_number).

    Seeding a fresh Random per order (rather than advancing one shared Random
    across a sequential loop) means any script can request order 217 first,
    order 4 second, or only order 4 at all, and always get the same content
    -- there is no "you must generate 1..N in order" dependency the way a
    single advancing RNG stream would impose.
    """
    return random.Random(f"{ORDER_POOL_SEED}:{region}:{order_number}")


def build_order(region: str, order_number: int) -> dict[str, Any]:
    """Build one order's full deterministic truth.

    Every field is reproducible from (region, order_number) alone. ERP, web,
    and CRM each call this directly and read only the fields they need --
    none of them mutate or persist it for another script to read.
    """
    rng = _order_rng(region, order_number)

    customer_index = rng.randint(1, 350)
    order_date = ORDER_DATE_START + timedelta(
        days=rng.randrange((ORDER_DATE_END - ORDER_DATE_START).days + 1)
    )
    currency = "USD" if region == "US" else rng.choice(EU_CURRENCIES)
    shipping_country = "US" if region == "US" else rng.choice(EU_SHIP_COUNTRIES)
    tax_rate = TAX_RATE_BY_REGION[region]

    item_count = rng.randint(1, 4)
    original_items = []
    for line_number in range(1, item_count + 1):
        quantity = rng.randint(1, 5)
        unit_price = Decimal(rng.randint(800, 25000)) / 100
        original_items.append(
            {
                "line_number": line_number,
                "product_id": f"SKU-{rng.randint(1, 250):05d}",
                "quantity": quantity,
                "unit_price": unit_price,
            }
        )
    original_subtotal = sum((item["unit_price"] * item["quantity"] for item in original_items), Decimal("0"))

    # --- Checkout promo: applied at checkout, sometimes fails ERP's backend
    # eligibility validation. When it validates, the discount is real and
    # baked into the stored line items (ERP's actual recognized amount drops
    # too); when it fails validation, ERP charges full price and the stored
    # items are left untouched -- a real, computable mismatch against what
    # the customer saw at checkout, not sampled noise.
    promo_applied = rng.random() < PROMO_RATE
    promo_validated = (rng.random() >= PROMO_VALIDATION_FAILURE_RATE) if promo_applied else None
    discount_multiplier = (
        Decimal("1") - PROMO_DISCOUNT_RATE if (promo_applied and promo_validated) else Decimal("1")
    )

    items = []
    for item in original_items:
        unit_price = (item["unit_price"] * discount_multiplier).quantize(Decimal("0.01"))
        tax_amount = (unit_price * item["quantity"] * tax_rate).quantize(Decimal("0.01"))
        items.append({**item, "unit_price": unit_price, "tax_amount": tax_amount})

    recognized_subtotal = sum((item["unit_price"] * item["quantity"] for item in items), Decimal("0"))
    recognized_tax = sum((item["tax_amount"] for item in items), Decimal("0"))
    recognized_total = (recognized_subtotal + recognized_tax).quantize(Decimal("0.01"))

    if promo_applied:
        checkout_subtotal = (original_subtotal * (Decimal("1") - PROMO_DISCOUNT_RATE)).quantize(Decimal("0.01"))
        checkout_tax = (checkout_subtotal * tax_rate).quantize(Decimal("0.01"))
        checkout_total = checkout_subtotal + checkout_tax
    else:
        checkout_total = recognized_total

    # --- Lifecycle: cancelled is a distinct terminal state (never shipped),
    # not a returned-then-reversed order. Only orders whose ship_date would
    # fall on/before AS_OF_DATE have actually shipped as of the snapshot;
    # orders placed in the last few days of the window are legitimately
    # still "pending" -- this is the one case where a real, non-vacuous
    # "unshipped" population exists for the recognition policy to exclude.
    cancelled = rng.random() < CANCEL_RATE
    status = "cancelled"
    ship_date = None
    returned = False
    refund_date = None
    refund_amount = None

    if not cancelled:
        candidate_ship_date = order_date + timedelta(days=rng.randint(*SHIP_LAG_DAYS))
        if candidate_ship_date > AS_OF_DATE:
            status = "pending"
        else:
            ship_date = candidate_ship_date
            status = "shipped"
            returned = rng.random() < RETURN_RATE
            if returned:
                status = "returned"
                refund_date = ship_date + timedelta(days=rng.randint(*RETURN_LAG_DAYS))
                refund_amount = recognized_total
            elif rng.random() < DELIVERED_RATE:
                status = "delivered"

    has_web_purchase = rng.random() < WEB_MATCH_RATE

    # Second amount-divergence mechanism (see SHIP_ESTIMATE_RATE above).
    # Deliberately the last rng draw in this function so it never resequences
    # any other field of this same order -- it only adds a new fact on top.
    ship_estimate_applied = rng.random() < SHIP_ESTIMATE_RATE
    ship_estimate_amount = SHIP_ESTIMATE_FLAT[region] if ship_estimate_applied else Decimal("0.00")
    if ship_estimate_applied:
        checkout_total = (checkout_total + ship_estimate_amount).quantize(Decimal("0.01"))

    return {
        "region": region,
        "order_number": order_number,
        "order_id": order_id_for(region, order_number),
        "customer_index": customer_index,
        "order_date": order_date,
        "currency": currency,
        "shipping_country": shipping_country,
        "tax_rate": tax_rate,
        "items": items,
        "recognized_total": recognized_total,
        "checkout_total": checkout_total,
        "promo_applied": promo_applied,
        "promo_validated": promo_validated,
        "ship_estimate_applied": ship_estimate_applied,
        "ship_estimate_amount": ship_estimate_amount,
        "status": status,  # pending | shipped | delivered | returned | cancelled
        "ship_date": ship_date,
        "refund_date": refund_date,
        "refund_amount": refund_amount,
        "has_web_purchase": has_web_purchase,
    }


def iter_orders(orders_per_region: int = DEFAULT_ORDERS_PER_REGION):
    """Yield every real order in the pool, region-major, order_number ascending."""
    for region in REGIONS:
        for order_number in range(1, orders_per_region + 1):
            yield build_order(region, order_number)


if __name__ == "__main__":
    for order in iter_orders(orders_per_region=5):
        print(order["order_id"], order["status"], order["recognized_total"], order["checkout_total"])
