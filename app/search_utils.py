from sqlalchemy import and_, func, or_


def multi_word_ilike(column, q: str):
    """Match every whitespace-separated word in q somewhere in column, in any
    order — so typing "tree size" finds "TREE BLUE SIZE 10" without requiring
    the words to be adjacent or typed in the product's actual order. Mirrors
    the matching POS's product search has used since it was added there."""
    terms = q.split()
    return and_(*[column.ilike(f"%{term}%") for term in terms])


def invoice_display_ilike(receipt_type_col, invoice_no_col, q: str):
    """Match an invoice the way it's written on the receipt — "SI33943",
    "DRB53121" — as well as by the bare number. The booklet letters live in
    receipt_type and the number in invoice_no, so searching the printed form
    found nothing before this; a handful of rows also have the letters typed
    into invoice_no itself, which the bare-column match still catches."""
    like = f"%{(q or '').strip()}%"
    display = func.concat(func.coalesce(receipt_type_col, ""), invoice_no_col)
    return or_(invoice_no_col.ilike(like), display.ilike(like))
