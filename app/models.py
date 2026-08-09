"""SQLAlchemy ORM models.

Phase 1:
  Step 1 - User (auth)
  Step 2 - Inventory: Category, UnitType, Product (beginning-inventory encoding)

Multi-unit conversion ("units ladder") and barcode are intentionally deferred;
the Product schema leaves room for them without rework.
"""
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(50), nullable=False, unique=True, index=True)
    full_name = Column(String(100))
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, server_default="cashier")  # admin | cashier
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SubCategory(Base):
    """A finer-grained grouping under a Category (e.g. Category "Paint" ->
    Sub Category "Rollers", "Thinner"). category_id is nullable so a sub
    category can be typed in before its parent Category is chosen — it's
    backfilled the next time it's saved alongside one, same forgiving
    get-or-create pattern as Category and UnitType."""
    __tablename__ = "sub_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    category = relationship("Category")


class UnitType(Base):
    __tablename__ = "unit_types"

    id = Column(Integer, primary_key=True)
    name = Column(String(40), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Shelf(Base):
    """Where a product physically sits in the store (e.g. "Shelf 1", "Shelf
    A") — same simple lookup-table pattern as Category/SubCategory/UnitType,
    used for browsing/filtering rather than anything computed."""
    __tablename__ = "shelves"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Encoder(Base):
    """Who actually wrote up a sale — a managed name list kept deliberately
    separate from Users/logins, for a shop where several people share one
    POS login and want a permanent, consistent (no-typos) record of who
    handled which transaction. Picked from a dropdown on the Sale form,
    never typed freehand, so the list only ever grows on purpose."""
    __tablename__ = "encoders"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    """One row of the Chart of Accounts. `system_key` (not `id`, not `name`)
    is what code refers to — the owner can rename an account or add their
    own sub-accounts freely, but the handful of accounts the posting engine
    actually writes to are pinned by this stable key and can't be deleted."""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    code = Column(String(20), nullable=False, unique=True, index=True)
    name = Column(String(150), nullable=False)
    account_type = Column(String(20), nullable=False)  # asset|liability|equity|revenue|cost_of_sales|expense
    normal_balance = Column(String(6), nullable=False)  # debit|credit
    parent_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    is_system = Column(Boolean, nullable=False, server_default="false")
    system_key = Column(String(40), nullable=True, unique=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    parent = relationship("Account", remote_side=[id])


class JournalEntry(Base):
    """One balanced (debit total == credit total) accounting transaction,
    auto-posted by the ERP or (later) entered by hand. Never edited or
    deleted once posted — a mistake gets a reversing entry instead, so a
    Trial Balance run for a past date always still adds up."""
    __tablename__ = "journal_entries"

    id = Column(Integer, primary_key=True)
    journal_no = Column(String(20), unique=True, index=True)  # JE-000001
    txn_date = Column(Date, nullable=False)
    reference_no = Column(String(60), nullable=True)
    source_type = Column(String(20), nullable=False)  # sale | reversal | manual (more per phase)
    source_id = Column(Integer, nullable=True, index=True)  # polymorphic (e.g. sales.id) — no FK on purpose
    description = Column(String(255))
    status = Column(String(12), nullable=False, server_default="posted")  # posted | reversed
    is_reversal_of_id = Column(Integer, ForeignKey("journal_entries.id"), nullable=True)
    entered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    posted_at = Column(DateTime(timezone=True), server_default=func.now())

    lines = relationship("JournalLine", back_populates="entry", cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("journal_entries.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    debit = Column(Numeric(12, 2), nullable=False, server_default="0")
    credit = Column(Numeric(12, 2), nullable=False, server_default="0")
    memo = Column(String(255), nullable=True)

    entry = relationship("JournalEntry", back_populates="lines")
    account = relationship("Account")


class AccountMapping(Base):
    """Which account an ERP function posts to — e.g. "SALE_CASH" -> Cash on
    Hand. The whole point: change what account a Cash Sale debits from
    Settings, not from code."""
    __tablename__ = "account_mappings"

    id = Column(Integer, primary_key=True)
    function_key = Column(String(40), nullable=False, unique=True)
    label = Column(String(150), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    account = relationship("Account")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, index=True)
    barcode = Column(String(64), nullable=True, unique=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    subcategory_id = Column(Integer, ForeignKey("sub_categories.id"), nullable=True)
    unit_type_id = Column(Integer, ForeignKey("unit_types.id"), nullable=True)
    shelf_id = Column(Integer, ForeignKey("shelves.id"), nullable=True)

    # Manual for now; sourced from the Supplier module later.
    cost_price = Column(Numeric(12, 2), nullable=False, server_default="0")
    selling_price = Column(Numeric(12, 2), nullable=False, server_default="0")

    # Beginning inventory encoding.
    beginning_stock = Column(Numeric(14, 3), nullable=False, server_default="0")
    stock_qty = Column(Numeric(14, 3), nullable=False, server_default="0")

    # Low-stock alert threshold in base units. 0 = no alert for this product.
    reorder_level = Column(Numeric(14, 3), nullable=False, server_default="0")

    # Every product carries THREE selling prices at once, so the shop can quote
    # a different one per customer type without re-pricing the item:
    #   selling_price  the fixed price, typed in directly (the default POS price)
    #   markup_price   cost * (1 + markup_pct/100)   -- % ON TOP OF COST
    #   margin_price   cost / (1 - margin_pct/100)   -- % OF THE SELLING PRICE
    # The two percentages are what the user sets; the prices are derived from
    # cost on save so they stay consistent. Markup and margin are NOT the same:
    # on a 300 cost, 30% markup gives 390 (only a 23.1% margin) while 30% margin
    # gives 428.57.
    markup_pct = Column(Numeric(6, 2), nullable=False, server_default="0")
    markup_price = Column(Numeric(12, 2), nullable=False, server_default="0")
    margin_pct = Column(Numeric(6, 2), nullable=False, server_default="0")
    margin_price = Column(Numeric(12, 2), nullable=False, server_default="0")

    is_vat = Column(Boolean, nullable=False, server_default="false")  # VAT toggle per product
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    category = relationship("Category")
    subcategory = relationship("SubCategory")
    unit_type = relationship("UnitType")
    shelf = relationship("Shelf")
    units = relationship(
        "ProductUnit",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductUnit.sort_order",
    )

    @property
    def total_qty(self):
        return (self.beginning_stock or 0) + (self.stock_qty or 0)

    @property
    def container(self):
        """Broken-bulk view: how many sealed packs + how much sits in the open
        container, expressed in the loose (smallest) unit. Returns None for
        products that aren't sold in both a pack and a loose unit.

        Works whichever unit is the base:
          - base = kg,   ladder Sack = 40      -> loose = kg,   pack = Sack
          - base = Sack, ladder kg   = 0.025   -> loose = kg,   pack = Sack
        The pack is the largest-factor unit, the loose unit the smallest-factor
        one, both measured in base units. With one open bag at a time, the open
        remainder is total mod pack_size, and sealed packs = total // pack_size.
        """
        base_name = self.unit_type.name if self.unit_type else "unit"
        entries = [(base_name, Decimal("1"))]
        for u in self.units:
            f = Decimal(str(u.factor_to_base or 0))
            if f > 0:
                entries.append((u.name, f))
        if len(entries) < 2:
            return None

        loose_name, loose_factor = min(entries, key=lambda e: e[1])
        pack_name, pack_factor = max(entries, key=lambda e: e[1])
        if pack_factor <= loose_factor or pack_factor <= 0 or loose_factor <= 0:
            return None

        total = Decimal(str(self.total_qty or 0))
        sealed = int(total // pack_factor)
        open_base = total - sealed * pack_factor       # remainder in base units
        open_loose = open_base / loose_factor          # expressed in the loose unit
        return {
            "pack_name": pack_name,
            "loose_name": loose_name,
            "sealed": sealed,
            "open": open_loose,
        }

    @property
    def unit_breakdown(self):
        """Cascading breakdown across EVERY defined unit (not just the two
        extremes container() uses) — e.g. base Cubic meter + ELF + ELF 1/2 +
        ELF 1/4 all shown at once, like coin change: as many of the biggest
        unit as fit, then the remainder cascades into the next-biggest, and
        so on down to the smallest, which keeps the leftover. Rounded to 4
        decimal places so a repeating-decimal factor (e.g. 1/0.0465) doesn't
        print a 20-digit number.

        Returns a list of {name, qty} from biggest unit to smallest, or None
        for products with no units ladder at all.
        """
        base_name = self.unit_type.name if self.unit_type else "unit"
        entries = [(base_name, Decimal("1"))]
        for u in self.units:
            f = Decimal(str(u.factor_to_base or 0))
            if f > 0:
                entries.append((u.name, f))
        if len(entries) < 2:
            return None

        entries.sort(key=lambda e: e[1], reverse=True)
        remaining = Decimal(str(self.total_qty or 0))
        breakdown = []
        for i, (name, factor) in enumerate(entries):
            is_last = i == len(entries) - 1
            if is_last:
                qty = (remaining / factor).quantize(Decimal("0.0001"))
            else:
                qty = (remaining // factor)
                remaining -= qty * factor
            breakdown.append({"name": name, "qty": qty})
        return breakdown


class ProductUnit(Base):
    """An extra sellable unit for a product (the 'units ladder').

    factor_to_base = how many base units (the product's Unit Type) make up 1 of
    this unit. e.g. base = kg, Bag => factor_to_base = 50.
    """
    __tablename__ = "product_units"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    name = Column(String(40), nullable=False)
    factor_to_base = Column(Numeric(14, 4), nullable=False, server_default="1")
    price = Column(Numeric(12, 2), nullable=False, server_default="0")  # the "Fixed" price
    sort_order = Column(Integer, nullable=False, server_default="0")

    # Same three-tier pricing idea as Product (see pricing.py) — but scoped
    # to THIS unit's own cost (base cost x factor_to_base), so e.g. a Sack
    # can be priced by margin % independently of how the base is priced.
    markup_pct = Column(Numeric(6, 2), nullable=False, server_default="0")
    markup_price = Column(Numeric(12, 2), nullable=False, server_default="0")
    margin_pct = Column(Numeric(6, 2), nullable=False, server_default="0")
    margin_price = Column(Numeric(12, 2), nullable=False, server_default="0")

    # Optional chain link: this unit's factor can be entered relative to
    # another unit in the same ladder (e.g. "1 Elf Load = 6 Sack") instead of
    # only relative to base. factor_to_base above always stays the resolved,
    # base-relative value — every other reader in the app (POS, purchases,
    # sales, receipts) only ever needs that one number and never has to know
    # a chain exists. relative_to_unit_id/relative_factor exist purely so the
    # product form can redisplay what the user actually typed on next edit.
    relative_to_unit_id = Column(Integer, ForeignKey("product_units.id"), nullable=True)
    relative_factor = Column(Numeric(14, 4), nullable=True)

    product = relationship("Product", back_populates="units")
    relative_to_unit = relationship("ProductUnit", remote_side=[id], foreign_keys=[relative_to_unit_id])

    @property
    def stock_as_this_unit(self):
        """The product's on-hand expressed in THIS unit — "how many Elf can I
        still make out of what's in the yard". Stock itself is only ever stored
        in base units; this is a read-only view of the same number, so it can
        never drift. Rounded to 3 dp because a factor like 8.6 divides into a
        long repeating decimal. None when the factor is unusable (0/blank)."""
        f = Decimal(str(self.factor_to_base or 0))
        if f <= 0:
            return None
        p = self.product
        if p is None:
            return None

        def per(value):
            return (Decimal(str(value or 0)) / f).quantize(Decimal("0.001"))

        return {
            "beginning": per(p.beginning_stock),
            "stock": per(p.stock_qty),
            "total": per(p.total_qty),
        }

    @property
    def cost_as_this_unit(self):
        """Cost of one of this unit (base cost × factor), or None if not costed."""
        f = Decimal(str(self.factor_to_base or 0))
        if f <= 0 or self.product is None:
            return None
        cost = Decimal(str(self.product.cost_price or 0))
        if cost <= 0:
            return None
        return (cost * f).quantize(Decimal("0.01"))


class Sale(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True)
    invoice_no = Column(String(20), unique=True, index=True)
    # Which physical receipt booklet this invoice # was written on (DRS, DRB,
    # SI, ...) — the shop keeps a separate numbered sequence per booklet, so
    # this is what "next invoice #" is computed against (see pos.py's
    # /pos/next-invoice). Free text, not an enum: new booklet types shouldn't
    # need a migration.
    receipt_type = Column(String(10), nullable=True)
    txn_type = Column(String(12), nullable=False, server_default="sale")  # sale | refund | exchange
    original_sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)
    customer_name = Column(String(150))
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    cashier_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Who actually wrote up this sale, picked from the managed Encoders list
    # — distinct from cashier_id (the logged-in account), since one shared
    # login is often used by several people. Optional; blank/NULL means it
    # wasn't noted.
    encoded_by_id = Column(Integer, ForeignKey("encoders.id"), nullable=True)

    subtotal = Column(Numeric(12, 2), nullable=False, server_default="0")
    discount_total = Column(Numeric(12, 2), nullable=False, server_default="0")
    vat_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    net_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    total = Column(Numeric(12, 2), nullable=False, server_default="0")

    payment_method = Column(String(40))
    amount_tendered = Column(Numeric(12, 2), nullable=False, server_default="0")  # paid now (non-receivable)
    change_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    receivable_amount = Column(Numeric(12, 2), nullable=False, server_default="0")  # credit on this sale
    due_date = Column(Date, nullable=True)  # when the credit falls due (sale date + customer terms)

    # True for a refund/exchange where the cashier couldn't find (or the
    # customer didn't have) the original invoice number, so the returned
    # item(s) were picked from an inventory search instead of pulled off a
    # recorded sale line. That means the price came from what the cashier
    # typed, not from a verified original price — worth a quick admin
    # spot-check, which is why these are surfaced in Notifications.
    no_invoice_return = Column(Boolean, nullable=False, server_default="false")

    # A voided sale is a data-entry mistake being erased from every report —
    # stock and payment are reversed at void time (see pos.void_sale), it's
    # never re-editable, and it stays here (not deleted) purely so the
    # invoice # and audit trail don't disappear.
    is_voided = Column(Boolean, nullable=False, server_default="false")
    void_reason = Column(String(255), nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    lines = relationship("SaleLine", back_populates="sale", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="sale", cascade="all, delete-orphan")
    settlements = relationship(
        "ReceivableSettlement", back_populates="sale", cascade="all, delete-orphan",
        foreign_keys="ReceivableSettlement.sale_id",
    )
    cashier = relationship("User", foreign_keys=[cashier_id])
    customer = relationship("Customer")
    voided_by = relationship("User", foreign_keys=[voided_by_id])
    encoded_by = relationship("Encoder")


class SaleLine(Base):
    __tablename__ = "sale_lines"

    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    product_name = Column(String(150), nullable=False)
    unit_name = Column(String(40))
    unit_factor = Column(Numeric(14, 4), nullable=False, server_default="1")
    qty = Column(Numeric(14, 3), nullable=False, server_default="0")
    unit_price = Column(Numeric(12, 2), nullable=False, server_default="0")
    discount = Column(Numeric(12, 2), nullable=False, server_default="0")
    line_total = Column(Numeric(12, 2), nullable=False, server_default="0")
    is_vat = Column(Boolean, nullable=False, server_default="false")

    # Cost per BASE unit captured at the moment of sale, so profit stays correct
    # even after a supplier price change. Cost of goods = qty * unit_factor * unit_cost.
    unit_cost = Column(Numeric(12, 2), nullable=False, server_default="0")

    # Which of the product's three prices was charged: fixed | markup | margin
    # (blank for a ladder unit, which carries its own single price). Recorded so
    # two sales of the same item at different prices can be told apart later.
    price_tier = Column(String(10), server_default="fixed")

    sale = relationship("Sale", back_populates="lines")


class Quotation(Base):
    """A price estimate given to a customer, before it becomes a real sale.

    Lifecycle: pending -> confirmed -> paid (converts into a Sale), or
    pending/confirmed -> cancelled. Nothing here touches stock or costing
    until it is converted — a quotation is just a promise of a price.
    """
    __tablename__ = "quotations"

    id = Column(Integer, primary_key=True)
    quote_no = Column(String(20), unique=True, index=True)
    status = Column(String(12), nullable=False, server_default="pending")  # pending | confirmed | paid | cancelled

    customer_name = Column(String(150))
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)

    vat_applied = Column(Boolean, nullable=False, server_default="false")
    subtotal = Column(Numeric(12, 2), nullable=False, server_default="0")
    discount_total = Column(Numeric(12, 2), nullable=False, server_default="0")
    vat_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    total = Column(Numeric(12, 2), nullable=False, server_default="0")

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    # Set once the quotation turns into a real, paid sale.
    converted_sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)

    lines = relationship("QuotationLine", back_populates="quotation", cascade="all, delete-orphan")
    customer = relationship("Customer")
    creator = relationship("User")
    converted_sale = relationship("Sale")


class QuotationLine(Base):
    __tablename__ = "quotation_lines"

    id = Column(Integer, primary_key=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    product_name = Column(String(150), nullable=False)
    unit_name = Column(String(40))
    unit_factor = Column(Numeric(14, 4), nullable=False, server_default="1")
    qty = Column(Numeric(14, 3), nullable=False, server_default="0")
    unit_price = Column(Numeric(12, 2), nullable=False, server_default="0")
    discount = Column(Numeric(12, 2), nullable=False, server_default="0")
    line_total = Column(Numeric(12, 2), nullable=False, server_default="0")

    quotation = relationship("Quotation", back_populates="lines")
    product = relationship("Product")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, index=True)
    tin = Column(String(30))
    address = Column(String(255))
    # Credit terms in days; a sale on credit falls due this many days after the sale.
    credit_days = Column(Integer, nullable=False, server_default="15")
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    method = Column(String(20), nullable=False)  # cash | gcash | card | bank_transfer | receivable
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")

    sale = relationship("Sale", back_populates="payments")


class ReceivableSettlement(Base):
    """A payment collected against a sale's outstanding credit."""
    __tablename__ = "receivable_settlements"

    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    method = Column(String(20), nullable=False)  # cash | gcash | card | bank_transfer | cheque | credit_note
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    bank = Column(String(60))          # for cheque
    cheque_no = Column(String(40))     # for cheque
    cheque_date = Column(String(20))   # for cheque (kept as text for now)
    # Set only when method is "credit_note" — the return/exchange sale whose
    # value was applied here instead of paid in cash, so a customer's
    # statement can point at exactly which transaction did it.
    source_sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)
    cashier_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sale = relationship("Sale", back_populates="settlements", foreign_keys=[sale_id])
    source_sale = relationship("Sale", foreign_keys=[source_sale_id])


class PostDatedCheque(Base):
    """A post-dated cheque — received from a customer settling their credit,
    or issued to pay a supplier. The money it represents stays in limbo
    (status=pending) until the bank actually honors it on/after cheque_date:
      cleared  -> received: creates the ReceivableSettlement now (credit
                  finally goes down); issued: the purchase is marked paid now.
      bounced  -> received: nothing to reverse, since it was never applied;
                  issued: the purchase stays unpaid.
      cancelled -> the cheque was returned/replaced before ever being deposited.
    """
    __tablename__ = "post_dated_cheques"

    id = Column(Integer, primary_key=True)
    direction = Column(String(10), nullable=False)              # received | issued
    status = Column(String(12), nullable=False, server_default="pending")  # pending | cleared | bounced | cancelled

    bank = Column(String(60))
    cheque_no = Column(String(40))
    cheque_date = Column(Date, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    notes = Column(String(255))

    # Received: which sale/customer this is settling.
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    settlement_id = Column(Integer, ForeignKey("receivable_settlements.id"), nullable=True)

    # Issued: which purchase/supplier this is paying.
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    sale = relationship("Sale")
    customer = relationship("Customer")
    settlement = relationship("ReceivableSettlement")
    purchase = relationship("Purchase")
    supplier = relationship("Supplier")
    creator = relationship("User")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True)
    code = Column(String(30), unique=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    contact_person = Column(String(120))
    mobile = Column(String(40))
    telephone = Column(String(40))
    email = Column(String(120))
    address = Column(String(255))
    tin = Column(String(30))
    payment_terms = Column(String(60))     # free-text note, e.g. "50% DP" — not used for date math
    # Structured terms, same idea as Customer.credit_days: how many days after
    # a delivery is confirmed before it's due for payment. Drives Payables
    # aging (due-soon/overdue), which free text can't be computed from.
    payment_days = Column(Integer, nullable=False, server_default="30")
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Purchase(Base):
    """A receiving (goods in) or a purchase return (goods back to supplier).

    A receive-type purchase goes through a status lifecycle, mirroring
    Quotations: pending (PO raised, nothing in stock yet) -> confirmed (goods
    physically arrived — stock added and cost updated right here) -> paid
    (payment settled with the supplier afterward; no further stock effect).
    A return has no staging — it takes effect immediately, same as before.
    """
    __tablename__ = "purchases"

    id = Column(Integer, primary_key=True)
    ref_no = Column(String(30), unique=True, index=True)
    txn_type = Column(String(12), nullable=False, server_default="receive")  # receive | return
    status = Column(String(12), nullable=False, server_default="pending")   # pending | confirmed | paid | cancelled
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    invoice_no = Column(String(40))        # supplier's invoice / DR number
    delivery_date = Column(String(20))     # as printed on the DR
    notes = Column(String(255))
    total = Column(Numeric(12, 2), nullable=False, server_default="0")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    payment_method = Column(String(20), nullable=True)   # cash | bank_transfer | cheque | gcash | other
    paid_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    # When this becomes payable — set once, at confirm time, to
    # confirmed_at + the supplier's payment_days (same idea as a Sale's
    # due_date). A 'confirmed' receive-type purchase IS the payable: goods
    # and cost already moved, payment hasn't. Never set for pending (nothing
    # owed yet) or return (no payment involved).
    due_date = Column(Date, nullable=True)

    # For a return: which delivery it's being sent back from.
    original_purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=True)

    supplier = relationship("Supplier")
    user = relationship("User")
    lines = relationship("PurchaseLine", back_populates="purchase", cascade="all, delete-orphan")
    original_purchase = relationship("Purchase", remote_side=[id])
    settlements = relationship("PurchaseSettlement", back_populates="purchase", cascade="all, delete-orphan")


class PurchaseSettlement(Base):
    """A payment made against a purchase's outstanding payable — the AP
    mirror of ReceivableSettlement. Lets a payable be paid off in more than
    one installment instead of all-or-nothing."""
    __tablename__ = "purchase_settlements"

    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=False)
    method = Column(String(20), nullable=False)  # cash | bank_transfer | cheque | gcash | other
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    bank = Column(String(60))          # for cheque
    cheque_no = Column(String(40))     # for cheque
    cheque_date = Column(String(20))   # for cheque (kept as text, same as ReceivableSettlement)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase = relationship("Purchase", back_populates="settlements")


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"

    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    product_name = Column(String(150), nullable=False)
    unit_name = Column(String(40))
    unit_factor = Column(Numeric(14, 4), nullable=False, server_default="1")
    qty = Column(Numeric(14, 3), nullable=False, server_default="0")
    unit_cost = Column(Numeric(12, 2), nullable=False, server_default="0")   # cost per purchase unit
    line_total = Column(Numeric(12, 2), nullable=False, server_default="0")

    # Cost history: what the product's per-base cost was before/after this line.
    old_cost = Column(Numeric(12, 4), server_default="0")
    new_cost = Column(Numeric(12, 4), server_default="0")

    purchase = relationship("Purchase", back_populates="lines")
    product = relationship("Product")


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty_base = Column(Numeric(14, 3), nullable=False)  # signed: negative = out
    reason = Column(String(30), nullable=False)         # sale | adjustment | ...
    ref = Column(String(30))
    # Peso valuation — only populated for "adjustment"/"stock_count" movements
    # (sale/purchase/refund/etc already have their own costing elsewhere, so
    # valuing them again here would double-count). unit_cost is the product's
    # cost_price at the moment of the movement; value = qty_base * unit_cost,
    # so a negative value is shrinkage (loss) and a positive one is a find (gain).
    unit_cost = Column(Numeric(12, 2), nullable=True)
    value = Column(Numeric(14, 2), nullable=True)
    note = Column(String(255), nullable=True)           # reason code, e.g. "Damage / breakage"
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MonthEndRolloverLine(Base):
    """One row per product per month-end rollover — the exact amount that
    moved from Stocks Qty into Actual Beginning that month. Kept separate
    from StockMovement because a rollover doesn't change total on-hand
    (it's a reclassification, beginning += stock_qty), so it isn't a real
    "in" or "out" the way StockMovement.qty_base means everywhere else."""
    __tablename__ = "month_end_rollover_lines"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_name = Column(String(150), nullable=False)
    period = Column(String(7), nullable=False)  # "2026-09"
    qty_moved = Column(Numeric(14, 3), nullable=False)
    old_beginning = Column(Numeric(14, 3), nullable=False)
    new_beginning = Column(Numeric(14, 3), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product")


class StockCount(Base):
    """A physical inventory / cycle count session: scan items on the shelf,
    the system compares against what it thinks is on hand, and Complete
    applies the difference as a stock correction. Delta-based (not an
    absolute overwrite), so it stays correct even if sales happen elsewhere
    on the system while the count is in progress."""
    __tablename__ = "stock_counts"

    id = Column(Integer, primary_key=True)
    ref_no = Column(String(20), unique=True)
    status = Column(String(20), nullable=False, server_default="open")  # open | completed | cancelled
    notes = Column(String(255))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_by = Column(Integer, ForeignKey("users.id"))
    completed_at = Column(DateTime(timezone=True))

    lines = relationship("StockCountLine", back_populates="stock_count", cascade="all, delete-orphan")


class StockCountLine(Base):
    __tablename__ = "stock_count_lines"

    id = Column(Integer, primary_key=True)
    stock_count_id = Column(Integer, ForeignKey("stock_counts.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_name = Column(String(150), nullable=False)
    # Snapshotted the moment this product was first scanned into the count —
    # what the system said was on hand at that instant. The eventual
    # adjustment is (counted - system_qty), applied as a delta, not as an
    # overwrite of whatever stock_qty happens to be at Complete time.
    system_qty = Column(Numeric(14, 3), nullable=False)
    counted_qty = Column(Numeric(14, 3), nullable=False, server_default="0")
    # Optional per-unit breakdown of how counted_qty was arrived at — e.g.
    # {"FORWARD": "2", "Elf": "3", "Elf 1/2": "1"} for a product with a units
    # ladder, so staff can count physical containers instead of doing the
    # base-unit math themselves. JSON text (same convention as audit_log);
    # counted_qty is always the resolved total and stays the one number
    # variance/completion logic reads — this is purely for redisplay.
    unit_breakdown = Column(Text, nullable=True)
    first_scanned_at = Column(DateTime(timezone=True), server_default=func.now())

    stock_count = relationship("StockCount", back_populates="lines")
    product = relationship("Product")


class ExpenseCategory(Base):
    """Create-your-own, same idea as Category/UnitType on Products."""
    __tablename__ = "expense_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Expense(Base):
    """A business expense already paid — rent, utilities, salaries, etc.

    Unlike Purchases this has no pending/confirmed staging: recording one here
    means the money is already out the door. A cheque used to pay one is just
    a reference detail, not a post-dated PDC — expenses this small a shop pays
    by cheque are typically cut and cleared same-day, not held in limbo.
    """
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True)
    ref_no = Column(String(20), unique=True, index=True)   # EXP-000001
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=True)
    payee = Column(String(150))                # who got paid — vendor, landlord, employee...
    description = Column(String(255))
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    expense_date = Column(Date, nullable=False)
    payment_method = Column(String(20), nullable=False, server_default="cash")  # cash|gcash|bank_transfer|cheque
    reference_no = Column(String(60))           # OR#, cheque #, transfer ref — freeform
    notes = Column(String(255))
    is_voided = Column(Boolean, nullable=False, server_default="false")

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    category = relationship("ExpenseCategory")
    creator = relationship("User")


class Delivery(Base):
    """Fulfillment of an already-completed Sale: pending (scheduled) ->
    out_for_delivery (driver has it) -> delivered, or -> cancelled.
    Doesn't touch stock — that already happened when the Sale was made.
    """
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True)
    delivery_no = Column(String(20), unique=True, index=True)   # DEL-000001
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    status = Column(String(20), nullable=False, server_default="pending")  # pending|out_for_delivery|delivered|cancelled

    recipient_name = Column(String(150))
    address = Column(String(255))
    contact_no = Column(String(40))
    driver_name = Column(String(100))
    vehicle = Column(String(60))
    scheduled_date = Column(Date, nullable=True)
    notes = Column(String(255))

    # --- Cash on Delivery -------------------------------------------------
    # COD lives on the delivery, not on the Sale: a walk-in POS sale is paid
    # at the counter, so "collect on handover" only makes sense once there is
    # something to hand over. The sale itself is rung up as a Receivable; this
    # flag marks that the balance is meant to be collected by the driver on
    # delivery rather than chased as ordinary credit.
    is_cod = Column(Boolean, nullable=False, server_default="false")
    cod_amount = Column(Numeric(12, 2), nullable=False, server_default="0")   # expected at scheduling
    collected_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    collected_method = Column(String(20))          # cash | gcash | card | bank_transfer
    collected_at = Column(DateTime(timezone=True), nullable=True)
    settlement_id = Column(Integer, ForeignKey("receivable_settlements.id"), nullable=True)

    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sale = relationship("Sale")
    creator = relationship("User")
    settlement = relationship("ReceivableSettlement")


class BankAccount(Base):
    """A bank account (or labeled cash box) whose balance the app tracks.
    Balance is never stored — it's opening_balance plus the sum of its
    BankTransactions, computed on the fly, same idea as how a sale's
    outstanding credit is derived rather than cached.
    """
    __tablename__ = "bank_accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)   # e.g. "BDO Checking - 1234"
    bank_name = Column(String(80))
    account_no = Column(String(60))
    opening_balance = Column(Numeric(12, 2), nullable=False, server_default="0")
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    transactions = relationship("BankTransaction", back_populates="account", cascade="all, delete-orphan")


class BankTransaction(Base):
    """A deposit into or withdrawal from a BankAccount. Moving money between
    two accounts is just a withdrawal on one and a deposit on the other —
    no separate 'transfer' type needed.
    """
    __tablename__ = "bank_transactions"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("bank_accounts.id"), nullable=False)
    txn_type = Column(String(12), nullable=False)   # deposit | withdrawal
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    txn_date = Column(Date, nullable=False)
    description = Column(String(255))
    reference_no = Column(String(60))
    is_voided = Column(Boolean, nullable=False, server_default="false")

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("BankAccount", back_populates="transactions")
    creator = relationship("User")


class AppSetting(Base):
    """A single editable configuration value, keyed by name.

    Lets the owner change things (business name, receipt details, …) from an
    in-app Settings screen instead of editing the `.env` file and rebuilding.
    A key-value row keeps the schema open: adding a new setting is a new row,
    never a migration. `.env` remains the source for infrastructure config
    (database URL, secret key) — these are the user-facing display settings.
    """
    __tablename__ = "app_settings"

    key = Column(String(60), primary_key=True)
    value = Column(String(500))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    """A who-did-what record of every meaningful change in the system.

    The rest of the app tracks *who created* a thing (created_by / cashier_id)
    but not who later edited, voided, cancelled or adjusted it — this table
    closes that gap. Each row is one action: the actor, when, what entity, a
    plain-language summary, and (for edits) a field-by-field before -> after
    diff kept as a JSON string.

    Written explicitly from each route via `audit.record(...)` rather than by a
    magic ORM hook, so the actor and request context are always attached and
    the log reads like the code that produced it. `username` is snapshotted so
    a later rename/deletion of the user never rewrites history.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # null = system / pre-auth event
    username = Column(String(50))                    # snapshot of who acted
    action = Column(String(30), nullable=False, index=True)   # create|update|void|cancel|adjust_stock|login|...
    entity_type = Column(String(30), nullable=False, index=True)  # product|expense|delivery|user|auth|...
    entity_id = Column(Integer, nullable=True)
    entity_label = Column(String(150))               # human name: product name, invoice no, …
    summary = Column(String(300))                    # one-line description of what happened
    changes = Column(Text)                           # JSON: {field: [old, new], …}, for edits
    ip = Column(String(45))                          # request client IP, when available

    user = relationship("User")


class CashierShift(Base):
    """One cash-drawer session: a cashier declares how much cash they're
    starting with, and — separately, whenever they stop — counts what's
    actually in the drawer so a shortage or overage is caught the same day
    rather than discovered weeks later.

    This is deliberately NOT a replacement for the automatic Cashier Activity
    history (which already derives what happened from Sales/Payments) — it's
    the physical-cash side of the same day: Activity says what *should* be
    true, this records what *actually counted out* true.

    A shift's window is [opened_at, closed_at or now()); `expected_cash` is
    computed from that window (see shifts.expected_cash_for) and frozen onto
    the row at close time so the reviewed number never silently drifts if
    something is edited/voided afterward.
    """
    __tablename__ = "cashier_shifts"

    id = Column(Integer, primary_key=True)
    cashier_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(10), nullable=False, server_default="open")  # open | closed

    opening_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    opened_at = Column(DateTime(timezone=True), server_default=func.now())

    # Filled in at close time.
    expected_cash = Column(Numeric(12, 2), nullable=True)   # opening + net cash movement, computed
    counted_cash = Column(Numeric(12, 2), nullable=True)    # physically counted by the cashier
    variance = Column(Numeric(12, 2), nullable=True)        # counted - expected (negative = short)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    # Optional: cash physically moved to the bank at close. Documents where the
    # counted cash went; does not affect the variance above (that's measured
    # before any deposit is taken out).
    deposited_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    bank_account_id = Column(Integer, ForeignKey("bank_accounts.id"), nullable=True)
    bank_transaction_id = Column(Integer, ForeignKey("bank_transactions.id"), nullable=True)

    notes = Column(String(255))

    cashier = relationship("User")
    bank_account = relationship("BankAccount")
    bank_transaction = relationship("BankTransaction")


class Notification(Base):
    """One entry in the Notifications Center — a centralized inbox for the
    alerts that otherwise only surface as nav badges and the Dashboard's
    "Needs your attention" widget (low/out of stock, overdue & due-soon
    credits, cheques due, pending deliveries, stale backup, below-cost pricing).

    These conditions are *derived* from live data, so a periodic sweep
    (`notifications.sync_notifications`) reconciles them into rows:
      - a condition that appears creates an unread row,
      - a condition that clears marks its row resolved (kept as history),
      - a condition that recurs after resolving creates a fresh unread row.
    `dedupe_key` is the stable identity of the underlying condition
    (e.g. "stock_out:12"), so the sweep never duplicates an open alert.
    """
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    dedupe_key = Column(String(80), nullable=False, index=True)  # e.g. "credit_overdue:42"
    category = Column(String(20), nullable=False)   # stock | pricing | credit | cheque | delivery | backup
    severity = Column(String(10), nullable=False, server_default="info")  # info | warning | danger
    title = Column(String(150), nullable=False)
    body = Column(String(300))
    link = Column(String(120))                      # where clicking the alert goes

    is_read = Column(Boolean, nullable=False, server_default="false")
    is_resolved = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    read_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
