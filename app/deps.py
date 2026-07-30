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


def is_admin(user) -> bool:
    """True admin only — the two areas that stay admin-exclusive (Users,
    Backup) check this directly instead of is_staff."""
    return user is not None and (user.role or "").lower() == "admin"


def is_staff(user) -> bool:
    """Admin or Manager — the general "not just a cashier" gate used by
    every admin-only route except Users and Backup, which stay is_admin."""
    return user is not None and (user.role or "").lower() in ("admin", "manager")
