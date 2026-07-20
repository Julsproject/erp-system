"""SQLAlchemy ORM models.

Phase 1:
  Step 1 - User (auth)
  Step 2 - Inventory: Category, UnitType, Product (beginning-inventory encoding)

Multi-unit conversion ("units ladder") and barcode are intentionally deferred;
the Product schema leaves room for them without rework.
"""
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
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
    receivable_amount = Column(Numeric(12, 2), nullable=False, server_default="0")  # utang on this sale

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

    sale = relationship("Sale", back_populates="lines")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, index=True)
    tin = Column(String(30))
    address = Column(String(255))
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
    """A payment collected against a sale's outstanding utang."""
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


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty_base = Column(Numeric(14, 3), nullable=False)  # signed: negative = out
    reason = Column(String(30), nullable=False)         # sale | adjustment | ...
    ref = Column(String(30))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
