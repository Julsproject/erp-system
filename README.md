# Hardware ERP

An in-house **POS + Inventory + Sales** system for a hardware store. Runs on one PC and is usable from any device on the same network through a web browser.

- **Stack:** Python + FastAPI + Jinja2 (server-rendered) · PostgreSQL · Docker Compose
- **Access:** a web browser — on the host PC or any phone/PC on the same LAN
- **Money:** Philippine peso, VAT (12% inclusive) toggle per product

---

## Features (built so far)

**Inventory**
- Products with **Category** and **Unit Type** (create-your-own — just type a new one)
- Columns: Product Name, Category, Unit Type, Cost of Sales, Selling Price, Actual Beginning Stocks, Stocks Qty, **Total Qty** (auto)
- Search + pagination (fast with a large catalog)
- **Bulk import** from Excel/CSV (download a template, fill it, upload)
- **Units ladder** (multi-unit): sell one product in several units (e.g. Bag = 50 kg) each with its own price
- VAT-able toggle per product · Archive items

**Point of Sale** — three modes:
- **Payment** — search & add products, choose the selling unit, per-line and overall discounts, live VAT, **Full or Split payment** (Cash / GCash / Card / Bank Transfer / Receivable), change, typed invoice number, printable receipt. Overselling is allowed (with a warning).
- **Refund** — look up an invoice, tick the items to refund → cash out, **stock added back**.
- **Exchange** — return items + buy new ones; pay the difference or get a cash refund; stock adjusted both ways.

**Customers & Credit**
- Customer accounts (Name, TIN, Address)
- **Receivable ("utang")** as a payment method; auto-creates the customer
- **Sales history** — All Sales (fully-paid) and **Receivables** (unpaid utang) with a **Pay** button to settle
- **Credits** menu — search a customer → printable **Statement of Account**

---

## Requirements

- **Windows 10/11** (or any OS with Docker)
- **Docker Desktop** — the only thing you need to install
  - On Windows it uses the **WSL2** backend. If Docker won't start after install, open **PowerShell as Administrator** and run `wsl --update`, then restart Docker Desktop.

---

## Quick start (Docker — recommended)

From this folder (`D:\hardware-erp`), open a terminal and run:

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

Run these from the `D:\hardware-erp` folder:

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

Settings live in the `.env` file in this folder:

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

Everything lives in the Postgres volume. To back up:

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
  main.py         FastAPI app + router registration (login/logout, dashboard)
  config.py       settings loaded from .env
  database.py     SQLAlchemy engine + session
  models.py       ORM models (users, products, units, sales, payments, customers, …)
  auth.py         bcrypt password hashing
  deps.py         shared auth dependency
  templating.py   Jinja2 setup + ₱ / qty format filters
  seed.py         creates the initial admin user
  products.py     Inventory module + Excel/CSV import
  pos.py          Point of Sale: sale, refund, exchange, receipt
  customers.py    Customer accounts
  sales.py        Sales history + receivables + settlement
  credits.py      Credit statements
  templates/      HTML (Jinja2)
  static/css/     styles
migrations/       Alembic migrations (schema history, 0001…)
Dockerfile
docker-compose.yml
.env              your settings
```

---

## How the database is built

Schema changes are versioned with **Alembic** migrations in `migrations/versions/`. They run **automatically on startup**, so you never run SQL by hand. Current migrations:

| Rev | Adds |
|---|---|
| 0001 | users |
| 0002 | categories, unit_types, products |
| 0003 | product_units (units ladder), sales, sale_lines, stock_movements |
| 0004 | customers, payments (split), receivable on sales |
| 0005 | receivable_settlements (utang collections) |
| 0006 | refund/exchange transaction type |
| 0007 | suppliers, purchases (receiving) |
| 0008 | dashboard support fields |
| 0009 | cash_shifts (later dropped, see 0011) |
| 0010 | quotations (estimates) |
| 0011 | drop cash_shifts — replaced by automatic Cashier Activity history |
| 0012 | purchase status lifecycle (pending/confirmed/paid/cancelled) |
| 0013 | link a purchase return back to the delivery it came from |
| 0014 | post_dated_cheques (PDC register) |
| 0015 | expense_categories, expenses, deliveries |
| 0016 | bank_accounts, bank_transactions (Cash & Banking) |

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
- **Can't reach the app from another device** → make sure both devices are on the same Wi-Fi/LAN, use the host PC's IPv4 (`ipconfig`), and allow port `8000` through Windows Firewall if prompted.
- **pgAdmin "password authentication failed for user erp"** → you're on the wrong port. Use **5433**, not 5432.
- **Login fails** → default is `admin` / `admin123`; confirm the containers are up with `docker compose ps`.

---

## Roadmap

Checked against an 18-module wishlist (photo of a handwritten list) and prioritized
into tiers. This section is the source of truth for "what's left" — keep it updated
so picking this up from a different device doesn't require re-deriving status from
git log.

### ✅ Core operations (done)
Dashboard · Sales/POS (payment, refund, exchange, split payment incl. **Cheque**
as a post-dated-cheque-backed method) · Inventory · Purchasing (PO lifecycle) ·
Customers · Suppliers · Quotations · PDC (post-dated cheque) register ·
role-based access (admin vs cashier) · pagination + filters across all list pages.

### ✅ Tier 1 (done)
- **Expenses** (`/expenses`) — categorized (create-your-own, same idea as Product
  categories), filterable, admin-only, void instead of delete.
- **Delivery Management** (`/deliveries`) — schedule from a receipt or by invoice #
  lookup, pending → out for delivery → delivered/cancelled. Open to cashiers too
  (operational, not back-office).
- Dashboard: Expenses KPI tile + net-profit sub-line, custom date-range picker,
  fixed the 90-day chart's overlapping labels (bars stay daily, labels thin to ~12).

### ✅ Tier 2 (done)
- **Reports** (`/reports`) — hub page; **Profit & Loss** (revenue, gross profit,
  expenses by category, net profit, any date range) and **Inventory Valuation**
  (by category and by product), both with Excel export.
- **Cash & Banking** (`/banking`) — multiple bank accounts, running balance derived
  from a deposit/withdrawal ledger (never stored, always computed). Scope was
  deliberately limited to balance-tracking, not statement reconciliation — confirm
  with the user before expanding that.

### 🔜 Tier 3 (not started — pick up here)
1. **Settings UI** — in-app screen for app name / admin password / etc. Right now
   config is `.env`-file only. Low effort, low urgency (only matters if someone
   besides the owner needs to change settings without editing files).
2. **Notifications Center** — centralize the alerts that already exist scattered
   around (low-stock/out-of-stock, overdue credits, cheques due, pending
   deliveries — currently nav badges + the Dashboard "Needs your attention"
   widget) into one inbox with history/read state.
3. **Full Accounting** (GL, P&L, Balance Sheet, VAT/BIR reports) — deliberately
   deprioritized: the owner's accountant/other software handles this externally.
   Only revisit if that changes; until then the P&L report + per-module Excel
   exports are the intended output for an accountant to work from.

### Skip unless a specific need shows up
- **Document Management** (file/receipt attachments) — no current need.
- **Generic configurable Approval Workflow** — the pending → confirmed → paid
  lifecycles already on Purchases/Quotations/PDC cover this for a small team; a
  configurable approval engine would be over-engineering here.
- **Full Employee/HR management** (attendance, payroll) — `users.py` stays scoped
  to login accounts + role only; no payroll processing planned in-app.
