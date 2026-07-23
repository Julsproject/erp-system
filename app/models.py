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


class UnitType(Base):
    __tablename__ = "unit_types"

    id = Column(Integer, primary_key=True)
    name = Column(String(40), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    unit_type_id = Column(Integer, ForeignKey("unit_types.id"), nullable=True)

    # Manual for now; sourced from the Supplier module later.
    cost_price = Column(Numeric(12, 2), nullable=False, server_default="0")
    selling_price = Column(Numeric(12, 2), nullable=False, server_default="0")

    # Beginning inventory encoding.
    beginning_stock = Column(Numeric(14, 3), nullable=False, server_default="0")
    stock_qty = Column(Numeric(14, 3), nullable=False, server_default="0")

    # Low-stock alert threshold in base units. 0 = no alert for this product.
    reorder_level = Column(Numeric(14, 3), nullable=False, server_default="0")

    is_vat = Column(Boolean, nullable=False, server_default="false")  # VAT toggle per product
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    category = relationship("Category")
    unit_type = relationship("UnitType")
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
    price = Column(Numeric(12, 2), nullable=False, server_default="0")
    sort_order = Column(Integer, nullable=False, server_default="0")

    product = relationship("Product", back_populates="units")


class Sale(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True)
    invoice_no = Column(String(20), unique=True, index=True)
    txn_type = Column(String(12), nullable=False, server_default="sale")  # sale | refund | exchange
    original_sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True)
    customer_name = Column(String(150))
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    cashier_id = Column(Integer, ForeignKey("users.id"), nullable=True)

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

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    lines = relationship("SaleLine", back_populates="sale", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="sale", cascade="all, delete-orphan")
    settlements = relationship("ReceivableSettlement", back_populates="sale", cascade="all, delete-orphan")
    cashier = relationship("User")
    customer = relationship("Customer")


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
    method = Column(String(20), nullable=False)  # cash | gcash | card | bank_transfer | cheque
    amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    bank = Column(String(60))          # for cheque
    cheque_no = Column(String(40))     # for cheque
    cheque_date = Column(String(20))   # for cheque (kept as text for now)
    cashier_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sale = relationship("Sale", back_populates="settlements")


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
    payment_terms = Column(String(60))     # e.g. COD, 30 days, 50% DP
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

    # For a return: which delivery it's being sent back from.
    original_purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=True)

    supplier = relationship("Supplier")
    user = relationship("User")
    lines = relationship("PurchaseLine", back_populates="purchase", cascade="all, delete-orphan")
    original_purchase = relationship("Purchase", remote_side=[id])


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
    created_at = Column(DateTime(timezone=True), server_default=func.now())


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
