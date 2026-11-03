"""Reading a paper or spreadsheet document into a page for review — the POS
cart (a customer's DR/SI) or New Purchase (a supplier's delivery). Two
sources, one result shape: an Excel sheet (items matched by exact name) or a
photo (read and matched by photo_reader). Nothing here saves anything; the
page is filled in and the cashier checks it before completing it."""
import io
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from . import models, photo_reader

# Column titles a sheet's item table can use, lower-cased.
_COLUMNS = {
    "qty": ("qty", "quantity"),
    "unit": ("unit", "uom"),
    "name": ("product name", "product", "item", "item name", "description", "articles"),
    "price": ("unit price", "unit cost", "price", "cost"),
    "amount": ("subtotal", "amount", "line total"),
}
# Labels above the table, read from the cell to their right.
_LABELS = {
    "party": ("supplier", "supplier name", "customer", "customer name", "sold to", "delivered to"),
    "receipt_type": ("receipt", "receipt type", "booklet"),
    "doc_no": ("invoice / dr #", "invoice/dr #", "invoice", "invoice #", "invoice no", "dr", "dr #", "dr no",
               "reference", "ref", "no", "no."),
    "date": ("delivery date", "date"),
}
MAX_EXCEL_BYTES = 5 * 1024 * 1024
MAX_LINES = 500
_UNIT_ALIASES = {"pc": "piece", "pcs": "piece", "pieces": "piece", "pairs": "pair", "gal": "gallon",
                 "gals": "gallon", "bxs": "box", "boxes": "box", "mtr": "meter", "m": "meter", "meters": "meter"}


class ImportProblem(Exception):
    """A message for the person importing, shown as-is."""


def key(value) -> str:
    return " ".join(str(value if value is not None else "").split()).lower()


def unit_key(value) -> str:
    k = key(value).rstrip(".")
    return _UNIT_ALIASES.get(k, k)


