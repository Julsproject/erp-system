# Hardware ERP

An in-house **POS + Inventory + Sales + full Accounting** system for a hardware store (Leafar Merchandising). Runs on one PC and is usable from any device on the same network through a web browser.

- **Stack:** Python + FastAPI + Jinja2 (server-rendered) · PostgreSQL · Docker Compose
- **Access:** a web browser — on the host PC or any phone/PC on the same LAN
- **Money:** Philippine peso, VAT (12% inclusive) toggle per product

---

## Features

### Inventory
- Products with **Category**, **Sub Category**, **Unit Type**, and **Shelf** (all create-your-own — just type a new one)
- **Three selling prices on every item** — Fixed, Markup %, Margin % — computed from cost and kept in sync by shared math in `app/pricing.py`
- **Units ladder**: sell one product in several units (e.g. Bag = 40 kg, Sack = 25 kg), each optionally chained off another unit (e.g. "1 Elf Load = 6 Sack") and each with its own Fixed/Markup/Margin pricing
- **Shelves** (`/shelves`): where each product physically sits in the store — browse by shelf, filter Inventory by shelf, and pull a shelf's items into a Stock Count in one click
- **Bulk import** from Excel/CSV — two flows: Normal Products (no unit conversion) and Units & Conversions (ladder pricing); download-a-template-first on both
- **Stock Count** (cycle count, `/stock-count`): scan/search or bulk-add-by-shelf, live variance vs system qty, per-unit count entry (e.g. "2 sacks + 1 open bag"), bulk upload of counted quantities from Excel, apply corrections on Complete (logged to Activity Log, Stock Card, and Inventory Adjustments report)
- **Stock Card** (`/products/{id}/stock-card`, admin) — full per-product movement ledger with running balance
- **Month-End Rollover** — folds Stocks Qty into Actual Beginning at month end; history kept per product, per month
- Search + pagination, VAT-able toggle, Archive

### Point of Sale
Three modes on one screen:
- **Payment** — search & add products, choose the selling unit and price tier, per-line/overall discounts, live VAT, **Full or Split payment** (Cash / GCash / Maya / Other E-Wallet / Card / Bank Transfer / Cheque / Receivable), typed or auto invoice number with **receipt-type prefix** (DRS/DRB/SI), transaction date **backdating**, printable receipt (regular + thermal) and PDF
- **Refund** — look up an invoice, tick items to refund, stock added back
- **Exchange** — return items + buy new ones in one transaction; stock adjusted both ways
- **Void a Sale** (admin/manager, cashier-permission toggle in Settings) — reverses stock and payment on a clean sale (no credit/refund/exchange/PDC attached) with a required reason; voided sales are pulled from every report but stay visible under **Voided Sales**
- On-the-fly **new/edit customer**, calculator popup with keyboard/numpad support, cancelled-receipt-number tracking (`/pos/cancelled-receipts`)

### Customers & Credit
- Customer accounts (Name, TIN, Address) with editable delivery address/note per sale
- **Receivable ("utang")** as a payment method; auto-creates the customer
- **Sales history** — All Sales (fully-paid), **Receivables** (unpaid utang) with a Pay button, Returns, Exchanges, Voided Sales
- **Credits** menu — search a customer → printable **Statement of Account**
- **Quotations** — price estimates with a pending → confirmed → paid lifecycle; converts into a real Sale on payment

### Purchasing & Suppliers
- **Suppliers** — profiles + per-supplier purchase history
- **Purchases** (`/purchases`) — receive goods (pending → confirmed → paid) or return goods to a supplier; confirming updates stock and the product's cost price; **Delivery date backdating**; item editing on unsettled purchases
- **Payables** with due-date aging

### Delivery Management
- Schedule a delivery from a receipt or invoice # lookup; pending → out for delivery → delivered/cancelled
- **Cash on Delivery (COD)** — ties into Receivables; marking Delivered records the collection as a settlement in the same step

### Cash & Banking
- Multiple bank accounts with a running balance derived from their deposit/withdrawal ledger (never stored)
- **Post-dated cheques (PDC)** register — received (from a customer) or issued (to a supplier); clearing/bouncing only takes effect once the bank actually honors it; supports **multi-invoice application** and a 3-bucket due-date alert
- **Bank Reconciliation** — match transactions against an imported bank statement (CSV import + Excel export)
- **Petty Cash** as an account kind

