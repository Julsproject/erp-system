"""Customer accounts (name, TIN, address) and receivable helper."""
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()


def get_or_create_customer(db: Session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    existing = (
        db.query(models.Customer)
        .filter(func.lower(models.Customer.name) == name.lower())
        .first()
    )
    if existing:
        return existing
    cust = models.Customer(name=name)
    db.add(cust)
    db.flush()
    return cust


def _outstanding_map(db: Session):
    """customer_id -> outstanding utang (original receivable minus settlements collected)."""
    receivable_rows = (
        db.query(models.Sale.customer_id, func.coalesce(func.sum(models.Sale.receivable_amount), 0))
        .filter(models.Sale.customer_id.isnot(None))
        .group_by(models.Sale.customer_id)
        .all()
    )
    # settlements collected per customer (join settlement -> sale -> customer)
    settled_rows = (
        db.query(models.Sale.customer_id, func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .join(models.ReceivableSettlement, models.ReceivableSettlement.sale_id == models.Sale.id)
        .filter(models.Sale.customer_id.isnot(None))
        .group_by(models.Sale.customer_id)
        .all()
    )
    settled = {cid: amt for cid, amt in settled_rows}
    return {cid: (bal - settled.get(cid, 0)) for cid, bal in receivable_rows}


@router.get("/customers", response_class=HTMLResponse)
def list_customers(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    q = (q or "").strip()
    query = db.query(models.Customer).filter(models.Customer.is_active.is_(True))
    if q:
        query = query.filter(models.Customer.name.ilike(f"%{q}%"))
    customers = query.order_by(models.Customer.name).all()
    outstanding = _outstanding_map(db)
    return templates.TemplateResponse(
        "customers/list.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "customers": customers,
            "outstanding": outstanding,
            "q": q,
        },
    )


@router.get("/customers/new", response_class=HTMLResponse)
def new_customer(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "customers/form.html",
        {"request": request, "app_name": request.app.title, "user": user, "customer": None, "error": None},
    )


@router.get("/customers/{customer_id:int}/edit", response_class=HTMLResponse)
def edit_customer(customer_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.get(models.Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers", status_code=302)
    return templates.TemplateResponse(
        "customers/form.html",
        {"request": request, "app_name": request.app.title, "user": user, "customer": customer, "error": None},
    )


@router.post("/customers")
async def create_customer(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return templates.TemplateResponse(
            "customers/form.html",
            {"request": request, "app_name": request.app.title, "user": user, "customer": None, "error": "Customer name is required."},
        )
    cust = models.Customer(name=name, tin=(form.get("tin") or "").strip() or None, address=(form.get("address") or "").strip() or None)
    db.add(cust)
    db.commit()
    return RedirectResponse("/customers", status_code=status.HTTP_302_FOUND)


@router.post("/customers/{customer_id:int}")
async def update_customer(customer_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.get(models.Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers", status_code=302)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return templates.TemplateResponse(
            "customers/form.html",
            {"request": request, "app_name": request.app.title, "user": user, "customer": customer, "error": "Customer name is required."},
        )
    customer.name = name
    customer.tin = (form.get("tin") or "").strip() or None
    customer.address = (form.get("address") or "").strip() or None
    db.commit()
    return RedirectResponse("/customers", status_code=status.HTTP_302_FOUND)
