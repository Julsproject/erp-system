"""Shared letterhead builder for the simple downloadable PDFs (receipt,
credit statement, purchase order) — business details on one side, document
info on the other, then who it's for/from underneath."""
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

INK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#64748b")
ACCENT = colors.HexColor("#1F6FEB")


def letterhead(biz: dict, doc_label: str, doc_meta: list, party_label: str, party_lines: list):
    """Returns a list of flowables: a two-column header (doc info left,
    business details right) under a rule, then a 'Customer:'/'Supplier:'
    line if party_lines is given."""
    styles = getSampleStyleSheet()
    label_style = ParagraphStyle("DocLabel", parent=styles["Heading2"], textColor=ACCENT, spaceAfter=3)
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], fontSize=9.5, textColor=MUTED, leading=13)
    biz_name_style = ParagraphStyle("BizName", parent=styles["Heading2"], alignment=TA_RIGHT, textColor=INK)
    biz_sub_style = ParagraphStyle("BizSub", parent=styles["Normal"], fontSize=9, alignment=TA_RIGHT, textColor=MUTED, leading=12)

    left = [Paragraph(doc_label, label_style)]
    for line in doc_meta:
        left.append(Paragraph(line, meta_style))

    right = [Paragraph(biz.get("business_name") or "", biz_name_style)]
    if biz.get("receipt_address"):
        right.append(Paragraph(biz["receipt_address"], biz_sub_style))
    if biz.get("receipt_contact"):
        right.append(Paragraph(biz["receipt_contact"], biz_sub_style))
    if biz.get("receipt_tin"):
        right.append(Paragraph(f"TIN: {biz['receipt_tin']}", biz_sub_style))

    head = Table([[left, right]], colWidths=[280, 260])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, INK),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))

    elements = [head, Spacer(1, 12)]

    if party_lines:
        party_style = ParagraphStyle("Party", parent=styles["Normal"], fontSize=10, leading=14)
        elements.append(Paragraph(f"<b>{party_label}:</b> " + " &nbsp;·&nbsp; ".join(party_lines), party_style))
        elements.append(Spacer(1, 12))

    return elements
