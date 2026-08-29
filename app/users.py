"""User accounts (admin only) — needed so the owner can create cashier logins."""
from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, models
from .auth import hash_password
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

ROLES = [
    ("cashier", "Cashier"),
    ("manager", "Manager"),
    ("admin", "Admin"),
    # Not a person — the account a wall screen signs in as. Confined to
    # /display and blocked from everything else (see main.confine_display).
    ("display", "Store Display (screen only)"),
]


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
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
    if not is_admin(user):
        return RedirectResponse("/", status_code=302)
    return _render_form(request, user)


@router.get("/users/{user_id:int}/edit", response_class=HTMLResponse)
def edit_user(user_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/", status_code=302)
    target = db.get(models.User, user_id)
    if not target:
        return RedirectResponse("/users", status_code=302)
    return _render_form(request, user, target=target)


@router.post("/users")
def create_user(
    request: Request,
    username: str = Form(""), password: str = Form(""), full_name: str = Form(""),
    role: str = Form("cashier"), status_field: str = Form("active", alias="status"),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/", status_code=302)

    username = (username or "").strip().lower()
    password = password or ""
    if not username:
        return _render_form(request, user, error="Username is required.")
    if len(password) < 4:
        return _render_form(request, user, error="Password must be at least 4 characters.")
    if db.query(models.User).filter(func.lower(models.User.username) == username).first():
        return _render_form(request, user, error=f"Username '{username}' is already taken.")

    role = (role or "cashier").strip().lower()
    new_user = models.User(
        username=username,
        full_name=(full_name or "").strip() or None,
        password_hash=hash_password(password),
        role=role,
        is_active=(status_field or "active") == "active",
    )
    db.add(new_user)
    db.flush()
    audit.record(
        db, user=user, request=request, action="create", entity_type="user",
        entity_id=new_user.id, entity_label=username,
        summary=f"Created {role} account “{username}”",
    )
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id:int}")
def update_user(
    user_id: int, request: Request,
    full_name: str = Form(""), role: str = Form("cashier"), status_field: str = Form("active", alias="status"),
    password: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/", status_code=302)
    target = db.get(models.User, user_id)
    if not target:
        return RedirectResponse("/users", status_code=302)

    before = {"full_name": target.full_name, "role": target.role,
              "status": "active" if target.is_active else "disabled"}
    target.full_name = (full_name or "").strip() or None
    new_role = (role or "cashier").strip().lower()
    active = (status_field or "active") == "active"

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

    pw_changed = False
    password = password or ""
    if password:
        if len(password) < 4:
            return _render_form(request, user, target=target, error="Password must be at least 4 characters.")
        target.password_hash = hash_password(password)
        pw_changed = True

    after = {"full_name": target.full_name, "role": target.role,
             "status": "active" if target.is_active else "disabled"}
    changes = audit.diff(before, after)
    if pw_changed:
        changes["password"] = ["••••", "changed"]
    if changes:
        audit.record(
            db, user=user, request=request, action="update", entity_type="user",
            entity_id=target.id, entity_label=target.username,
            summary=f"Edited account “{target.username}”"
                    + (" (password reset)" if pw_changed else ""),
            changes=changes,
        )
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_302_FOUND)
