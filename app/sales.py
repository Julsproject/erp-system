"""Sales history (fully-paid) and receivables (credit) with settlement."""
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

SETTLE_METHODS = [("cash", "Cash"), ("gcash", "GCash"), ("bank_transfer", "Bank Transfer"), ("cheque", "Cheque")]
PAGE_SIZE = 20


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


def _back_url(user, sale=None) -> str:
    """Where "back"/"cancel" should go — admins return to the receivables
    list; cashiers (who can't see that list) go to the customer's credit
    statement instead, since that's how they got here."""
    if is_admin(user):
        return "/sales/receivables"
    if sale and sale.customer_id:
        return f"/credits/{sale.customer_id}"
    return "/credits"


@router.get("/sales", response_class=HTMLResponse)
def sales_history(
    request: Request,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    q = (q or "").strip()
    page = max(page, 1)

    # "Fully paid" done at the SQL level (not in Python) so LIMIT/OFFSET
    # pagination is accurate: receivable_amount <= what's been settled so far.
    settled_sub = (
        db.query(
            models.ReceivableSettlement.sale_id.label("sid"),
            func.coalesce(func.sum(models.ReceivableSettlement.amount), 0).label("paid"),
        )
        .group_by(models.ReceivableSettlement.sale_id)
        .subquery()
    )
    query = (
        db.query(models.Sale)
        .outerjoin(settled_sub, settled_sub.c.sid == models.Sale.id)
        .filter(models.Sale.receivable_amount <= func.coalesce(settled_sub.c.paid, 0))
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(models.Sale.invoice_no.ilike(like), models.Sale.customer_name.ilike(like)))

    total_count, total_sales = query.with_entities(
        func.count(models.Sale.id), func.coalesce(func.sum(models.Sale.total), 0)
    ).one()
    pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)

    sales_page = (
        query.order_by(models.Sale.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    totals = {"count": total_count, "sales": Decimal(str(total_sales or 0))}
    return templates.TemplateResponse(
        "sales/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "sales": sales_page, "totals": totals, "tab": "all", "q": q,
         "page": page, "pages": pages},
    )


@router.get("/sales/receivables", response_class=HTMLResponse)
def receivables(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    q = (q or "").strip()
    settled = _settled_map(db)
    query = db.query(models.Sale).filter(models.Sale.receivable_amount > 0)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(models.Sale.invoice_no.ilike(like), models.Sale.customer_name.ilike(like)))
    with_credit = query.order_by(models.Sale.id.desc()).all()
    rows = []
    for s in with_credit:
        out = _outstanding(s, settled)
        if out > 0:
            rows.append((s, out))
    total_credit = sum((out for _, out in rows), Decimal("0"))
    return templates.TemplateResponse(
        "sales/receivables.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "total_credit": total_credit, "tab": "receivables", "q": q},
    )


@router.get("/sales/receivables/{sale_id:int}/pay", response_class=HTMLResponse)
def settle_form(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse(_back_url(user), status_code=302)
    settled = _settled_map(db)
    outstanding = _outstanding(sale, settled)
    return templates.TemplateResponse(
        "sales/settle.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "sale": sale, "outstanding": outstanding, "methods": SETTLE_METHODS, "error": None,
            "back_url": _back_url(user, sale),
        },
    )


@router.post("/sales/receivables/{sale_id:int}/pay")
async def settle_pay(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse(_back_url(user), status_code=302)

    settled = _settled_map(db)
    outstanding = _outstanding(sale, settled)
    form = await request.form()
    method = (form.get("method") or "cash").strip().lower()
    amount = _dec(form.get("amount"))

    def rerender(error):
        return templates.TemplateResponse(
            "sales/settle.html",
            {"request": request, "app_name": request.app.title, "user": user,
             "sale": sale, "outstanding": outstanding, "methods": SETTLE_METHODS, "error": error,
             "back_url": _back_url(user, sale)},
        )

    if amount <= 0:
        return rerender("Enter an amount greater than zero.")
    if amount > outstanding:
        amount = outstanding  # never collect more than owed

    if method == "cheque":
        # A post-dated cheque doesn't settle anything yet — it just goes into
        # the PDC register until the bank actually honors it.
        raw_date = (form.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            return rerender("Enter a valid cheque date (the date printed on the cheque).")
        pdc = models.PostDatedCheque(
            direction="received", amount=amount,
            bank=(form.get("bank") or "").strip() or None,
            cheque_no=(form.get("cheque_no") or "").strip() or None,
            cheque_date=cheque_date,
            sale_id=sale.id, customer_id=sale.customer_id,
            created_by=user.id,
        )
        db.add(pdc)
        db.flush()
        db.commit()
        return RedirectResponse(f"/pdc/{pdc.id}", status_code=status.HTTP_302_FOUND)

    db.add(models.ReceivableSettlement(
        sale_id=sale.id, method=method, amount=amount,
        cashier_id=user.id,
    ))
    db.commit()
    return RedirectResponse(_back_url(user, sale), status_code=status.HTTP_302_FOUND)
