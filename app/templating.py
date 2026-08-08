"""Shared Jinja2 templates instance and view helpers."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


def _to_decimal(value) -> Decimal:
    """Decimal(some_float) expands the float's exact binary value (e.g.
    Decimal(8.6) -> 8.5999999999999996447286321199499070644378662109375)
    instead of the clean "8.6" you'd expect — routing through str() first
    avoids that for any float that came from a JSON round-trip (e.g. a
    dict built for a JS fetch response) before it reaches a template."""
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def peso(value) -> str:
    """Format a number as Philippine peso with thousands separators."""
    try:
        return "₱{:,.2f}".format(_to_decimal(value))
    except (InvalidOperation, TypeError, ValueError):
        return "₱0.00"


def qty(value) -> str:
    """Format a quantity, trimming trailing zeros (e.g. 12.000 -> 12, 5.500 -> 5.5)."""
    try:
        d = _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    d = d.normalize()
    s = format(d, "f")
    return s


def whole_qty(value) -> str:
    """Whole-number view of a quantity, dropping any fractional remainder
    (e.g. 19.7 sacks -> "19"). Used where the fractional part is already
    broken out elsewhere as the open-container amount, so the shop only
    has to read one number for how many full units are on the shelf.
    """
    try:
        d = _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    return str(d.to_integral_value(rounding=ROUND_FLOOR))


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


def check_month_end_rollover() -> str:
    """Fires the throttled month-end rollover check on page load — see
    products.maybe_run_month_end_rollover. Returns '' (nothing to render);
    called for its side effect only. Imported lazily; never raises."""
    try:
        from .products import maybe_run_month_end_rollover
        maybe_run_month_end_rollover()
    except Exception:
        pass
    return ""


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
templates.env.filters["whole_qty"] = whole_qty
templates.env.globals["price_alert_count"] = price_alert_count
templates.env.globals["pdc_due_count"] = pdc_due_count
templates.env.globals["check_month_end_rollover"] = check_month_end_rollover
templates.env.globals["asset_version"] = asset_version
templates.env.globals["notif_unread_count"] = notif_unread_count
templates.env.globals["business_info"] = business_info
templates.env.globals["min_margin_pct"] = min_margin_pct
