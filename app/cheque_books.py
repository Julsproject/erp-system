"""Cheque series register: booklets, next-number, duplicate guard, gaps.

A cheque number written into a free-text box tells you nothing after the
fact — you can type the same one twice and never notice, and a cheque that
was torn out but never recorded leaves no trace at all. A ChequeBook turns
the numbers into a range you can audit:

  * the next number is suggested, not remembered
  * the same number can't be used twice on one booklet (DB unique index)
  * every number in the range with no cheque behind it shows as a GAP —
    "unaccounted for" — which is the whole point of the register
  * a spoiled cheque is recorded as a status="voided" cheque worth 0, so
    the number is accounted for instead of reading as a gap

Only ISSUED cheques belong to a booklet; a received one is drawn on the
customer's own bank and carries no number of ours.
"""
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import audit, models
from .database import get_db
from .deps import get_current_user, is_staff
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")

# A real booklet is 25-100 leaves. The cap is a typo guard (start 1, end
# 999999 would otherwise try to render a million rows), not a rule about
# how big a booklet may be — raise it if a bank ever issues a bigger one.
MAX_BOOK_SIZE = 500

# What a number that IS spoken for can be sitting in. A number missing from
# this map was never used at all, which is what makes it a gap.
SEQ_STATUS_LABELS = {
    "pending": "Issued",
    "deposited": "Deposited",
    "cleared": "Cleared",
    "bounced": "Bounced",
    "cancelled": "Cancelled",
    "voided": "Voided (spoiled)",
}


def _today():
    return datetime.now(MANILA).date()


def format_cheque_no(book: models.ChequeBook, seq) -> str:
    """The number as it's printed on the cheque face — prefix + zero-padded
    sequence, e.g. book(prefix="A", digits=7) with seq 12345 -> "A0012345"."""
    if book is None or seq is None:
        return ""
    width = max(int(book.digits or 1), 1)
    return "{}{}".format(book.prefix or "", str(int(seq)).zfill(width))


def used_seqs(db: Session, book_id: int) -> dict:
    """{cheque_seq: PostDatedCheque} for one booklet — every number spoken
    for, in any status. Voided ones count: a spoiled cheque still uses its
    number up."""
    rows = (
        db.query(models.PostDatedCheque)
        .filter(models.PostDatedCheque.cheque_book_id == book_id,
                models.PostDatedCheque.cheque_seq.isnot(None))
        .all()
    )
    return {int(r.cheque_seq): r for r in rows}


def next_seq(db: Session, book: models.ChequeBook):
    """The next number to write, or None once the booklet is used up.

    Highest-used + 1 rather than lowest-unused: cheques come off a physical
    pad in order, so the number after the last one written is the one now on
    top. A gap further back is something to investigate, not the next cheque
    to hand out.
    """
    if not book:
        return None
    used = used_seqs(db, book.id)
    start, end = int(book.start_no), int(book.end_no)
    candidate = (max(used) + 1) if used else start
    if candidate < start:
        candidate = start
    return candidate if candidate <= end else None


def seq_taken(db: Session, book_id: int, seq: int) -> bool:
    return (
        db.query(models.PostDatedCheque.id)
        .filter(models.PostDatedCheque.cheque_book_id == book_id,
                models.PostDatedCheque.cheque_seq == seq)
        .first()
        is not None
    )


def book_stats(db: Session, book: models.ChequeBook) -> dict:
    """Counts behind the booklet's progress line and its gap warning."""
    used = used_seqs(db, book.id)
    start, end = int(book.start_no), int(book.end_no)
    total = max(end - start + 1, 0)
    # Only numbers BELOW the highest one written can be gaps. The unwritten
    # tail of the pad is just cheques not used yet, not missing ones.
    highest = max(used) if used else None
    gaps = [n for n in range(start, highest)] if highest else []
    gaps = [n for n in gaps if n not in used]
    return {
        "total": total,
        "used": len(used),
        "left": max(total - len(used), 0),
        "gaps": gaps,
        "gap_count": len(gaps),
        "next": next_seq(db, book),
        "highest": highest,
    }


def active_books_for(db: Session, bank_account_id: int):
    return (
        db.query(models.ChequeBook)
        .filter(models.ChequeBook.bank_account_id == bank_account_id,
                models.ChequeBook.is_active.is_(True))
        .order_by(models.ChequeBook.start_no)
        .all()
    )


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #
@router.get("/cheques/books", response_class=HTMLResponse)
def list_books(request: Request, error: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    books = (
        db.query(models.ChequeBook)
        .order_by(models.ChequeBook.is_active.desc(), models.ChequeBook.bank_account_id,
                  models.ChequeBook.start_no)
        .all()
    )
    rows = [{"book": b, "stats": book_stats(db, b)} for b in books]
    total_gaps = sum(r["stats"]["gap_count"] for r in rows)
    accounts = (
        db.query(models.BankAccount)
        .filter(models.BankAccount.is_active.is_(True))
        .order_by(models.BankAccount.name).all()
    )
    return templates.TemplateResponse(
        "cheques/books.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "total_gaps": total_gaps, "accounts": accounts,
         "fmt": format_cheque_no, "error": error},
    )