### Full Accounting (`/accounting`, admin)
Double-entry bookkeeping layered on top of every other module — nothing is entered twice:
- **Chart of Accounts** + account mappings
- **Automatic posting** from Sales, Purchases, Expenses, Receivable settlements, and Banking/PDC — the journal entry is created the moment the underlying transaction happens
- **General Ledger**, **Trial Balance** (Excel export), **manual Journal Entries** (draft/post/delete/reverse)
- **Balance Sheet** and ledger-based **Profit & Loss**
- **VAT Report** — Output VAT vs Input VAT, netted to VAT Payable
- **Cash Book / Bank Ledger**, **Cash Flow Statement** (Excel export)
- **AR / AP Subledgers** — per-customer / per-supplier drill-down
- **Financial Dashboard** — rollup of every accounting report in one place

### Reports (`/reports`)
The operational/management-facing reports, separate from the ledger-based Accounting suite above:
Profit & Loss, Sales by Product, Sales by Unit, Inventory Valuation, Low Margin, Inventory Adjustments, Month-End Rollover History, **Weekly Summary printout**.

### Expenses
Categorized (create-your-own), receipt #, file attachments, Credit Card as a payment method, filterable, void instead of delete.

### Admin
- **Users** (admin-only) — create cashier/manager/admin logins, roles
- **Encoders** — a managed list of who actually wrote up a sale, separate from login accounts (for shops where several people share one POS login)
- **Settings** (admin-only) — business name/receipt header, minimum-margin and low-stock alert thresholds, cashier-void permission toggle, change your own password
- **Notifications Center** — one inbox for low/out-of-stock, below-cost pricing, overdue/due-soon credits, cheques due, pending deliveries, stale backup
- **Activity Log** (admin-only) — system-wide who-did-what trail with before/after diffs on edits
- **Cashier Activity** — read-only per-day summary of what a cashier processed
- **Backup** (admin-only) — on-demand download + browse the automatic daily backups

---

## Requirements

- **Windows 10/11** (or any OS with Docker)
- **Docker Desktop** — the only thing you need to install
  - On Windows it uses the **WSL2** backend. If Docker won't start after install, open **PowerShell as Administrator** and run `wsl --update`, then restart Docker Desktop.

---

## Quick start (Docker — recommended)

From this folder, open a terminal and run:

```powershell
docker compose up -d --build
```

That single command:
1. Starts a **PostgreSQL** database
2. Creates the database, runs all **migrations** (builds every table), and seeds the **admin** user
3. Starts the app

**Open it:**
- On this PC: <http://localhost:8000>
- From another device on the LAN: `http://<this-pc-ip>:8000`
  (find the IP by running `ipconfig` and looking for the IPv4 address, e.g. `192.168.100.14`)

**Default login:** `admin` / `admin123`
(change these in the `.env` file — see Configuration below)

---

## Everyday commands

```powershell
docker compose up -d          # start (fast after the first build)
docker compose down           # stop (all data is kept)
docker compose restart app    # restart just the app
docker compose logs -f app    # watch the app's logs
docker compose up -d --build  # rebuild after code changes
```

The system also **auto-starts** when the PC boots (as long as Docker Desktop is set to run at startup), because the containers use `restart: unless-stopped`.

---

## Configuration (`.env`)

```
APP_NAME=Hardware ERP          # shown on the login screen and receipts
SECRET_KEY=dev-secret-change-me # CHANGE THIS before real use
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
```

After changing `.env`, apply it with:

```powershell
docker compose up -d
```

> The admin user is only **created** the first time. Changing `ADMIN_PASSWORD` later won't update an existing admin — change the password from inside the app instead (or ask the developer).

Most other display/behavior settings (business name, receipt header, low-stock/margin thresholds, cashier-void permission) are editable from **Settings** in-app — no `.env` edit or rebuild needed for those.

---

## Viewing the database (pgAdmin 4)

