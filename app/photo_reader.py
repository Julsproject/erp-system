"""Reads a photo of a paper document — a customer's DR/SI for the POS, or a
supplier's delivery slip for New Purchase — with Claude, and matches each
line to an item in Inventory. Nothing is saved here: the POS cart or the
New Purchase form is filled from the result, and the cashier checks it
against the paper before completing the sale or saving the purchase.

Needs ANTHROPIC_API_KEY in the app's environment (.env). Each photo is one
Messages API call; the item list is sent as a cached system block, so only
the first photo in an hour pays for it in full."""
import base64
import io
import json
import logging
import os
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from . import models, settings_store

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_SIDE_PX = 2000          # phone photos are resized to this before sending


class PhotoReadError(Exception):
    """Something the cashier can act on — shown as-is on the page."""


def is_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


_NUMBER_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_no": {"type": "string"},
        "date": {"type": "string"},
        "party_name": {"type": "string"},
        "written_total": _NUMBER_OR_NULL,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "qty": _NUMBER_OR_NULL,
                    "unit": {"type": "string"},
                    "written_text": {"type": "string"},
                    "unit_price": _NUMBER_OR_NULL,
                    "amount": _NUMBER_OR_NULL,
                    "product_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
                "required": ["qty", "unit", "written_text", "unit_price", "amount", "product_id", "confidence", "note"],
                "additionalProperties": False,
            },
        },
        "skipped": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_no", "date", "party_name", "written_total", "lines", "skipped"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You read photos of paper documents from {shop}, a hardware store in the Philippines, and match every line to the store's item list.

How to read the document:
- Read every item line, top to bottom. Leave out lines that are crossed out or marked N/S, no stock or cancelled, and list each of them in "skipped" with a few words on why.
- qty and unit as written (pcs, box, gal, pair, m, kg...). unit_price is the price for one unit as written; when only the line amount is written, give the amount and leave unit_price null. Copy numbers exactly as written, never adjusted to make the total work.
- written_text is the line's description as written, so a person can compare it with the paper.
- written_total is the grand total written on the document, or null if there is none.
- doc_no is the printed or written document number (digits only, no "No."). date is YYYY-MM-DD; dates here are written month/day/year, and a two-digit year means 20YY. party_name is the other party's name as written; leave any of these "" when it isn't on the page.

How to match items:
- product_id is the id from the item list for the same item: same size, gauge, thickness, length, color and brand when the line says them. Shorthand the store uses: MPW = marine plywood, CB = circuit breaker, BON / bolt = bolt-on, G.E = GE, ord = ordinary, TG = Topgrade, FO = orange PVC fitting, B. rivet = blind rivet, sq. bar = square bar, G-26 = gauge 26.
- The list's price helps confirm a match, but a different price on the paper is normal and doesn't rule a match out.
- confidence: "high" when it can only be that item, "medium" when it's the likeliest of a few, "low" for a guess. Use null when nothing on the list fits; don't force a match.
- note: one short sentence when something needs a person's eye (an unclear word, two items that could fit, an odd quantity), else ""."""

_KIND = {
    "sale": {
        "price_label": "selling price",
        "ask": "This is one of our own sales documents (a Delivery Receipt or Sales Invoice) given to a customer. party_name is the customer (the \"Delivered to\" / \"Sold to\" name).",
    },
    "purchase": {
        "price_label": "current cost",
        "ask": "This is a supplier's delivery document for goods we bought. party_name is the supplier (the store that issued it) — not {shop}, which is the buyer.",
    },
}


def _catalog_text(db: Session, kind: str) -> tuple[str, dict]:
    products = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True))
        .order_by(models.Product.id)   # stable order keeps the cached prompt identical between photos
        .all()
    )
    rows = []
    for p in products:
        price = p.selling_price if kind == "sale" else p.cost_price
        unit = p.unit_type.name if p.unit_type else ""
        rows.append(f"{p.id} | {' '.join(p.name.split())} | {unit} | {Decimal(str(price or 0)):.2f}")
    return "\n".join(rows), {p.id: p for p in products}


