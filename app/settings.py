"""Settings UI (admin only): edit the display settings that used to require
editing `.env` and rebuilding — the business name (shown on the login page,
every page title and printed receipts) and the optional receipt address /
contact / TIN / footer. Also a convenience "change my password" form.

Infrastructure config (DATABASE_URL, SECRET_KEY) stays in `.env`; it is not
editable here on purpose.
"""
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import audit, settings_store
from .auth import hash_password, verify_password
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

# key -> (label, max length). Order here is the order shown on the page.
FIELDS = [
    ("business_name", "Business name", 100),
    ("receipt_address", "Receipt address", 255),
    ("receipt_contact", "Receipt contact (phone / email)", 120),
    ("receipt_tin", "Receipt TIN", 30),
    ("receipt_footer", "Receipt footer message", 255),
]


def _render(request, db, user, saved="", error="", pw_error="", pw_saved=False):
    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "values": settings_store.get_all(db), "fields": FIELDS,
            "saved": saved, "error": error, "pw_error": pw_error, "pw_saved": pw_saved,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    return _render(request, db, user)


@router.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    form = await request.form()
    if not (form.get("business_name") or "").strip():
        return _render(request, db, user, error="Business name can't be empty.")

    before = settings_store.get_all(db)
    for key, _label, maxlen in FIELDS:
        value = (form.get(key) or "").strip()[:maxlen]
        settings_store.set_setting(db, key, value)
    db.flush()
    after = settings_store.get_all(db)
    changes = audit.diff(before, after)
    if changes:
        audit.record(
            db, user=user, request=request, action="settings_change", entity_type="setting",
            entity_label="Business settings", summary="Updated business/receipt settings", changes=changes,
        )
    db.commit()

    # The business name drives app.title, which every page and receipt reads —
    # update it in place so the change shows immediately, no restart needed.
    request.app.title = settings_store.get_setting(db, "business_name")
    return _render(request, db, user, saved="Settings saved.")


@router.post("/settings/password")
async def change_password(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    form = await request.form()
    current = form.get("current_password") or ""
    new = form.get("new_password") or ""
    confirm = form.get("confirm_password") or ""

    if not verify_password(current, user.password_hash):
        return _render(request, db, user, pw_error="Current password is incorrect.")
    if len(new) < 4:
        return _render(request, db, user, pw_error="New password must be at least 4 characters.")
    if new != confirm:
        return _render(request, db, user, pw_error="New password and confirmation don't match.")

    user.password_hash = hash_password(new)
    audit.record(
        db, user=user, request=request, action="password_change", entity_type="user",
        entity_id=user.id, entity_label=user.username, summary=f"{user.username} changed their own password",
    )
    db.commit()
    return _render(request, db, user, pw_saved=True)
