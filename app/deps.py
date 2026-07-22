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
    return user is not None and (user.role or "").lower() == "admin"