def _prepare_image(data: bytes) -> str:
    """Upright, at most MAX_SIDE_PX on its longest side, as base64 JPEG."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise PhotoReadError("That photo couldn't be opened. Use a JPG or PNG photo (an iPhone HEIC photo has to be "
                             "taken in \"Most Compatible\" format, or sent as JPG).")
    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return base64.standard_b64encode(out.getvalue()).decode("ascii")


def _money(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def read_document(db: Session, data: bytes, kind: str) -> dict:
    """Returns {"doc_no", "date", "party_name", "written_total", "lines",
    "skipped", "computed_total"}; each line carries "product" (the matched
    models.Product, or None). Raises PhotoReadError."""
    if kind not in _KIND:
        raise ValueError(kind)
    if not is_configured():
        raise PhotoReadError("Photo reading isn't set up yet: the ANTHROPIC_API_KEY setting is missing. "
                             "Ask whoever manages the system to add it.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoReadError("That photo is too big — it should be under 15 MB.")
    image_b64 = _prepare_image(data)

    import anthropic

    shop = (settings_store.get_setting(db, "business_name") or "the store").strip()
    catalog, products = _catalog_text(db, kind)
    system = [
        {"type": "text", "text": _INSTRUCTIONS.format(shop=shop)},
        {"type": "text",
         "text": f"Item list — id | name | unit | {_KIND[kind]['price_label']}:\n{catalog}",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]
    client = anthropic.Anthropic(timeout=180.0)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
            system=system,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": _KIND[kind]["ask"].format(shop=shop) + " Read it and match the items."},
            ]}],
        )
    except anthropic.AuthenticationError:
        raise PhotoReadError("The photo reader's API key was not accepted. Ask whoever manages the system to check ANTHROPIC_API_KEY.")
    except anthropic.PermissionDeniedError:
        raise PhotoReadError("The photo reader's API key isn't allowed to do this. Check the Anthropic account.")
    except anthropic.RateLimitError:
        raise PhotoReadError("Too many photos at once — wait a minute and try again.")
    except anthropic.BadRequestError as e:
        log.warning("photo read rejected: %s", e)
        raise PhotoReadError("The photo reader turned this request down. Try a clearer photo, or encode it by hand.")
    except anthropic.APIStatusError as e:
        log.warning("photo read failed: %s", e)
        raise PhotoReadError("The photo reader is having trouble right now. Try again in a few minutes.")
    except anthropic.APIConnectionError:
        raise PhotoReadError("Couldn't reach the photo reader — check the internet connection and try again.")

    usage = response.usage
    log.info("photo read %s: in=%s cache_read=%s cache_write=%s out=%s", kind, usage.input_tokens,
             usage.cache_read_input_tokens, usage.cache_creation_input_tokens, usage.output_tokens)
    if response.stop_reason == "refusal":
        raise PhotoReadError("The photo reader declined this photo. Encode it by hand instead.")
    if response.stop_reason == "max_tokens":
        raise PhotoReadError("That document has too many lines to read in one photo — photograph it in two halves.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise PhotoReadError("The photo couldn't be read. Try again with a clearer, straight-on photo.")

    lines = []
    computed = Decimal("0")
    for raw in parsed.get("lines") or []:
        qty = _money(raw.get("qty"))
        unit_price = _money(raw.get("unit_price"))
        amount = _money(raw.get("amount"))
        if unit_price is None and amount is not None and qty:
            unit_price = (amount / qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if qty is not None and unit_price is not None:
            computed += (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        product = products.get(raw.get("product_id")) if raw.get("product_id") is not None else None
        lines.append({
            "qty": qty, "unit": (raw.get("unit") or "").strip(), "written_text": (raw.get("written_text") or "").strip(),
            "unit_price": unit_price, "amount": amount, "product": product,
            "confidence": raw.get("confidence") or "low", "note": (raw.get("note") or "").strip(),
        })
    return {
        "doc_no": (parsed.get("doc_no") or "").strip(),
        "date": (parsed.get("date") or "").strip(),
        "party_name": (parsed.get("party_name") or "").strip(),
        "written_total": _money(parsed.get("written_total")),
        "computed_total": computed,
        "lines": lines,
        "skipped": [s for s in (parsed.get("skipped") or []) if s],
    }


def total_warning(result: dict) -> str | None:
    """The lines read should add up to the total written on the paper — when
    they don't, a number was probably misread."""
    written, computed = result["written_total"], result["computed_total"]
    if written is None or abs(written - computed) < Decimal("0.05"):
        return None
    return (f"The lines read add up to ₱{computed:,.2f}, but the total written on the paper is ₱{written:,.2f}. "
            "A quantity or price was probably misread — compare each line with the paper.")
