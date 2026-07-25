"""Read/write helpers for the DB-backed AppSetting key-value store.

These are the user-facing display settings the owner can edit from the in-app
Settings screen (business name shown on the login page and receipts, plus the
optional receipt address/contact/TIN/footer). Infrastructure config — database
URL, secret key — stays in `.env`; it is not exposed here.

The business name also drives `app.title`, which every page and receipt reads,
so `main.py` loads it into `app.title` on startup and the Settings save updates
`app.title` in place — no rebuild or restart needed for a name change to show.
"""
from . import models
from .config import settings
from .database import SessionLocal

# key -> default. The business name falls back to the .env APP_NAME so a fresh
# install (no row yet) still shows the configured name.
DEFAULTS = {
    "business_name": settings.app_name,
    "receipt_address": "",
    "receipt_contact": "",
    "receipt_tin": "",
    "receipt_footer": "Thank you for your purchase!",
    # Shop-wide minimum acceptable margin %. Blank = disabled — no
    # below-target warnings until the owner opts in by setting a number.
    # Separate from the existing below-cost/no-margin alerts, which always
    # fire regardless of this (losing money is never OK; this is a softer,
    # "you're profitable but under your own standard" nudge).
    "min_margin_pct": "",
}


def get_all(db) -> dict:
    """Every setting as a dict, defaults filled in for any key not yet saved."""
    rows = {s.key: s.value for s in db.query(models.AppSetting).all()}
    return {k: (rows.get(k) if rows.get(k) is not None else default) for k, default in DEFAULTS.items()}


def get_setting(db, key: str, default: str = "") -> str:
    row = db.get(models.AppSetting, key)
    if row is not None and row.value is not None:
        return row.value
    return DEFAULTS.get(key, default)


def set_setting(db, key: str, value: str) -> None:
    row = db.get(models.AppSetting, key)
    if row is None:
        db.add(models.AppSetting(key=key, value=value))
    else:
        row.value = value


def business_name() -> str:
    """The saved business name, or the .env default. Used at startup to seed
    app.title and as a safe standalone lookup (opens its own session)."""
    db = SessionLocal()
    try:
        return get_setting(db, "business_name", settings.app_name)
    except Exception:
        return settings.app_name
    finally:
        db.close()


def business_info() -> dict:
    """All display settings, for templates (registered as a Jinja global).
    Never raises — a settings lookup must not break a page or receipt render.
    """
    db = SessionLocal()
    try:
        return get_all(db)
    except Exception:
        return dict(DEFAULTS)
    finally:
        db.close()


def min_margin_pct():
    """The shop-wide minimum-margin target, or None when not set (disabled).
    Standalone lookup (opens its own session) for the Notifications sweep."""
    db = SessionLocal()
    try:
        raw = get_setting(db, "min_margin_pct", "")
        return float(raw) if raw not in (None, "") else None
    except Exception:
        return None
    finally:
        db.close()
