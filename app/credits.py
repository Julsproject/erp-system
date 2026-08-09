"""Credits menu: look up a customer and view/print their credit statement."""
import io
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, settings_store
from .database import get_db
from .deps import get_current_user
from .sales import SETTLE_METHODS
from .templating import templates

router = APIRouter()

PAGE_SIZE = 15


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
def credits_search(
    request: Request,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    q = (q or "").strip()
    page = max(page, 1)
    query = (
        db.query(models.Customer)
        .join(models.Sale, models.Sale.customer_id == models.Customer.id)
        .filter(models.Sale.receivable_amount > 0)
        .distinct()
    )
    if q:
        query = query.filter(models.Customer.name.ilike(f"%{q}%"))
    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    customers = (
        query.order_by(models.Customer.name)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    # outstanding per listed customer
    outstanding = {}
    for c in customers:
        sales = db.query(models.Sale).filter(models.Sale.customer_id == c.id, models.Sale.receivable_amount > 0).all()
        settled = _settled_for(db, [s.id for s in sales])
        bal = sum(((s.receivable_amount or 0) - settled.get(s.id, 0) for s in sales), Decimal("0"))
        outstanding[c.id] = bal

    return templates.TemplateResponse(
        "credits/search.html",
        {"request": request, "app_name": request.app.title, "user": user, "customers": customers,
         "outstanding": outstanding, "q": q, "page": page, "pages": pages},
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
        sold_items = [{"name": ln.product_name, "qty": ln.qty, "unit": ln.unit_name} for ln in s.lines if (ln.qty or 0) > 0]
        rows.append({"sale": s, "orig": orig, "paid": paid, "outstanding": outstanding, "sold_items": sold_items})
        orig_total += orig
        paid_total += paid
        out_total += outstanding

    # Every individual payment AND return/exchange applied against these
    # invoices — so a customer can point at "I returned 5 hammers on the
    # 28th" instead of just seeing a Paid total that moved with no
    # explanation.
    settlements = (
        db.query(models.ReceivableSettlement)
        .filter(models.ReceivableSettlement.sale_id.in_([s.id for s in sales]))
        .order_by(models.ReceivableSettlement.created_at)
        .all()
    ) if sales else []

    # Older settlements (recorded before source_sale_id was tracked) don't
    # carry that link — this backfills a candidate list of this customer's
    # own refund/exchange transactions against these invoices, to match up
    # by amount below.
    related_returns = (
        db.query(models.Sale)
        .filter(models.Sale.original_sale_id.in_([s.id for s in sales]), models.Sale.txn_type.in_(("refund", "exchange")))
        .all()
    ) if sales else []

    # For a credit_note settlement, list exactly which product(s) came back —
    # a refund's lines are all returned; an exchange's are mixed, so only the
    # negative-qty side (what went back) counts as "returned" here.
    returned_items = {}
    for st in settlements:
        if st.method != "credit_note":
            continue
        source_sale = st.source_sale
        if not source_sale:
            source_sale = next(
                (r for r in related_returns if r.original_sale_id == st.sale_id and abs(r.total or 0) == st.amount),
                None,
            )
        if not source_sale:
            continue
        items = []
        for ln in source_sale.lines:
            if source_sale.txn_type == "refund" or (ln.qty or 0) < 0:
                items.append({"name": ln.product_name, "qty": abs(ln.qty or 0), "unit": ln.unit_name})
        returned_items[st.id] = items

    return templates.TemplateResponse(
        "credits/statement.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "customer": customer, "rows": rows, "settlements": settlements, "returned_items": returned_items,
            "orig_total": orig_total, "paid_total": paid_total, "out_total": out_total,
        },
    )


@router.get("/credits/{customer_id:int}/pdf")
def credit_statement_pdf(customer_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A simple downloadable PDF of the customer's credit statement — every
    credit invoice, the items bought on it, and the balances. Matches the
    on-screen statement, just as a file instead of print-to-PDF."""
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
        items = ", ".join(
            f"{float(ln.qty):g} {ln.unit_name or ''} {ln.product_name}".strip()
            for ln in s.lines if (ln.qty or 0) > 0
        )
        rows.append((s, orig, paid, outstanding, items))
        orig_total += orig
        paid_total += paid
        out_total += outstanding

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from .pdf_utils import letterhead

    biz = settings_store.get_all(db)
    as_of = rows[0][0].created_at.strftime("%b %d, %Y") if rows and rows[0][0].created_at else ""
    doc_meta = [f"As of: {as_of}"] if as_of else []

    party_lines = [customer.name]
    if customer.tin:
        party_lines.append(f"TIN {customer.tin}")
    if customer.address:
        party_lines.append(customer.address)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    styles = getSampleStyleSheet()
    elements = letterhead(biz, "Statement of Account", doc_meta, "Customer", party_lines)

    table_data = [["Invoice #", "Date", "Items", "Credit", "Paid", "Outstanding"]]
    for s, orig, paid, outstanding, items in rows:
        date_str = s.created_at.strftime("%b %d, %Y") if s.created_at else "-"
        table_data.append([s.invoice_no, date_str, items or "-", f"{orig:,.2f}", f"{paid:,.2f}", f"{outstanding:,.2f}"])
    table_data.append(["", "", "Totals", f"{orig_total:,.2f}", f"{paid_total:,.2f}", f"{out_total:,.2f}"])

    table = Table(table_data, colWidths=[55, 65, 185, 65, 65, 75], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F6FEB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))
    elements.append(Paragraph(f"<b>Outstanding balance: {out_total:,.2f}</b>", styles["Heading3"]))

    doc.build(elements)
    buf.seek(0)
    safe_name = "".join(c for c in customer.name if c.isalnum() or c in (" ", "_", "-")).strip() or "customer"
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_statement.pdf"'},
    )


def _outstanding_sales(db: Session, customer_id: int):
    """This customer's still-owed invoices, oldest first — settling the
    whole balance pays these off in the order they were incurred."""
    sales = (
        db.query(models.Sale)
        .filter(models.Sale.customer_id == customer_id, models.Sale.receivable_amount > 0)
        .order_by(models.Sale.id)
        .all()
    )
    settled = _settled_for(db, [s.id for s in sales])
    out = []
    for s in sales:
        outstanding = (s.receivable_amount or Decimal("0")) - settled.get(s.id, Decimal("0"))
        if outstanding > 0:
            out.append((s, outstanding))
    return out


@router.get("/credits/{customer_id:int}/pay-full", response_class=HTMLResponse)
def pay_full_form(customer_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.get(models.Customer, customer_id)
    if not customer:
        return RedirectResponse("/credits", status_code=302)
    owed = _outstanding_sales(db, customer_id)
    total = sum((o for _, o in owed), Decimal("0"))
    return templates.TemplateResponse(
        "credits/pay_full.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "customer": customer, "owed": owed, "total": total, "methods": SETTLE_METHODS, "error": None,
        },
    )


@router.post("/credits/{customer_id:int}/pay-full")
async def pay_full_submit(customer_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.get(models.Customer, customer_id)
    if not customer:
        return RedirectResponse("/credits", status_code=302)
    owed = _outstanding_sales(db, customer_id)
    total = sum((o for _, o in owed), Decimal("0"))

    form = await request.form()
    method = (form.get("method") or "cash").strip().lower()

    def rerender(error):
        return templates.TemplateResponse(
            "credits/pay_full.html",
            {
                "request": request, "app_name": request.app.title, "user": user,
                "customer": customer, "owed": owed, "total": total, "methods": SETTLE_METHODS, "error": error,
            },
        )

    if not owed:
        return RedirectResponse(f"/credits/{customer_id}", status_code=status.HTTP_302_FOUND)

    cheque_date = None
    if method == "cheque":
        raw_date = (form.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            return rerender("Enter a valid cheque date (the date printed on the cheque).")

    # Pays every outstanding invoice off in full, oldest first — this is
    # "clear the whole balance," not a partial amount, so there's nothing to
    # split or leave short. Cheque still opens one PDC per invoice (each
    # needs its own sale_id to reconcile against), all from the same cheque.
    for sale, outstanding in owed:
        if method == "cheque":
            db.add(models.PostDatedCheque(
                direction="received", amount=outstanding,
                bank=(form.get("bank") or "").strip() or None,
                cheque_no=(form.get("cheque_no") or "").strip() or None,
                cheque_date=cheque_date,
                sale_id=sale.id, customer_id=customer.id,
                created_by=user.id,
            ))
        else:
            db.add(models.ReceivableSettlement(
                sale_id=sale.id, method=method, amount=outstanding, cashier_id=user.id,
            ))
    db.commit()
    return RedirectResponse(f"/credits/{customer_id}", status_code=status.HTTP_302_FOUND)
