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
    activity, backup, banking, credits, customers, dashboard, deliveries, expenses, models, pdc, pos, products,
    purchases, quotations, reports, sales, suppliers, users,
)
from .auth import verify_password
from .config import settings
from .database import get_db
from .templating import templates

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

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
app.include_router(backup.router)
app.include_router(activity.router)
app.include_router(users.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "app_name": settings.app_name, "error": None},
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
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "error": "Invalid username or password.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)


# "/" is served by the dashboard router.
