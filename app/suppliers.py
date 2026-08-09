"""Supplier profiles and per-supplier purchase history."""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_staff
from .purchases import PAYMENT_METHODS, _purchase_outstanding, _settled_for_purchases
from .templating import templates

router = APIRouter()

PAYMENT_TERMS = ["COD", "7 days", "15 days", "30 days", "60 days", "50% DP", "Consignment"]
PAGE_SIZE = 15


def _next_code(db: Session) -> str:
    """Auto-generate SUP-0001 style codes when the user leaves Code blank."""
    n = db.query(func.count(models.Supplier.id)).scalar() or 0
    while True:
        n += 1
        code = f"SUP-{n:04d}"
        if not db.query(models.Supplier).filter(models.Supplier.code == code).first():
            return code


@router.get("/suppliers/search")
def search_suppliers(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """JSON autocomplete for the purchase form's supplier field."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not is_staff(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    q = (q or "").strip()
    query = db.query(models.Supplier).filter(models.Supplier.is_active.is_(True))
    if q:
        query = query.filter(models.Supplier.name.ilike(f"%{q}%"))
    rows = query.order_by(models.Supplier.name).limit(20).all()
    return {"suppliers": [{"id": s.id, "name": s.name, "code": s.code or "", "terms": s.payment_terms or ""} for s in rows]}


@router.post("/suppliers/quick")
async def quick_supplier(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Create a supplier that isn't on file yet, straight from the purchase form."""
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not is_staff(user):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Supplier name is required."}, status_code=400)

    existing = db.query(models.Supplier).filter(func.lower(models.Supplier.name) == name.lower()).first()
    if existing:
        return {"ok": True, "existed": True, "supplier": {"id": existing.id, "name": existing.name, "code": existing.code or ""}}

    supplier = models.Supplier(
        code=_next_code(db),
        name=name,
        contact_person=(data.get("contact_person") or "").strip() or None,
        mobile=(data.get("mobile") or "").strip() or None,
        payment_terms=(data.get("payment_terms") or "").strip() or None,
        payment_days=30,   # sensible default; refine later via the full Suppliers form
        is_active=True,
    )
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return {"ok": True, "existed": False, "supplier": {"id": supplier.id, "name": supplier.name, "code": supplier.code or ""}}


@router.get("/suppliers", response_class=HTMLResponse)
def list_suppliers(
    request: Request,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    q = (q or "").strip()
    page = max(page, 1)
    query = db.query(models.Supplier)
    if q:
        query = query.filter(models.Supplier.name.ilike(f"%{q}%"))
    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    suppliers = (
        query.order_by(models.Supplier.name)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    # Site-wide payables snapshot (not scoped to the name search above) —
    # what's actually still owed right now (net of any partial payments
    # already recorded), same basis as the Payables tab.
    settled_sub = (
        db.query(
            models.PurchaseSettlement.purchase_id.label("pid"),
            func.coalesce(func.sum(models.PurchaseSettlement.amount), 0).label("paid"),
        )
        .group_by(models.PurchaseSettlement.purchase_id)
        .subquery()
    )
    outstanding_expr = models.Purchase.total - func.coalesce(settled_sub.c.paid, 0)
    payables_owed, payables_count = (
        db.query(func.coalesce(func.sum(outstanding_expr), 0), func.count(models.Purchase.id))
        .outerjoin(settled_sub, settled_sub.c.pid == models.Purchase.id)
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
        .filter(outstanding_expr > 0)
        .one()
    )
    pending_pos = (
        db.query(func.count(models.Purchase.id))
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "pending")
        .scalar()
    )

    return templates.TemplateResponse(
        "suppliers/list.html",
        {"request": request, "app_name": request.app.title, "user": user, "suppliers": suppliers, "q": q,
         "page": page, "pages": pages, "total": total,
         "payables_owed": Decimal(str(payables_owed or 0)), "payables_count": payables_count,
         "pending_pos": pending_pos or 0},
    )


def _render_form(request, user, supplier=None, error=None):
    return templates.TemplateResponse(
        "suppliers/form.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "supplier": supplier, "terms": PAYMENT_TERMS, "error": error},
    )


@router.get("/suppliers/new", response_class=HTMLResponse)
def new_supplier(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    return _render_form(request, user)


@router.get("/suppliers/{supplier_id:int}/edit", response_class=HTMLResponse)
def edit_supplier(supplier_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    supplier = db.get(models.Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)
    return _render_form(request, user, supplier=supplier)


def _apply_form(supplier: models.Supplier, form):
    supplier.name = (form.get("name") or "").strip()
    supplier.contact_person = (form.get("contact_person") or "").strip() or None
    supplier.mobile = (form.get("mobile") or "").strip() or None
    supplier.telephone = (form.get("telephone") or "").strip() or None
    supplier.email = (form.get("email") or "").strip() or None
    supplier.address = (form.get("address") or "").strip() or None
    supplier.tin = (form.get("tin") or "").strip() or None
    supplier.payment_terms = (form.get("payment_terms") or "").strip() or None
    try:
        days = int(form.get("payment_days") or 30)
    except (TypeError, ValueError):
        days = 30
    supplier.payment_days = max(days, 0)
    supplier.is_active = (form.get("status") or "active") == "active"


@router.post("/suppliers")
async def create_supplier(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    form = await request.form()
    if not (form.get("name") or "").strip():
        return _render_form(request, user, error="Supplier name is required.")
    code = (form.get("code") or "").strip()
    if code and db.query(models.Supplier).filter(models.Supplier.code == code).first():
        return _render_form(request, user, error=f"Supplier code '{code}' is already used.")
    supplier = models.Supplier(code=code or _next_code(db))
    _apply_form(supplier, form)
    db.add(supplier)
    db.commit()
    return RedirectResponse("/suppliers", status_code=status.HTTP_302_FOUND)


@router.post("/suppliers/{supplier_id:int}")
async def update_supplier(supplier_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    supplier = db.get(models.Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)
    form = await request.form()
    if not (form.get("name") or "").strip():
        return _render_form(request, user, supplier=supplier, error="Supplier name is required.")
    code = (form.get("code") or "").strip()
    if code and code != (supplier.code or ""):
        clash = db.query(models.Supplier).filter(models.Supplier.code == code, models.Supplier.id != supplier.id).first()
        if clash:
            return _render_form(request, user, supplier=supplier, error=f"Supplier code '{code}' is already used.")
        supplier.code = code
    _apply_form(supplier, form)
    db.commit()
    return RedirectResponse("/suppliers", status_code=status.HTTP_302_FOUND)


@router.get("/suppliers/{supplier_id:int}/history", response_class=HTMLResponse)
def supplier_history(supplier_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Purchase history: receipts, returns, delivery history and item costs."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    supplier = db.get(models.Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.supplier_id == supplier_id)
        .order_by(models.Purchase.id.desc())
        .all()
    )
    received_total = Decimal("0")
    returned_total = Decimal("0")
    for p in purchases:
        if p.txn_type == "return":
            returned_total += (p.total or Decimal("0"))
        else:
            received_total += (p.total or Decimal("0"))

    # Item cost history for this supplier — latest cost paid per product.
    item_rows = {}
    for p in purchases:
        for ln in p.lines:
            key = (ln.product_id, ln.product_name, ln.unit_name)
            if key not in item_rows:          # purchases are newest-first
                item_rows[key] = {
                    "name": ln.product_name, "unit": ln.unit_name,
                    "last_cost": ln.unit_cost, "last_date": p.created_at,
                    "qty_total": Decimal("0"), "product": ln.product,
                }
            item_rows[key]["qty_total"] += (ln.qty or Decimal("0")) * (1 if p.txn_type != "return" else -1)

    # Outstanding payables + every payment applied against them — the AP
    # mirror of a customer's credit statement.
    payables = [p for p in purchases if p.txn_type == "receive" and p.status == "confirmed"]
    settled = _settled_for_purchases(db, [p.id for p in payables])
    payable_rows = []
    out_total = Decimal("0")
    for p in payables:
        outstanding = _purchase_outstanding(p, settled)
        payable_rows.append({"purchase": p, "outstanding": outstanding})
        out_total += outstanding
    settlements = (
        db.query(models.PurchaseSettlement)
        .join(models.Purchase, models.Purchase.id == models.PurchaseSettlement.purchase_id)
        .filter(models.Purchase.supplier_id == supplier_id)
        .order_by(models.PurchaseSettlement.created_at)
        .all()
    )

    return templates.TemplateResponse(
        "suppliers/history.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "supplier": supplier, "purchases": purchases,
            "received_total": received_total, "returned_total": returned_total,
            "net_total": received_total - returned_total,
            "items": list(item_rows.values()),
            "payable_rows": payable_rows, "out_total": out_total, "settlements": settlements,
            "payment_method_labels": dict(PAYMENT_METHODS),
        },
    )


def _outstanding_purchases(db: Session, supplier_id: int):
    """This supplier's still-owed deliveries, oldest first — the AP mirror
    of credits._outstanding_sales."""
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.supplier_id == supplier_id, models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
        .order_by(models.Purchase.id)
        .all()
    )
    settled = _settled_for_purchases(db, [p.id for p in purchases])
    out = []
    for p in purchases:
        outstanding = _purchase_outstanding(p, settled)
        if outstanding > 0:
            out.append((p, outstanding))
    return out


@router.get("/suppliers/{supplier_id:int}/pay-full", response_class=HTMLResponse)
def supplier_pay_full_form(supplier_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    supplier = db.get(models.Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)
    owed = _outstanding_purchases(db, supplier_id)
    total = sum((o for _, o in owed), Decimal("0"))
    return templates.TemplateResponse(
        "suppliers/pay_full.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "supplier": supplier, "owed": owed, "total": total, "methods": PAYMENT_METHODS, "error": None,
        },
    )


@router.post("/suppliers/{supplier_id:int}/pay-full")
async def supplier_pay_full_submit(supplier_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    supplier = db.get(models.Supplier, supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers", status_code=302)
    owed = _outstanding_purchases(db, supplier_id)
    total = sum((o for _, o in owed), Decimal("0"))

    form = await request.form()
    method = (form.get("method") or "cash").strip().lower()
    if method not in dict(PAYMENT_METHODS):
        method = "cash"

    def rerender(error):
        return templates.TemplateResponse(
            "suppliers/pay_full.html",
            {
                "request": request, "app_name": request.app.title, "user": user,
                "supplier": supplier, "owed": owed, "total": total, "methods": PAYMENT_METHODS, "error": error,
            },
        )

    if not owed:
        return RedirectResponse(f"/suppliers/{supplier_id}/history", status_code=status.HTTP_302_FOUND)

    cheque_date = None
    if method == "cheque":
        raw_date = (form.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            return rerender("Enter a valid cheque date (the date printed on the cheque).")

    # Pays every outstanding delivery off in full, oldest first — same
    # "clear the whole balance" idea as a customer's Pay Full. Cheque opens
    # ONE PDC for the whole total, with one PdcApplication row per purchase
    # it's covering — a physical cheque is one piece of paper, not one per
    # purchase; it doesn't settle until cleared.
    if method == "cheque":
        pdc = models.PostDatedCheque(
            direction="issued", amount=total,
            bank=(form.get("bank") or "").strip() or None,
            cheque_no=(form.get("cheque_no") or "").strip() or None,
            cheque_date=cheque_date,
            supplier_id=supplier.id,
            created_by=user.id,
        )
        db.add(pdc)
        db.flush()
        for purchase, outstanding in owed:
            db.add(models.PdcApplication(pdc_id=pdc.id, purchase_id=purchase.id, amount=outstanding))
    else:
        for purchase, outstanding in owed:
            db.add(models.PurchaseSettlement(
                purchase_id=purchase.id, method=method, amount=outstanding, created_by=user.id,
            ))
            purchase.status = "paid"
            purchase.payment_method = method
            purchase.paid_at = func.now()
    db.commit()
    return RedirectResponse(f"/suppliers/{supplier_id}/history", status_code=status.HTTP_302_FOUND)
