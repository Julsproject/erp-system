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
    """cost + a percentage of the cost."""
    cost, pct = _dec(cost), _dec(pct)
    if cost <= 0 or pct < 0:
        return Decimal("0")
    return _money(cost * (Decimal("1") + pct / Decimal("100")))


def margin_price(cost, pct) -> Decimal:
    """A price where `pct` percent of it is profit."""
    cost, pct = _dec(cost), _dec(pct)
    if cost <= 0 or pct < 0:
        return Decimal("0")
    if pct > MAX_MARGIN:
        pct = MAX_MARGIN
    return _money(cost / (Decimal("1") - pct / Decimal("100")))


def true_margin(price, cost) -> Decimal:
    """What share of `price` is actually profit — the number the Gross Profit
    reports will show, whichever way the price was set."""
    price, cost = _dec(price), _dec(cost)
    if price <= 0:
        return Decimal("0")
    return _money((price - cost) / price * Decimal("100"))


def apply_to(product, cost, markup_pct, margin_pct) -> None:
    """Store both percentages on a product and refresh their derived prices."""
    product.markup_pct = _dec(markup_pct)
    product.margin_pct = _dec(margin_pct)
    product.markup_price = markup_price(cost, product.markup_pct)
    product.margin_price = margin_price(cost, product.margin_pct)
