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


def price_alert_count() -> int:
    """How many active products are selling at or below cost (or have no price).

    Exposed to every template so the sidebar can show a live warning badge
    no matter which page the user is on.
    """
    from . import models
    from .database import SessionLocal

    db = SessionLocal()
    try:
        return (
            db.query(models.Product)
            .filter(
                models.Product.is_active.is_(True),
                models.Product.cost_price > 0,
                models.Product.selling_price <= models.Product.cost_price,
            )
            .count()
        )
    except Exception:  # never let a badge break a page render
        return 0
    finally:
        db.close()


templates.env.filters["peso"] = peso
templates.env.filters["qty"] = qty
templates.env.globals["price_alert_count"] = price_alert_count
