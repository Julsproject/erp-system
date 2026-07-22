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


def pdc_due_count() -> int:
    """How many pending post-dated cheques are due within 3 days or overdue.

    Exposed to every template so the sidebar can show a live badge no matter
    which page the user is on, same idea as price_alert_count.
    """
    from datetime import date, timedelta

    from . import models
    from .database import SessionLocal

    db = SessionLocal()
    try:
        horizon = date.today() + timedelta(days=3)
        return (
            db.query(models.PostDatedCheque)
            .filter(models.PostDatedCheque.status == "pending", models.PostDatedCheque.cheque_date <= horizon)
            .count()
        )
    except Exception:  # never let a badge break a page render
        return 0
    finally:
        db.close()


def asset_version() -> str:
    """Cache-busting token for static assets.

    Uses the stylesheet's last-modified time, so browsers refetch the CSS the
    moment it actually changes — and keep caching it the rest of the time.
    """
    import os

    try:
        return str(int(os.path.getmtime("app/static/css/styles.css")))
    except OSError:
        return "1"


templates.env.filters["peso"] = peso
templates.env.filters["qty"] = qty
templates.env.globals["price_alert_count"] = price_alert_count
templates.env.globals["pdc_due_count"] = pdc_due_count
templates.env.globals["asset_version"] = asset_version