def _number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("₱", "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text(value) -> str:
    """A cell as text — an invoice typed as a number comes back 16986.0."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value if value is not None else "").split())


def _date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _read_sheet(rows: list) -> dict | None:
    """Finds the header row, reads the labels above it and one item per row
    below it. None if no row has the needed column titles."""
    for header_at, row in enumerate(rows[:40]):
        titles = {}
        for col, cell in enumerate(row):
            k = key(cell)
            for field, names in _COLUMNS.items():
                if k in names and field not in titles:
                    titles[field] = col
        if "qty" in titles and "name" in titles and ("price" in titles or "amount" in titles):
            break
    else:
        return None

    head = {"party": "", "receipt_type": "", "doc_no": "", "date": ""}
    for row in rows[:header_at]:
        for col, cell in enumerate(row):
            label = key(cell).rstrip(":").strip()
            # Only the cell right beside the label — a blank one stays blank
            # instead of picking up a note further along the row.
            value = row[col + 1] if col + 1 < len(row) else None
            if value in (None, ""):
                continue
            for field, names in _LABELS.items():
                if label in names and not head[field]:
                    head[field] = _date(value) if field == "date" else _text(value)

    def cell(row, field):
        col = titles.get(field)
        return row[col] if col is not None and col < len(row) else None

    items = []
    for row_no, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        name = _text(cell(row, "name"))
        qty, price, amount = _number(cell(row, "qty")), _number(cell(row, "price")), _number(cell(row, "amount"))
        # A Total line or a note under the table has no item name.
        if not name or (qty is None and price is None and amount is None) or key(name).startswith("example:"):
            continue
        if price is None and amount is not None and qty:
            price = (amount / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        items.append({"where": f"Row {row_no}, {name}", "text": name, "qty": qty, "unit": _text(cell(row, "unit")),
                      "price": price, "product_id": None, "confidence": "high", "note": ""})
    return {**head, "items": items}


def from_excel(db: Session, filename: str, contents: bytes) -> dict:
    """{"party", "receipt_type", "doc_no", "date", "items", "warnings"} — each
    item matched to an active product by its exact name (spacing and capitals
    aside) through "product_id"."""
    if not (filename or "").lower().endswith(".xlsx"):
        raise ImportProblem("Choose an Excel .xlsx file. An older .xls file has to be saved as .xlsx first.")
    if len(contents) > MAX_EXCEL_BYTES:
        raise ImportProblem("That file is too big — it should be under 5 MB.")
    from openpyxl import load_workbook
    try:
        book = load_workbook(io.BytesIO(contents), data_only=True, read_only=True)
        parsed = None
        for sheet in [book.active] + [ws for ws in book.worksheets if ws is not book.active]:
            parsed = _read_sheet([list(r) for r in sheet.iter_rows(values_only=True)])
            if parsed:
                break
        book.close()
    except Exception:
        raise ImportProblem("That file couldn't be read. Open it in Excel, save it as .xlsx and try again.")
    if not parsed:
        raise ImportProblem("No item table found. The sheet needs columns titled Qty, Product Name and Unit Price.")
    items = parsed["items"]
    if not items:
        raise ImportProblem("The item table is empty.")
    if len(items) > MAX_LINES:
        raise ImportProblem(f"That's {len(items)} rows — the most one import can take is {MAX_LINES}.")

    warnings = []
    active, archived = {}, set()
    for pid, name, is_active in db.query(models.Product.id, models.Product.name, models.Product.is_active):
        if is_active:
            active.setdefault(key(name), pid)
        else:
            archived.add(key(name))
    for item in items:
        item["product_id"] = active.get(key(item["text"]))
        if not item["product_id"] and key(item["text"]) in archived:
            warnings.append(f"{item['where']}: an archived item has this name, so it wasn't matched.")
    return {**{k: parsed[k] for k in ("party", "receipt_type", "doc_no", "date")}, "items": items, "warnings": warnings}


def from_photo(db: Session, contents: bytes, kind: str) -> dict:
    """Same shape as from_excel, read from a photo. Raises ImportProblem."""
    try:
        result = photo_reader.read_document(db, contents, kind)
    except photo_reader.PhotoReadError as e:
        raise ImportProblem(str(e))
    if not result["lines"]:
        raise ImportProblem("No item lines could be read from that photo. Take it again straight-on, in good light, "
                            "with the whole document in the picture.")
    warnings = []
    note = photo_reader.total_warning(result)
    if note:
        warnings.append(note)
    items = []
    for n, line in enumerate(result["lines"], start=1):
        where = f"Line {n}, “{line['written_text']}”"
        if line["note"]:
            warnings.append(f"{where}: {line['note']}")
        if line["product"] and line["confidence"] != "high":
            warnings.append(f"{where}: matched to {line['product'].name} — {line['confidence']} confidence, please check.")
        items.append({
            "where": where, "text": line["written_text"], "qty": line["qty"], "unit": line["unit"],
            "price": line["unit_price"], "product_id": line["product"].id if line["product"] else None,
            "confidence": line["confidence"], "note": line["note"],
        })
    for skipped in result["skipped"]:
        warnings.append(f"Left out: {skipped}")
    return {"party": result["party_name"], "receipt_type": "", "doc_no": result["doc_no"], "date": result["date"],
            "items": items, "warnings": warnings}


def template(kind: str) -> bytes:
    """A blank sheet in the layout from_excel reads."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    if kind == "sale":
        sheet.title = "Sale"
        head = [["Customer", ""], ["Receipt", "DRS"], ["Invoice #", ""], ["Date", ""]]
    else:
        sheet.title = "Delivery"
        head = [["Supplier", ""], ["Invoice / DR #", ""], ["Delivery date", ""]]
    for row in head:
        sheet.append(row)
    sheet.append([])
    sheet.append(["Qty", "Unit", "Product Name", "Unit Price"])
    sheet.append([10, "Piece", "Example: type the item name exactly as in Inventory", 25.5])
    header_row = len(head) + 2
    for r in range(1, len(head) + 1):
        sheet.cell(r, 1).font = Font(name="Arial", bold=True)
    for c in range(1, 5):
        sheet.cell(header_row, c).font = Font(name="Arial", bold=True)
    for col, width in zip("ABCD", (16, 14, 52, 12)):
        sheet.column_dimensions[col].width = width
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
