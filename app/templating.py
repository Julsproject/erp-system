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
    """How many active products need a pricing review — same three triggers
    as the Selling Price tab's own count (see pricing.needs_review_expr),
    so the sidebar badge, dashboard widget, and top banner never disagree
    with what that tab actually flags.

    Exposed to every template so the sidebar can show a live warning badge
    no matter which page the user is on.
    """
    from . import models, pricing, settings_store
    from .database import SessionLocal

    db = SessionLocal()
    try:
        min_margin = settings_store.min_margin_pct()
        return (
            db.query(models.Product)
            .filter(models.Product.is_active.is_(True), pricing.needs_review_expr(min_margin))
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


def notif_unread_count() -> int:
    """Unread, unresolved Notifications Center count for the sidebar badge.
    Imported lazily to avoid a circular import (notifications imports templating).
    """
    try:
        from .notifications import unread_count
        return unread_count()
    except Exception:  # never let a badge break a page render
        return 0


def business_info() -> dict:
    """DB-saved display settings (business name + receipt details) for templates.
    Imported lazily to keep templating import-light; never raises."""
    try:
        from .settings_store import business_info as _bi
        return _bi()
    except Exception:
        return {}


def min_margin_pct():
    """The shop-wide minimum-margin target (float), or None when disabled.
    Imported lazily; never raises."""
    try:
        from .settings_store import min_margin_pct as _mmp
        return _mmp()
    except Exception:
        return None


templates.env.filters["peso"] = peso
templates.env.filters["qty"] = qty
templates.env.globals["price_alert_count"] = price_alert_count
templates.env.globals["pdc_due_count"] = pdc_due_count
templates.env.globals["asset_version"] = asset_version
templates.env.globals["notif_unread_count"] = notif_unread_count
templates.env.globals["business_info"] = business_info
templates.env.globals["min_margin_pct"] = min_margin_pct