@router.post("/cheques/books")
def create_book(
    request: Request,
    bank_account_id: str = Form(""), prefix: str = Form(""),
    start_no: str = Form(""), end_no: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    def fail(msg):
        return RedirectResponse("/cheques/books?error=" + quote(msg), status_code=status.HTTP_302_FOUND)

    try:
        account = db.get(models.BankAccount, int(bank_account_id or 0))
    except ValueError:
        account = None
    if not account:
        return fail("Choose which bank account this booklet is drawn on.")

    raw_start = (start_no or "").strip()
    raw_end = (end_no or "").strip()
    try:
        start, end = int(raw_start), int(raw_end)
    except ValueError:
        return fail("Start and end numbers must be whole numbers, digits only.")
    if start <= 0 or end < start:
        return fail("The end number has to be the same as or higher than the start number.")
    if (end - start + 1) > MAX_BOOK_SIZE:
        return fail("That range covers {} cheques — more than a booklet holds. Check the numbers.".format(end - start + 1))

    # Overlapping ranges on one account would make "which booklet is this
    # number from?" ambiguous, and the duplicate guard is per-booklet.
    for other in db.query(models.ChequeBook).filter(models.ChequeBook.bank_account_id == account.id).all():
        if start <= int(other.end_no) and int(other.start_no) <= end:
            return fail("Numbers {}-{} are already registered on {}.".format(other.start_no, other.end_no, account.name))

    book = models.ChequeBook(
        bank_account_id=account.id,
        prefix=(prefix or "").strip() or None,
        start_no=start, end_no=end,
        # Match however the start number was typed, so 0000045 stays 7 wide.
        digits=len(raw_start),
        notes=(notes or "").strip() or None,
    )
    db.add(book)
    db.flush()
    label = "{}-{}".format(format_cheque_no(book, start), format_cheque_no(book, end))
    audit.record(
        db, user=user, request=request, action="create", entity_type="cheque_book",
        entity_id=book.id, entity_label=label,
        summary="Registered cheque booklet {} on {}".format(label, account.name),
    )
    db.commit()
    return RedirectResponse("/cheques/books/{}".format(book.id), status_code=status.HTTP_302_FOUND)


@router.get("/cheques/books/{book_id:int}", response_class=HTMLResponse)
def view_book(book_id: int, request: Request, error: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Every number in the booklet, in order, with what happened to it. The
    gap rows are the reason this page exists."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    book = db.get(models.ChequeBook, book_id)
    if not book:
        return RedirectResponse("/cheques/books", status_code=302)

    used = used_seqs(db, book.id)
    stats = book_stats(db, book)
    highest = stats["highest"]
    rows = []
    for seq in range(int(book.start_no), int(book.end_no) + 1):
        pdc = used.get(seq)
        if pdc is not None:
            state = "used"
        elif highest is not None and seq < highest:
            state = "gap"        # skipped over — a cheque is unaccounted for
        else:
            state = "unused"     # still on the pad
        rows.append({"seq": seq, "no": format_cheque_no(book, seq), "pdc": pdc, "state": state})

    return templates.TemplateResponse(
        "cheques/book_view.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "book": book, "rows": rows, "stats": stats, "labels": SEQ_STATUS_LABELS,
         "fmt": format_cheque_no, "error": error},
    )


@router.post("/cheques/books/{book_id:int}/void")
def void_number(
    book_id: int, request: Request, seq: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Account for a spoiled cheque — torn, misprinted, never handed over.
    Recorded at 0 with no invoices behind it, purely so the number stops
    showing as a gap."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    book = db.get(models.ChequeBook, book_id)
    if not book:
        return RedirectResponse("/cheques/books", status_code=302)

    def fail(msg):
        return RedirectResponse("/cheques/books/{}?error={}".format(book_id, quote(msg)), status_code=status.HTTP_302_FOUND)

    try:
        number = int((seq or "").strip())
    except ValueError:
        return fail("Enter the number of the spoiled cheque.")
    if not (int(book.start_no) <= number <= int(book.end_no)):
        return fail("{} isn't in this booklet ({}-{}).".format(number, book.start_no, book.end_no))
    if seq_taken(db, book.id, number):
        return fail("Cheque {} is already recorded.".format(format_cheque_no(book, number)))

    pdc = models.PostDatedCheque(
        direction="issued", status="voided", amount=Decimal("0"),
        cheque_no=format_cheque_no(book, number), cheque_date=_today(),
        bank_account_id=book.bank_account_id, cheque_book_id=book.id, cheque_seq=number,
        notes=(notes or "").strip() or "Spoiled cheque",
        created_by=user.id, resolved_at=datetime.now(MANILA),
    )
    db.add(pdc)
    db.flush()
    audit.record(
        db, user=user, request=request, action="void", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no,
        summary="Voided spoiled cheque {} — number accounted for, no payment".format(pdc.cheque_no),
    )
    db.commit()
    return RedirectResponse("/cheques/books/{}".format(book_id), status_code=status.HTTP_302_FOUND)


@router.post("/cheques/books/{book_id:int}/toggle")
def toggle_book(book_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Close a finished booklet (or reopen one closed by mistake). A closed
    booklet stops being offered on the payment screen; its numbers stay in
    the register."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    book = db.get(models.ChequeBook, book_id)
    if not book:
        return RedirectResponse("/cheques/books", status_code=302)
    book.is_active = not bool(book.is_active)
    audit.record(
        db, user=user, request=request, action="update", entity_type="cheque_book",
        entity_id=book.id,
        entity_label="{}-{}".format(format_cheque_no(book, book.start_no), format_cheque_no(book, book.end_no)),
        summary=("Reopened" if book.is_active else "Closed") + " cheque booklet",
    )
    db.commit()
    return RedirectResponse("/cheques/books/{}".format(book_id), status_code=status.HTTP_302_FOUND)
