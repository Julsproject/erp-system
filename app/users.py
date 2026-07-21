"""User accounts (admin only) — needed so the owner can create cashier logins."""
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .auth import hash_password
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

ROLES = [("cashier", "Cashier"), ("admin", "Admin")]


def _is_admin(user) -> bool:
    return user is not None and (user.role or "").lower() == "admin"


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/", status_code=302)
    users = db.query(models.User).order_by(models.User.username).all()
    return templates.TemplateResponse(
        "users/list.html",
        {"request": request, "app_name": request.app.title, "user": user, "users": users},
    )


def _render_form(request, user, target=None, error=None):
    return templates.TemplateResponse(
        "users/form.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "target": target, "roles": ROLES, "error": error},
    )


@router.get("/users/new", response_class=HTMLResponse)
def new_user(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/", status_code=302)
    return _render_form(request, user)


@router.get("/users/{user_id:int}/edit", response_class=HTMLResponse)
def edit_user(user_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/", status_code=302)
    target = db.get(models.User, user_id)
    if not target:
        return RedirectResponse("/users", status_code=302)
    return _render_form(request, user, target=target)


@router.post("/users")
async def create_user(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    if not username:
        return _render_form(request, user, error="Username is required.")
    if len(password) < 4:
        return _render_form(request, user, error="Password must be at least 4 characters.")
    if db.query(models.User).filter(func.lower(models.User.username) == username).first():
        return _render_form(request, user, error=f"Username '{username}' is already taken.")

    db.add(models.User(
        username=username,
        full_name=(form.get("full_name") or "").strip() or None,
        password_hash=hash_password(password),
        role=(form.get("role") or "cashier").strip().lower(),
        is_active=(form.get("status") or "active") == "active",
    ))
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id:int}")
async def update_user(user_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/", status_code=302)
    target = db.get(models.User, user_id)
    if not target:
        return RedirectResponse("/users", status_code=302)

    form = await request.form()
    target.full_name = (form.get("full_name") or "").strip() or None
    new_role = (form.get("role") or "cashier").strip().lower()
    active = (form.get("status") or "active") == "active"

    # Don't let the last admin lock everyone out of user management.
    if target.role == "admin" and (new_role != "admin" or not active):
        admins_left = (
            db.query(models.User)
            .filter(models.User.role == "admin", models.User.is_active.is_(True), models.User.id != target.id)
            .count()
        )
        if admins_left == 0:
            return _render_form(request, user, target=target,
                                error="This is the only active admin — keep it an active Admin.")
    target.role = new_role
    target.is_active = active

    password = form.get("password") or ""
    if password:
        if len(password) < 4:
            return _render_form(request, user, target=target, error="Password must be at least 4 characters.")
        target.password_hash = hash_password(password)

    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_302_FOUND)
