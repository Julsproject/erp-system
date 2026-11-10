"""SI Numbers — every invoice number of a booklet in order (sunod-sunod),
with the numbers in between that were never used shown as MISSING.

A number is accounted for when it became a sale (or refund/exchange) or was
logged as cancelled/spoiled; anything else between the first and last number
used is missing. Two pads of one booklet can be in use at once, so a jump of
more than SERIES_BREAK unused numbers starts a new run instead of counting
the whole stretch between the pads as missing (the same rule as the VAT
report's gap check). Missing SI numbers also show in the Notifications Center
(see notifications._current_alerts) until they're found or logged.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_staff
from .templating import templates

router = APIRouter()

BOOKLETS = ["SI", "DRS", "DRB"]
SERIES_BREAK = 100


def _core(invoice_no):
    from .pos import _numeric_core  # local: pos imports a lot; keep this module light
    return _numeric_core(invoice_no)


def _runs(nums):
    """Consecutive stretches of used numbers, split at a jump > SERIES_BREAK."""
    ordered = sorted(nums)
    if not ordered:
        return []
    runs, start, prev = [], ordered[0], ordered[0]
    for n in ordered[1:]:
        if n - prev > SERIES_BREAK:
            runs.append((start, prev))
            start = n
        prev = n
    runs.append((start, prev))
    return runs


def booklet_numbers(db: Session, booklet: str):
    """(rows in number order, missing numbers, width). Each row is
    {number, state: sold|cancelled|missing, sales: [...], cancelled: row}."""
    sales_by_no, width = {}, 0
    for s in db.query(models.Sale).filter(models.Sale.receipt_type == booklet):
        n = _core(s.invoice_no)
        if n is None:
            continue
        sales_by_no.setdefault(n, []).append(s)
        width = max(width, len(str(n)))
    cancelled_by_no = {}
    for c in db.query(models.CancelledReceipt).filter(models.CancelledReceipt.receipt_type == booklet):
        n = _core(c.invoice_no)
        if n is not None:
            cancelled_by_no[n] = c

    rows, missing = [], []
    used = set(sales_by_no) | set(cancelled_by_no)
    for lo, hi in _runs(used):
        if rows:
            rows.append({"break": True, "number": f"{lo}"})
        for n in range(lo, hi + 1):
            if n in sales_by_no:
                rows.append({"number": n, "state": "sold",
                             "sales": sorted(sales_by_no[n], key=lambda s: s.id)})
            elif n in cancelled_by_no:
                rows.append({"number": n, "state": "cancelled", "cancelled": cancelled_by_no[n]})
            else:
                rows.append({"number": n, "state": "missing"})
                missing.append(n)
    return rows, missing, width


def missing_numbers(db: Session, booklet: str = "SI"):
    return booklet_numbers(db, booklet)[1]


@router.get("/sales/si-numbers", response_class=HTMLResponse)
def si_numbers(request: Request, booklet: str = "SI", missing_only: int = 0,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    booklet = booklet.upper() if booklet.upper() in BOOKLETS else "SI"
    rows, missing, _width = booklet_numbers(db, booklet)
    used = [r for r in rows if not r.get("break")]
    shown = [r for r in rows if r.get("break") or r["state"] == "missing"] if missing_only else rows
    return templates.TemplateResponse("sales/si_numbers.html", {
        "request": request, "app_name": request.app.title, "user": user,
        "booklet": booklet, "booklets": BOOKLETS, "missing_only": missing_only,
        "rows": shown, "missing": missing,
        "first": used[0]["number"] if used else None, "last": used[-1]["number"] if used else None,
        "sold_count": sum(1 for r in used if r["state"] == "sold"),
        "cancelled_count": sum(1 for r in used if r["state"] == "cancelled"),
    })
