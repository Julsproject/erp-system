"""Shared Jinja2 templates instance and view helpers."""
from decimal import Decimal, InvalidOperation

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


def peso(value) -> str:
    """Format a number as Philippine peso with thousands separators."""
    try:
        return "₱{:,.2f}".format(Decimal(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return "₱0.00"


def qty(value) -> str:
    """Format a quantity, trimming trailing zeros (e.g. 12.000 -> 12, 5.500 -> 5.5)."""
    try:
        d = Decimal(value or 0)
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    d = d.normalize()
    s = format(d, "f")
    return s


templates.env.filters["peso"] = peso
templates.env.filters["qty"] = qty
