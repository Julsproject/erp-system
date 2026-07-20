"""Inventory / Products module: encode products with beginning stock.

Columns the client asked for: Product Name, Category, Unit Type, Cost of Sales,
Selling Price, Actual Beginning Stocks, Stocks Qty, Total Qty.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

PAGE_SIZE = 20

# Columns used for the import template and the upload parser.
TEMPLATE_HEADERS = [
    "Product Name",
    "Category",
    "Unit Type",
    "Cost of Sales",
    "Selling Price",
    "Actual Beginning Stocks",
    "Stocks Qty",
]

# Maps (normalized) spreadsheet headers -> internal field names, so columns can
# be in any order and tolerate small naming differences.
HEADER_MAP = {
    "product name": "name", "name": "name", "product": "name",
    "category": "category",
    "unit type": "unit_type", "unit": "unit_type", "unit of measure": "unit_type", "uom": "unit_type",
    "cost of sales": "cost", "cost": "cost", "cost price": "cost",
    "selling price": "selling", "price": "selling", "srp": "selling",
    "actual beginning stocks": "beginning", "beginning stock": "beginning",
    "beginning stocks": "beginning", "beginning": "beginning",
    "stocks qty": "stocks", "stock qty": "stocks", "stocks": "stocks", "stock": "stocks",
    "vat": "vat", "vat-able": "vat", "vatable": "vat",
}
FIELDS = ["name", "category", "unit_type", "cost", "selling", "beginning", "stocks", "vat"]


def _to_decimal(value: str, default: str = "0") -> Decimal:
    value = (value or "").strip().replace(",", "")
    if value == "":
        value = default
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(default)


def _get_or_create_category(db: Session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    existing = db.query(models.Category).filter(func.lower(models.Category.name) == name.lower()).first()
    if existing:
        return existing
    cat = models.Category(name=name)
    db.add(cat)
    db.flush()
    return cat


def _get_or_create_unit_type(db: Session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    existing = db.query(models.UnitType).filter(func.lower(models.UnitType.name) == name.lower()).first()
    if existing:
        return existing
    unit = models.UnitType(name=name)
    db.add(unit)
    db.flush()
    return unit


@router.get("/products", response_class=HTMLResponse)
def list_products(
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

    query = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if q:
        query = query.filter(models.Product.name.ilike(f"%{q}%"))

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)

    products = (
        query.order_by(models.Product.name)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    return templates.TemplateResponse(
        "products/list.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "products": products,
            "q": q,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )


def _render_form(request, db, user, product=None, error=None):
    categories = db.query(models.Category).order_by(models.Category.name).all()
    unit_types = db.query(models.UnitType).order_by(models.UnitType.name).all()
    return templates.TemplateResponse(
        "products/form.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "product": product,
            "categories": categories,
            "unit_types": unit_types,
            "error": error,
        },
    )


@router.get("/products/new", response_class=HTMLResponse)
def new_product(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _render_form(request, db, product=None, user=user)


@router.get("/products/{product_id:int}/edit", response_class=HTMLResponse)
def edit_product(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    product = db.get(models.Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=302)
    return _render_form(request, db, user, product=product)


def _save_from_form(product: models.Product, db: Session, form):
    product.name = (form.get("name") or "").strip()
    product.category = _get_or_create_category(db, form.get("category"))
    product.unit_type = _get_or_create_unit_type(db, form.get("unit_type"))
    product.cost_price = _to_decimal(form.get("cost_price"))
    product.selling_price = _to_decimal(form.get("selling_price"))
    product.beginning_stock = _to_decimal(form.get("beginning_stock"))
    product.stock_qty = _to_decimal(form.get("stock_qty"))
    product.is_vat = bool(form.get("is_vat"))

    # Units ladder (extra sellable units). Parallel arrays from the form.
    names = form.getlist("unit_name")
    factors = form.getlist("unit_factor")
    prices = form.getlist("unit_price")
    product.units.clear()
    order = 0
    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        fac = _to_decimal(factors[i] if i < len(factors) else "1", "1")
        if fac <= 0:
            fac = Decimal("1")
        pr = _to_decimal(prices[i] if i < len(prices) else "0")
        product.units.append(
            models.ProductUnit(name=nm, factor_to_base=fac, price=pr, sort_order=order)
        )
        order += 1


@router.post("/products")
async def create_product(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not (form.get("name") or "").strip():
        return _render_form(request, db, user, product=None, error="Product name is required.")
    product = models.Product()
    _save_from_form(product, db, form)
    db.add(product)
    db.commit()
    return RedirectResponse("/products", status_code=status.HTTP_302_FOUND)


@router.post("/products/{product_id:int}")
async def update_product(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    product = db.get(models.Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=302)
    form = await request.form()
    if not (form.get("name") or "").strip():
        return _render_form(request, db, user, product=product, error="Product name is required.")
    _save_from_form(product, db, form)
    db.commit()
    return RedirectResponse("/products", status_code=status.HTTP_302_FOUND)


@router.post("/products/{product_id:int}/archive")
def archive_product(product_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    product = db.get(models.Product, product_id)
    if product:
        product.is_active = False
        db.commit()
    return RedirectResponse("/products", status_code=status.HTTP_302_FOUND)


# --------------------------------------------------------------------------- #
# Bulk import (Excel / CSV)
# --------------------------------------------------------------------------- #
def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "y", "yes", "true", "vat", "x", "✓", "oui"}


def _parse_upload(filename: str, contents: bytes):
    """Return (rows, error). rows is a list of dicts keyed by FIELDS."""
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            text = contents.decode("utf-8-sig", errors="replace")
            table = list(csv.reader(io.StringIO(text)))
        elif name.endswith(".xlsx") or name.endswith(".xlsm"):
            wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
            ws = wb.active
            table = [list(row) for row in ws.iter_rows(values_only=True)]
        else:
            return None, "Unsupported file type. Please upload a .xlsx or .csv file."
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the file: {exc}"

    if not table:
        return None, "The file appears to be empty."

    header = table[0]
    idx = {}
    for i, cell in enumerate(header):
        key = str(cell or "").strip().lower()
        if key in HEADER_MAP:
            idx[HEADER_MAP[key]] = i
    if "name" not in idx:
        return None, (
            "Missing a 'Product Name' column. Download the template to see the "
            "expected format."
        )

    def cell(raw, field):
        i = idx.get(field)
        if i is None or i >= len(raw):
            return ""
        val = raw[i]
        return "" if val is None else str(val).strip()

    rows = []
    for raw in table[1:]:
        if not raw:
            continue
        record = {f: cell(raw, f) for f in FIELDS}
        if not any(record[f] for f in FIELDS):  # skip blank rows
            continue
        rows.append(record)
    return rows, None


@router.get("/products/import", response_class=HTMLResponse)
def import_form(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "products/import.html",
        {"request": request, "app_name": request.app.title, "user": user, "result": None},
    )


@router.post("/products/import", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)

    contents = await file.read()
    rows, error = _parse_upload(file.filename, contents)

    if error:
        result = {"error": error}
    else:
        created = updated = skipped = 0
        errors = []
        for line_no, record in enumerate(rows, start=2):  # row 2 = first data row
            name = record["name"].strip()
            if not name:
                skipped += 1
                continue
            try:
                existing = (
                    db.query(models.Product)
                    .filter(func.lower(models.Product.name) == name.lower())
                    .filter(models.Product.is_active.is_(True))
                    .first()
                )
                product = existing or models.Product()
                product.name = name
                product.category = _get_or_create_category(db, record["category"])
                product.unit_type = _get_or_create_unit_type(db, record["unit_type"])
                product.cost_price = _to_decimal(record["cost"])
                product.selling_price = _to_decimal(record["selling"])
                product.beginning_stock = _to_decimal(record["beginning"])
                product.stock_qty = _to_decimal(record["stocks"])
                product.is_vat = _parse_bool(record["vat"])
                if existing:
                    updated += 1
                else:
                    db.add(product)
                    created += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"row": line_no, "name": name, "message": str(exc)})
        db.commit()
        result = {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "total": len(rows),
            "filename": file.filename,
        }

    return templates.TemplateResponse(
        "products/import.html",
        {"request": request, "app_name": request.app.title, "user": user, "result": result},
    )


@router.get("/products/import/template")
def download_template(user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(TEMPLATE_HEADERS)

    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill

    # Example rows (delete these before importing your real data).
    ws.append(["Portland Cement 40kg", "Cement", "Bag", 220, 260, 10, 5])
    ws.append(["Common Wire Nail #4", "Fasteners", "Kg", 70, 95, 25.5, 0])

    widths = [26, 16, 12, 14, 14, 24, 12]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="product_import_template.xlsx"'},
    )
