"""Customer accounts (name, TIN, address) and receivable helper."""
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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


@router.get("/customers/search")
def search_customers(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """JSON autocomplete for the POS customer field."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    q = (q or "").strip()
    query = db.query(models.Customer).filter(models.Customer.is_active.is_(True))
    if q:
        query = query.filter(models.Customer.name.ilike(f"%{q}%"))
    customers = query.order_by(models.Customer.name).limit(20).all()
    return {
        "customers": [
            {"id": c.id, "name": c.name, "tin": c.tin or "", "address": c.address or ""}
            for c in customers
        ]
    }


@router.get("/customers", response_class=HTMLResponse)
def list_customers(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    q = (q or "").strip()
    query = db.query(models.Customer).filter(models.Customer.is_active.is_(True))
    if q:
        query = query.filter(models.Customer.name.ilike(f"%{q}%"))
    customers = query.order_by(models.Customer.name).all()
    return templates.TemplateResponse(
        "customers/list.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "customers": customers,
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


@router.get("/customers/{customer_id:int}/history", response_class=HTMLResponse)
def customer_history(customer_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.get(models.Customer, customer_id)
    if not customer:
        return RedirectResponse("/customers", status_code=302)

    sales = (
        db.query(models.Sale)
        .filter(models.Sale.customer_id == customer_id)
        .order_by(models.Sale.id.desc())
        .all()
    )

    # settlements collected per sale, to show each sale's paid/credit status
    sale_ids = [s.id for s in sales]
    settled = {}
    if sale_ids:
        rows = (
            db.query(models.ReceivableSettlement.sale_id, func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
            .filter(models.ReceivableSettlement.sale_id.in_(sale_ids))
            .group_by(models.ReceivableSettlement.sale_id)
            .all()
        )
        settled = {sid: Decimal(amt) for sid, amt in rows}

    rows = []
    total_spent = Decimal("0")
    total_out = Decimal("0")
    for s in sales:
        outstanding = (s.receivable_amount or Decimal("0")) - settled.get(s.id, Decimal("0"))
        rows.append({"sale": s, "outstanding": outstanding})
        if s.txn_type == "sale":
            total_spent += (s.total or Decimal("0"))
        if outstanding > 0:
            total_out += outstanding

    return templates.TemplateResponse(
        "customers/history.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "customer": customer, "rows": rows, "count": len(rows),
            "total_spent": total_spent, "total_out": total_out,
        },
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
    cust = models.Customer(
        name=name,
        tin=(form.get("tin") or "").strip() or None,
        address=(form.get("address") or "").strip() or None,
        credit_days=int(form.get("credit_days") or 15),
    )
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
    try:
        customer.credit_days = int(form.get("credit_days") or 15)
    except (TypeError, ValueError):
        customer.credit_days = 15
    db.commit()
    return RedirectResponse("/customers", status_code=status.HTTP_302_FOUND)
