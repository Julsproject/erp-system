"""FastAPI application: auth + module routers.

Phase 1 - Step 1: login/logout + dashboard.
Phase 1 - Step 2: Inventory (Products) module.
"""
from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import (
    activity, audit, backup, banking, credits, customers, dashboard, deliveries, expenses, models,
    notifications, pdc, pos, products, purchases, quotations, reports, sales, suppliers, users,
)
from . import settings as settings_module   # app/settings.py — the Settings UI router
from . import settings_store
from .auth import verify_password
from .config import settings
from .database import get_db
from .templating import templates

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def load_business_name() -> None:
    """Adopt the DB-saved business name as the app title so login, page titles
    and receipts all reflect a name changed from the Settings screen. Falls
    back to the .env APP_NAME when nothing has been saved yet."""
    app.title = settings_store.business_name()


app.include_router(products.router)
app.include_router(pos.router)
app.include_router(customers.router)
app.include_router(sales.router)
app.include_router(quotations.router)
app.include_router(credits.router)
app.include_router(suppliers.router)
app.include_router(purchases.router)
app.include_router(pdc.router)
app.include_router(expenses.router)
app.include_router(deliveries.router)
app.include_router(reports.router)
app.include_router(banking.router)
app.include_router(notifications.router)
app.include_router(backup.router)
app.include_router(activity.router)
app.include_router(audit.router)
app.include_router(users.router)
app.include_router(settings_module.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "app_name": request.app.title, "error": None},
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter_by(username=username, is_active=True).first()
    if not user or not verify_password(password, user.password_hash):
        audit.record(
            db, request=request, action="login_failed", entity_type="auth",
            entity_label=username, summary=f"Failed sign-in attempt for “{username}”",
        )
        db.commit()
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "app_name": request.app.title,
                "error": "Invalid username or password.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session["user_id"] = user.id
    audit.record(
        db, user=user, request=request, action="login", entity_type="auth",
        entity_label=user.username, summary=f"{user.username} signed in",
    )
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if user_id:
        user = db.query(models.User).filter_by(id=user_id).first()
        if user:
            audit.record(
                db, user=user, request=request, action="logout", entity_type="auth",
                entity_label=user.username, summary=f"{user.username} signed out",
            )
            db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)


# "/" is served by the dashboard router.
