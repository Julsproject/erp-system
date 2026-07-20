"""Credits menu: look up a customer and view/print their credit (utang) statement."""
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()


def _settled_for(db: Session, sale_ids):
    if not sale_ids:
        return {}
    rows = (
        db.query(models.ReceivableSettlement.sale_id, func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(models.ReceivableSettlement.sale_id.in_(sale_ids))
        .group_by(models.ReceivableSettlement.sale_id)
        .all()
    )
    return {sid: Decimal(amt) for sid, amt in rows}


@router.get("/credits", response_class=HTMLResponse)
def credits_search(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    q = (q or "").strip()
    query = (
        db.query(models.Customer)
        .join(models.Sale, models.Sale.customer_id == models.Customer.id)
        .filter(models.Sale.receivable_amount > 0)
        .distinct()
    )
    if q:
        query = query.filter(models.Customer.name.ilike(f"%{q}%"))
    customers = query.order_by(models.Customer.name).all()

    # outstanding per listed customer
    outstanding = {}
    for c in customers:
        sales = db.query(models.Sale).filter(models.Sale.customer_id == c.id, models.Sale.receivable_amount > 0).all()
        settled = _settled_for(db, [s.id for s in sales])
        bal = sum(((s.receivable_amount or 0) - settled.get(s.id, 0) for s in sales), Decimal("0"))
        outstanding[c.id] = bal

    return templates.TemplateResponse(
        "credits/search.html",
        {"request": request, "app_name": request.app.title, "user": user, "customers": customers, "outstanding": outstanding, "q": q},
    )


@router.get("/credits/{customer_id:int}", response_class=HTMLResponse)
def credit_statement(customer_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.get(models.Customer, customer_id)
    if not customer:
        return RedirectResponse("/credits", status_code=302)

    sales = (
        db.query(models.Sale)
        .filter(models.Sale.customer_id == customer_id, models.Sale.receivable_amount > 0)
        .order_by(models.Sale.id)
        .all()
    )
    settled = _settled_for(db, [s.id for s in sales])
    rows = []
    orig_total = paid_total = out_total = Decimal("0")
    for s in sales:
        orig = s.receivable_amount or Decimal("0")
        paid = settled.get(s.id, Decimal("0"))
        outstanding = orig - paid
        rows.append({"sale": s, "orig": orig, "paid": paid, "outstanding": outstanding})
        orig_total += orig
        paid_total += paid
        out_total += outstanding

    return templates.TemplateResponse(
        "credits/statement.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "customer": customer, "rows": rows,
            "orig_total": orig_total, "paid_total": paid_total, "out_total": out_total,
        },
    )
