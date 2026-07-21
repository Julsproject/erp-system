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
    backup, credits, customers, dashboard, models, pos, products,
    purchases, sales, shifts, suppliers, users,
)
from .auth import verify_password
from .config import settings
from .database import SessionLocal, get_db
from .templating import templates

app = FastAPI(title=settings.app_name)

# Paths a cashier can still reach without an open cash drawer.
DRAWER_EXEMPT = ("/login", "/logout", "/static", "/health", "/shift", "/favicon.ico")


# NOTE ON ORDER: this is registered BEFORE SessionMiddleware on purpose.
# Starlette runs the most recently added middleware outermost, so adding the
# session layer afterwards means request.session is already populated here.
@app.middleware("http")
async def require_open_drawer(request: Request, call_next):
    """Send cashiers to the drawer form until they declare their starting cash."""
    path = request.url.path
    if not path.startswith(DRAWER_EXEMPT):
        user_id = request.session.get("user_id")
        if user_id:
            db = SessionLocal()
            try:
                user = db.query(models.User).filter_by(id=user_id, is_active=True).first()
                if user and (user.role or "").lower() != "admin":
                    has_open = (
                        db.query(models.CashShift)
                        .filter(models.CashShift.user_id == user.id, models.CashShift.closed_at.is_(None))
                        .first()
                    )
                    if not has_open:
                        return RedirectResponse("/shift/open", status_code=status.HTTP_302_FOUND)
            finally:
                db.close()
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(products.router)
app.include_router(pos.router)
app.include_router(customers.router)
app.include_router(sales.router)
app.include_router(credits.router)
app.include_router(suppliers.router)
app.include_router(purchases.router)
app.include_router(backup.router)
app.include_router(shifts.router)
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