The database is exposed on host port **5433** (so it won't clash with a separate local PostgreSQL on 5432). In pgAdmin, register a new server:

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | **5433** |
| Maintenance database | `hardware_erp` |
| Username | `erp` |
| Password | `erp` |

Then browse **Databases → hardware_erp → Schemas → public → Tables**, right-click a table → **View/Edit Data → All Rows**.

---

## Backup & restore

An automatic daily backup service is included (`hardware-erp-backup` in `docker-compose.yml`) — it writes a dump to the shared `backups/` folder once a day and keeps `BACKUP_KEEP_DAYS` (default 31) of history. The **Backup** page in-app (admin-only) lets you download a fresh one on demand or re-download any of the scheduled ones.

Manual equivalents:

```powershell
docker exec hardware-erp-db pg_dump -U erp hardware_erp > backup.sql
```

To restore into a fresh database:

```powershell
type backup.sql | docker exec -i hardware-erp-db psql -U erp -d hardware_erp
```

---

## Project structure

```
app/
  main.py         FastAPI app + router registration
  config.py       settings loaded from .env
  database.py     SQLAlchemy engine + session
  models.py       ORM models (users, products, units, sales, payments, customers, accounting, …)
  auth.py         bcrypt password hashing
  deps.py         shared auth dependencies (is_staff / is_admin)
  templating.py   Jinja2 setup + peso / qty format filters
  seed.py         creates the initial admin user
  pricing.py       shared markup/margin math

  products.py      Inventory + bulk import (normal + units)
  shelves.py       Shelf locations
  stock_count.py   Physical stock count
  pos.py           POS: sale / refund / exchange / void / receipt
  customers.py     Customer accounts
  sales.py         Sales history + receivables + settlement + voided sales
  quotations.py    Price estimates
  credits.py       Credit statements
  suppliers.py     Supplier profiles
  purchases.py     Purchasing / receiving / returns
  deliveries.py    Delivery scheduling + COD
  expenses.py      Business expenses
  banking.py       Bank accounts + ledger + reconciliation
  pdc.py           Post-dated cheques
  encoders.py      Who-wrote-up-the-sale list
  reports.py       Operational reports (P&L, valuation, sales-by-*, weekly summary)
  accounting.py    Full double-entry ledger: CoA, GL, Trial Balance, Balance Sheet, VAT, subledgers
  dashboard.py     Home page: KPIs, charts, alerts
  notifications.py Notifications Center
  shifts.py        Cash drawer shift counts
  activity.py      Cashier activity history
  audit.py         Audit trail (record + view)
  backup.py        DB backup UI
  users.py         User accounts (admin-only)
  settings.py      Settings UI (admin-only)

  templates/       HTML (Jinja2), one folder per module
  static/css/      styles
migrations/        Alembic migrations (schema history)
Dockerfile
docker-compose.yml
.env               your settings
```

---

## How the database is built

Schema changes are versioned with **Alembic** migrations in `migrations/versions/`, numbered `0001` through the current head — they run **automatically on startup**, so you never run SQL by hand. Roughly, by range:

| Range | Covers |
|---|---|
| 0001–0011 | Core: users, categories/unit types/products, POS + sales/refund/exchange, customers, receivables, suppliers/purchases, quotations |
| 0012–0021 | Purchase status lifecycle, PDC register, expenses, deliveries, bank accounts, settings, notifications, audit log, three selling prices |
| 0022–0034 | Cashier shifts, sub-categories, barcode, purchase settlements/valuation, stock count, purchase multiplier (added then removed) |
| 0035–0041 | Per-unit markup/margin, month-end rollover history, void-a-sale, shelves, sale receipt type, encoders |
| 0042–0053 | Full Accounting (Chart of Accounts, posting engine for Purchases/Expenses/Banking), bank reconciliation, PDC integrity, Maya/Other E-Wallet, Petty Cash, PDC multi-invoice, expense enhancements, cancelled receipt tracking, per-sale delivery address |

Check `migrations/versions/` for the exact list — each file's name and docstring describe what it adds.

---

## Running without Docker (developers only)

Requires Python 3.12+ and a reachable PostgreSQL.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:DATABASE_URL = "postgresql+psycopg2://erp:erp@localhost:5433/hardware_erp"
alembic upgrade head        # build tables
python -m app.seed          # create admin
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Troubleshooting

- **Docker Desktop won't start / "engine is unable to start"** → open PowerShell **as Administrator**, run `wsl --update`, then `wsl --shutdown`, quit and reopen Docker Desktop until it shows **Engine running**.
- **Docker Desktop shows an "Inference manager" crash popup** → unrelated to this app (it's Docker's bundled AI/model-runner feature); dismiss with the **X**, don't click Quit or Reset — the app/database containers keep running fine underneath.
- **Can't reach the app from another device** → make sure both devices are on the same Wi-Fi/LAN, use the host PC's IPv4 (`ipconfig`), and allow port `8000` through Windows Firewall if prompted.
- **pgAdmin "password authentication failed for user erp"** → you're on the wrong port. Use **5433**, not 5432.
- **Login fails** → default is `admin` / `admin123`; confirm the containers are up with `docker compose ps`.
- **Port 5433 already allocated on startup** → another Postgres container on this machine (from an unrelated project) is holding that host port. Either stop that container, or change the host-side port mapping for `hardware-erp-db` in `docker-compose.yml`.

---

## Role permissions

- **Cashier** — POS, own Activity, Credits/Customers lookup; everything else redirects to POS.
- **Manager** — everything a Cashier can do, plus Inventory, Purchasing, Sales/Reports, Stock Count, Shelves, Finance (Expenses/Banking/Accounting reports), Void a Sale (if enabled in Settings).
- **Admin** — everything a Manager can do, plus **Users**, **Backup**, and **Stock Card** — the three areas that stay admin-exclusive.
