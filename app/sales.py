"""Sales history (fully-paid) and receivables (utang) with settlement."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

SETTLE_METHODS = [("cash", "Cash"), ("gcash", "GCash"), ("bank_transfer", "Bank Transfer"), ("cheque", "Cheque")]


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _settled_map(db: Session):
    rows = (
        db.query(models.ReceivableSettlement.sale_id, func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .group_by(models.ReceivableSettlement.sale_id)
        .all()
    )
    return {sid: Decimal(amt) for sid, amt in rows}


def _outstanding(sale, settled_map) -> Decimal:
    return (sale.receivable_amount or Decimal("0")) - settled_map.get(sale.id, Decimal("0"))


@router.get("/sales", response_class=HTMLResponse)
def sales_history(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    settled = _settled_map(db)
    recent = db.query(models.Sale).order_by(models.Sale.id.desc()).limit(500).all()
    paid = [s for s in recent if _outstanding(s, settled) <= 0]
    totals = {
        "count": len(paid),
        "sales": sum((s.total or 0 for s in paid), Decimal("0")),
    }
    return templates.TemplateResponse(
        "sales/list.html",
        {"request": request, "app_name": request.app.title, "user": user, "sales": paid, "totals": totals, "tab": "all"},
    )


@router.get("/sales/receivables", response_class=HTMLResponse)
def receivables(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    settled = _settled_map(db)
    with_utang = (
        db.query(models.Sale)
        .filter(models.Sale.receivable_amount > 0)
        .order_by(models.Sale.id.desc())
        .all()
    )
    rows = []
    for s in with_utang:
        out = _outstanding(s, settled)
        if out > 0:
            rows.append((s, out))
    total_utang = sum((out for _, out in rows), Decimal("0"))
    return templates.TemplateResponse(
        "sales/receivables.html",
        {"request": request, "app_name": request.app.title, "user": user, "rows": rows, "total_utang": total_utang, "tab": "receivables"},
    )


@router.get("/sales/receivables/{sale_id:int}/pay", response_class=HTMLResponse)
def settle_form(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/sales/receivables", status_code=302)
    settled = _settled_map(db)
    outstanding = _outstanding(sale, settled)
    return templates.TemplateResponse(
        "sales/settle.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "sale": sale, "outstanding": outstanding, "methods": SETTLE_METHODS, "error": None,
        },
    )


@router.post("/sales/receivables/{sale_id:int}/pay")
async def settle_pay(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/sales/receivables", status_code=302)

    settled = _settled_map(db)
    outstanding = _outstanding(sale, settled)
    form = await request.form()
    method = (form.get("method") or "cash").strip().lower()
    amount = _dec(form.get("amount"))

    def rerender(error):
        return templates.TemplateResponse(
            "sales/settle.html",
            {"request": request, "app_name": request.app.title, "user": user,
             "sale": sale, "outstanding": outstanding, "methods": SETTLE_METHODS, "error": error},
        )

    if amount <= 0:
        return rerender("Enter an amount greater than zero.")
    if amount > outstanding:
        amount = outstanding  # never collect more than owed

    db.add(models.ReceivableSettlement(
        sale_id=sale.id, method=method, amount=amount,
        bank=(form.get("bank") or "").strip() or None,
        cheque_no=(form.get("cheque_no") or "").strip() or None,
        cheque_date=(form.get("cheque_date") or "").strip() or None,
        cashier_id=user.id,
    ))
    db.commit()
    return RedirectResponse("/sales/receivables", status_code=status.HTTP_302_FOUND)
