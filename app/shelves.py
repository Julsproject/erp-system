"""Shelves: a simple browsable list of where products physically sit in the
store. Not a lookup-table admin screen for its own sake — the point is being
able to click a shelf and see everything on it, for both the Inventory page
and picking a shelf's items in bulk during Stock Count."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, models
from .database import get_db
from .deps import get_current_user, is_floor_staff, is_staff
from .products import _get_or_create_shelf
from .templating import templates

router = APIRouter()


@router.get("/shelves", response_class=HTMLResponse)
def shelves_list(
    request: Request, db: Session = Depends(get_db), user=Depends(get_current_user),
    error: str = "", name: str = "", merged: str = "",
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)

    counts = dict(
        db.query(models.Product.shelf_id, func.count(models.Product.id))
        .filter(models.Product.is_active.is_(True), models.Product.shelf_id.isnot(None))
        .group_by(models.Product.shelf_id)
        .all()
    )
    shelves = db.query(models.Shelf).order_by(models.Shelf.name).all()
    error_msg = f"“{name}” still has products assigned — move them to another shelf first." if error == "inuse" else ""
    return templates.TemplateResponse(
        "shelves/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "shelves": shelves, "counts": counts, "error": error_msg, "merged": merged},
    )


@router.get("/shelves/merge/search")
def merge_search(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Shelf picker for the Merge Shelves modal — same idea as Inventory's
    product merge search, just over Shelf rows instead."""
    if not user or not is_staff(user):
        return JSONResponse({"shelves": []}, status_code=401)
    q = (q or "").strip()
    if not q:
        return {"shelves": []}
    counts = dict(
        db.query(models.Product.shelf_id, func.count(models.Product.id))
        .filter(models.Product.is_active.is_(True), models.Product.shelf_id.isnot(None))
        .group_by(models.Product.shelf_id)
        .all()
    )
    shelves = (
        db.query(models.Shelf)
        .filter(models.Shelf.name.ilike(f"%{q}%"))
        .order_by(models.Shelf.name)
        .limit(20)
        .all()
    )
    return {"shelves": [{"id": s.id, "name": s.name, "product_count": counts.get(s.id, 0)} for s in shelves]}


@router.post("/shelves/merge")
def merge_shelves(
    request: Request, keep_id: int = Form(...), merge_id: int = Form(...),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Combine two shelves into one: every product on the losing shelf moves
    to the keeper, then the now-empty shelf row is removed outright — unlike
    a product merge there's no separate history to reassign (shelf is just a
    Product attribute, nothing else references it), so once the products are
    moved there's nothing left worth keeping around, same as an empty shelf
    deleted the normal way."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    if keep_id == merge_id:
        return RedirectResponse("/shelves?error=samepick", status_code=302)
    keep = db.get(models.Shelf, keep_id)
    dup = db.get(models.Shelf, merge_id)
    if not keep or not dup:
        return RedirectResponse("/shelves?error=notfound", status_code=302)

    moved = (
        db.query(models.Product)
        .filter(models.Product.shelf_id == dup.id)
        .update({"shelf_id": keep.id}, synchronize_session=False)
    )
    dup_name = dup.name
    db.delete(dup)
    audit.record(
        db, user=user, request=request, action="merge", entity_type="shelf",
        entity_id=keep.id, entity_label=keep.name,
        summary=f"Merged shelf “{dup_name}” into “{keep.name}” — moved {moved} product(s)",
    )
    db.commit()
    return RedirectResponse(f"/shelves?merged={quote(keep.name)}", status_code=302)


@router.post("/shelves")
def create_shelf(request: Request, name: str = Form(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    _get_or_create_shelf(db, name)
    db.commit()
    return RedirectResponse("/shelves", status_code=302)


@router.get("/shelves/{shelf_id:int}", response_class=HTMLResponse)
def shelf_detail(shelf_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    shelf = db.get(models.Shelf, shelf_id)
    if not shelf:
        return RedirectResponse("/shelves", status_code=302)
    products = (
        db.query(models.Product)
        .filter(models.Product.shelf_id == shelf_id, models.Product.is_active.is_(True))
        .order_by(models.Product.name)
        .all()
    )
    return templates.TemplateResponse(
        "shelves/detail.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "shelf": shelf, "products": products},
    )


@router.post("/shelves/{shelf_id:int}/rename")
def rename_shelf(shelf_id: int, request: Request, name: str = Form(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    shelf = db.get(models.Shelf, shelf_id)
    if not shelf:
        return RedirectResponse("/shelves", status_code=302)
    name = (name or "").strip()
    if name:
        shelf.name = name
        db.commit()
    return RedirectResponse(f"/shelves/{shelf_id}", status_code=302)


@router.post("/shelves/{shelf_id:int}/delete")
def delete_shelf(shelf_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    shelf = db.get(models.Shelf, shelf_id)
    if not shelf:
        return RedirectResponse("/shelves", status_code=302)
    # Refuse rather than silently orphaning products off this shelf — the
    # admin should reassign them to another shelf first (or clear it
    # per-product) so nothing's whereabouts quietly goes missing.
    still_assigned = db.query(models.Product.id).filter(models.Product.shelf_id == shelf_id).first()
    if still_assigned:
        return RedirectResponse(f"/shelves?error=inuse&name={quote(shelf.name)}", status_code=302)
    db.delete(shelf)
    db.commit()
    return RedirectResponse("/shelves", status_code=302)
