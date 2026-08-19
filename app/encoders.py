"""Encoders: a managed, permanent list of who actually writes up a sale —
deliberately separate from Users/logins, for a shop where several people
share one POS account. Picked from a dropdown on the Sale form, never typed
freehand, so it can't drift into near-duplicate spellings of the same name.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()


@router.get("/encoders", response_class=HTMLResponse)
def encoders_list(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    counts = dict(
        db.query(models.Sale.encoded_by_id, func.count(models.Sale.id))
        .filter(models.Sale.encoded_by_id.isnot(None))
        .group_by(models.Sale.encoded_by_id)
        .all()
    )
    encoders = db.query(models.Encoder).order_by(models.Encoder.is_active.desc(), models.Encoder.name).all()
    return templates.TemplateResponse(
        "encoders/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "encoders": encoders, "counts": counts},
    )


@router.post("/encoders")
def create_encoder(name: str = Form(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    name = (name or "").strip()
    if name:
        existing = db.query(models.Encoder).filter(func.lower(models.Encoder.name) == name.lower()).first()
        if existing:
            existing.is_active = True  # re-adding a deactivated name brings it back
        else:
            db.add(models.Encoder(name=name))
        db.commit()
    return RedirectResponse("/encoders", status_code=302)


@router.post("/encoders/{encoder_id:int}/rename")
def rename_encoder(encoder_id: int, name: str = Form(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    encoder = db.get(models.Encoder, encoder_id)
    name = (name or "").strip()
    if encoder and name:
        encoder.name = name
        db.commit()
    return RedirectResponse("/encoders", status_code=302)


@router.post("/encoders/{encoder_id:int}/toggle")
def toggle_encoder(encoder_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Deactivate/reactivate rather than delete — past sales keep pointing at
    this name (it's their permanent record of who wrote them up), so the
    name itself is never removed. Deactivating just drops it off the POS
    dropdown for new sales."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    encoder = db.get(models.Encoder, encoder_id)
    if encoder:
        encoder.is_active = not encoder.is_active
        db.commit()
    return RedirectResponse("/encoders", status_code=302)
