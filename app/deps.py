"""Shared FastAPI dependencies."""
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from . import models
from .database import get_db


def get_current_user(request: Request, db: Session = Depends(get_db)):
    """Return the logged-in User, or None. Routes decide how to handle None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(models.User).filter_by(id=user_id, is_active=True).first()


# Three roles, three gates. Read them as concentric rings — every admin is
# also staff, every staff member is also floor staff:
#
#   is_floor_staff  cashier + manager + admin — counter work. Ringing up a
#                   sale, receiving a delivery, counting stock, looking up
#                   what's in stock and what it sells for.
#   is_staff        manager + admin — running the store. Sales history and
#                   voids, suppliers and payables, expenses, banking,
#                   cheques, the management reports, editing the catalogue.
#   is_admin        admin only — owning the books and the system. The
#                   accounting ledger, settings, encoders, the audit trail,
#                   user accounts, backups, stock cards.
#
# The line between staff and admin is deliberate: a manager runs the shop
# day to day, but the ledger, the audit trail and who-can-log-in belong to
# the owner. A manager who can post journal entries isn't a manager.
def is_admin(user) -> bool:
    """Admin only — the books and the system itself."""
    return user is not None and (user.role or "").lower() == "admin"


def is_staff(user) -> bool:
    """Admin or Manager — everything involved in running the store."""
    return user is not None and (user.role or "").lower() in ("admin", "manager")


def is_floor_staff(user) -> bool:
    """Anyone who works the counter — cashier, manager or admin.

    Spelled out as a role list rather than "is anyone logged in" so that
    adding a future read-only or delivery-driver role doesn't silently
    hand it the till and the stock count. `display` is deliberately absent.
    """
    return user is not None and (user.role or "").lower() in ("admin", "manager", "cashier")


def is_display(user) -> bool:
    """The store's wall screen — not a person.

    A `display` account exists so a tablet or TV can be signed in once and
    left running. It sits outside the three rings above: it fails every one
    of them, so it can't reach any normal page, and `main.confine_display`
    pins it to /display. That containment is the point — if someone picks
    up the tablet, there is nothing else to browse to.
    """
    return user is not None and (user.role or "").lower() == "display"


def safe_back_url(raw: str, fallback: str) -> str:
    """A caller-supplied "return here afterwards" URL, made safe to redirect to.

    Used so editing a row from a filtered list lands back on that same
    filtered list instead of an unfiltered one. Only same-site paths are
    accepted: it must start with a single "/", which rejects both
    "//evil.com" (protocol-relative) and "https://evil.com" — otherwise a
    crafted link could turn our own redirect into a way off the site.
    """
    raw = (raw or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return fallback
