"""Accounting: Chart of Accounts + Journal Entries + the ERP-function ->
account mapping table that drives automatic posting. Phase 1 only wires
Sales; Purchases/Expenses/Banking are deferred to later migrations.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


# (code, name, account_type, normal_balance, system_key)
SEED_ACCOUNTS = [
    ("1000", "Cash on Hand", "asset", "debit", "CASH_ON_HAND"),
    ("1010", "GCash", "asset", "debit", "GCASH"),
    ("1020", "Bank Accounts", "asset", "debit", "BANK"),
    ("1100", "Accounts Receivable", "asset", "debit", "AR"),
    ("1200", "Inventory - Merchandise", "asset", "debit", "INVENTORY_MERCHANDISE"),
    ("1300", "Input VAT", "asset", "debit", "INPUT_VAT"),
    ("1900", "Inventory Adjustment", "asset", "debit", "INVENTORY_ADJUSTMENT"),
    ("2000", "Accounts Payable", "liability", "credit", "AP"),
    ("2100", "Output VAT", "liability", "credit", "OUTPUT_VAT"),
    ("2200", "VAT Payable", "liability", "credit", "VAT_PAYABLE"),
    ("3000", "Owner's Equity", "equity", "credit", "OWNERS_EQUITY"),
    ("3100", "Retained Earnings", "equity", "credit", "RETAINED_EARNINGS"),
    ("4000", "Sales - Hardware", "revenue", "credit", "SALES_REVENUE"),
    ("4100", "Sales Returns & Allowances", "revenue", "debit", "SALES_RETURNS"),
    ("5000", "Cost of Sales", "cost_of_sales", "debit", "COST_OF_SALES"),
    ("6000", "Operating Expenses", "expense", "debit", "EXPENSES"),
]

# (function_key, label, account_system_key)
SEED_MAPPINGS = [
    ("SALE_CASH", "Cash Sale — debit account", "CASH_ON_HAND"),
    ("SALE_GCASH", "GCash Sale — debit account", "GCASH"),
    ("SALE_CARD", "Card Sale — debit account", "BANK"),
    ("SALE_BANK_TRANSFER", "Bank Transfer Sale — debit account", "BANK"),
    ("AR", "Credit Sale / Cheque — debit account (Accounts Receivable)", "AR"),
    ("SALES_REVENUE", "Sale — credit account (net of VAT)", "SALES_REVENUE"),
    ("OUTPUT_VAT", "Sale — credit account (VAT portion)", "OUTPUT_VAT"),
]


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("account_type", sa.String(20), nullable=False),
        sa.Column("normal_balance", sa.String(6), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("system_key", sa.String(40), nullable=True, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_accounts_code", "accounts", ["code"])

    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("journal_no", sa.String(20), unique=True),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("reference_no", sa.String(60), nullable=True),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(255)),
        sa.Column("status", sa.String(12), nullable=False, server_default="posted"),
        sa.Column("is_reversal_of_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True),
        sa.Column("entered_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_journal_entries_journal_no", "journal_entries", ["journal_no"])
    op.create_index("ix_journal_entries_source", "journal_entries", ["source_type", "source_id"])
    op.create_index("ix_journal_entries_txn_date", "journal_entries", ["txn_date"])

    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("credit", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("memo", sa.String(255), nullable=True),
    )
    op.create_index("ix_journal_lines_account_id", "journal_lines", ["account_id"])
    op.create_index("ix_journal_lines_entry_id", "journal_lines", ["entry_id"])

    op.create_table(
        "account_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("function_key", sa.String(40), nullable=False, unique=True),
        sa.Column("label", sa.String(150), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    accounts_tbl = sa.table(
        "accounts",
        sa.column("code", sa.String), sa.column("name", sa.String),
        sa.column("account_type", sa.String), sa.column("normal_balance", sa.String),
        sa.column("is_system", sa.Boolean), sa.column("system_key", sa.String),
    )
    op.bulk_insert(accounts_tbl, [
        {"code": code, "name": name, "account_type": atype, "normal_balance": bal,
         "is_system": True, "system_key": key}
        for code, name, atype, bal, key in SEED_ACCOUNTS
    ])

    conn = op.get_bind()
    key_to_id = dict(conn.execute(sa.text("SELECT system_key, id FROM accounts")).fetchall())

    mappings_tbl = sa.table(
        "account_mappings",
        sa.column("function_key", sa.String), sa.column("label", sa.String), sa.column("account_id", sa.Integer),
    )
    op.bulk_insert(mappings_tbl, [
        {"function_key": fkey, "label": label, "account_id": key_to_id[akey]}
        for fkey, label, akey in SEED_MAPPINGS
    ])


def downgrade() -> None:
    op.drop_table("account_mappings")
    op.drop_index("ix_journal_lines_entry_id", table_name="journal_lines")
    op.drop_index("ix_journal_lines_account_id", table_name="journal_lines")
    op.drop_table("journal_lines")
    op.drop_index("ix_journal_entries_txn_date", table_name="journal_entries")
    op.drop_index("ix_journal_entries_source", table_name="journal_entries")
    op.drop_index("ix_journal_entries_journal_no", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_index("ix_accounts_code", table_name="accounts")
    op.drop_table("accounts")
