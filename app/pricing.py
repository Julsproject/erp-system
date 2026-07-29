"""The three selling prices every product carries.

  fixed   typed in directly — the default price POS uses.
  markup  a % ON TOP OF COST       ->  price = cost * (1 + pct/100)
  margin  a % OF THE SELLING PRICE ->  price = cost / (1 - pct/100)

Markup and margin are easy to confuse and are NOT interchangeable. On a cost of
300: a 30% markup gives 390, of which 90 is profit — that is only a 23.1%
margin. A 30% margin gives 428.57, of which 128.57 is profit — a true 30%.
Because the app's Gross Profit reports measure profit as a share of revenue
(margin), a markup-priced sale always reports a lower % than the number typed.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTS = Decimal("0.01")
# A 100% margin implies price = cost/0 -> undefined, so cap just below it.
MAX_MARGIN = Decimal("99.99")


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def markup_price(cost, pct) -> Decimal:
    """cost + a percentage of the cost. A negative markup (selling below
    cost, typed directly into the price cell and solved back to a %) is a
    real, representable state — it only bottoms out at price 0."""
    cost, pct = _dec(cost), _dec(pct)
    if cost <= 0:
        return Decimal("0")
    price = cost * (Decimal("1") + pct / Decimal("100"))
    return _money(max(price, Decimal("0")))


def margin_price(cost, pct) -> Decimal:
    """A price where `pct` percent of it is profit. Negative pct (selling
    below cost) works the same as positive — it just can't reach exactly 0
    at any finite pct (the curve is asymptotic), so "make this free" should
    go through the Fixed price row instead."""
    cost, pct = _dec(cost), _dec(pct)
    if cost <= 0:
        return Decimal("0")
    if pct > MAX_MARGIN:
        pct = MAX_MARGIN
    return _money(max(cost / (Decimal("1") - pct / Decimal("100")), Decimal("0")))


def profit(price, cost) -> Decimal:
    """Straight profit — works fine at any price, including 0 or negative."""
    return _money(_dec(price) - _dec(cost))


def true_margin(price, cost) -> Decimal:
    """What share of `price` is actually profit — the number the Gross Profit
    reports will show, whichever way the price was set.

    An unpriced/free item (price <= 0) isn't "0% margin" — margin/price is
    undefined at price=0, but the shop still spent `cost` and got nothing
    back, i.e. a full loss on that cost: -100%. (A margin-against-cost read
    of -cost/cost gives the same -100%, so there's no ambiguity here.) An
    item with no cost either is truly neutral: nothing spent, nothing lost.
    """
    price, cost = _dec(price), _dec(cost)
    if price <= 0:
        return Decimal("-100") if cost > 0 else Decimal("0")
    return _money((price - cost) / price * Decimal("100"))


def apply_to(product, cost, markup_pct, margin_pct) -> None:
    """Store both percentages on a product and refresh their derived prices."""
    product.markup_pct = _dec(markup_pct)
    product.margin_pct = _dec(margin_pct)
    product.markup_price = markup_price(cost, product.markup_pct)
    product.margin_price = margin_price(cost, product.margin_pct)


def needs_review_expr(min_margin):
    """SQL version of needs_review's three triggers, for filtering/counting
    at the database level — the one place that definition lives, so the
    sidebar badge, the dashboard widget, and the Selling Price tab's own
    count and filter never drift apart."""
    from sqlalchemy import and_, or_
    from . import models

    price = models.Product.selling_price
    cost = models.Product.cost_price
    conditions = [price <= 0, and_(cost > 0, price <= cost)]
    if min_margin is not None:
        margin_expr = (price - cost) / price * 100
        conditions.append(and_(cost > 0, price > cost, margin_expr < min_margin))
    return or_(*conditions)


def needs_review(price, cost, min_margin_pct=None):
    """Whether a product's pricing needs attention, and why. Three triggers:
      1. No selling price set (price <= 0).
      2. Selling at or below cost (zero or negative profit).
      3. Profitable, but the true margin falls under the shop's target
         (only checked when a target is configured).
    Returns (bool, reason | None).
    """
    price, cost = _dec(price), _dec(cost)
    if price <= 0:
        return True, "No selling price set"
    if cost > 0 and price <= cost:
        return True, "Selling at or below cost"
    if min_margin_pct is not None and cost > 0 and price > cost:
        tm = true_margin(price, cost)
        if tm < _dec(min_margin_pct):
            return True, f"Margin below {min_margin_pct:g}% target"
    return False, None
