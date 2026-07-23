# Hardware ERP

An in-house **POS + Inventory + Sales** system for a hardware store. Runs on one PC and is usable from any device on the same network through a web browser.

- **Stack:** Python + FastAPI + Jinja2 (server-rendered) · PostgreSQL · Docker Compose
- **Access:** a web browser — on the host PC or any phone/PC on the same LAN
- **Money:** Philippine peso, VAT (12% inclusive) toggle per product

---

## Features (built so far)

**Inventory**
- Products with **Category** and **Unit Type** (create-your-own — just type a new one)
- **Three selling prices on every item**, set on the product form and on the
  purchase screen's quick "create product" panel:
  - **Fixed** — typed in directly (the default POS price).
  - **Markup %** — `cost × (1 + pct/100)`, a % *on top of cost*.
  - **Margin %** — `cost ÷ (1 − pct/100)`, a % *of the selling price*.

  You enter the two percentages; their prices are calculated from cost and
  refresh whenever the cost changes. Each row shows its profit and **true
  margin**, because markup and margin are not interchangeable: on a ₱300 cost,
  30% markup gives ₱390 (only a 23.1% margin) while 30% margin gives ₱428.57.
  Since the Gross Profit reports measure profit as a share of revenue, a
  markup-priced sale always reports a lower % than the number typed.
  Shared maths lives in `app/pricing.py` so the form, the purchase panel and
  the server can't drift apart.
- **Choosing a price at POS** — the per-line *unit* dropdown lists all three
  (`piece`, `piece · Markup`, `piece · Margin`) with their prices, so the
  cashier picks one from the control they already use; different lines can use
  different prices. The extra options only appear once a markup/margin % is
  set, so items priced the old way look unchanged. `sale_lines.price_tier`
  records which price was charged (shown on the receipt), so two sales of the
  same item at different prices can be told apart later.
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
| 0017 | app_settings (Settings UI), notifications (Notifications Center) |
| 0018 | audit_log (system-wide who-did-what activity trail) |
| 0019 | Cash on Delivery fields on deliveries (COD flag, amount, collection) |
| 0020 | three selling prices per product (fixed + markup % + margin %) |
| 0021 | sale_lines.price_tier — which of the three prices was charged |

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
- **Cash on Delivery (COD)** — lives on the *delivery*, deliberately not as a POS
  payment method: a walk-in sale is paid at the counter, so "collect on handover"
  only makes sense once there is something to hand over. The sale is rung up as a
  Receivable; ticking **COD** when scheduling the delivery (offered only when the
  invoice still has a balance) marks that balance as the driver's to collect.
  Marking the delivery **Delivered** is what records the collection — it creates
  the `ReceivableSettlement` in the same step, so handover and payment can't drift
  apart. Partial collections are supported (the remainder stays outstanding).
  While a COD delivery is open, the invoice is tagged **COD / "on delivery"** in
  Receivables and is excluded from overdue-credit notifications — it is awaiting a
  handover, not a customer who is late paying.
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

### ✅ Tier 3 (Settings + Notifications done)
1. **Settings UI** (`/settings`, admin-only) — done. Edit the **business name**
   (drives the login screen, page titles and the receipt header) plus optional
   **receipt address / contact / TIN / footer**, and **change your own password**,
   all without editing `.env` or rebuilding. Backed by an `app_settings` key-value
   table (adding a new setting is a new row, never a migration); the business name
   is loaded into `app.title` on startup and updated in place on save. `.env` still
   owns infrastructure config (DATABASE_URL, SECRET_KEY) — intentionally not editable
   in-app.
2. **Notifications Center** (`/notifications`, admin-only) — done. One inbox for the
   alerts that were previously only nav badges + the Dashboard "Needs your attention"
   widget: out/low stock, below-cost pricing, overdue & due-soon credits, cheques
   due/overdue, pending deliveries, stale/missing backup. Conditions are **derived**
   from live data and reconciled into `notifications` rows by a throttled sweep
   (`notifications.sync_notifications`): a new condition inserts an unread row, a
   cleared one is marked resolved (kept under the **History** tab), and a recurrence
   raises a fresh row. Sidebar badge shows the unread count; supports mark-read /
   mark-all-read. The old badges + dashboard widget were left in place — this is an
   addition, not a replacement.

### ✅ Tracking, Logs & History (done)
Built in response to the owner's ask — "know if the business is moving or not,
especially tracking, logs and history." Three connected pieces:

1. **Activity Log** (`/audit`, admin-only, in the Admin nav group) — a system-wide
   who-did-what trail backed by `audit_log`. Every meaningful change records the
   actor, timestamp, client IP, a plain-language summary and (for edits) a
   field-by-field **before → after** diff. Instrumented across: inventory
   (create / edit / **stock adjustment** / archive / bulk import), sign-in
   (login / failed login / logout), users (create / edit / password reset),
   business settings + own-password change, expenses (create / edit / void),
   banking (account edits, transaction create / void) and deliveries (schedule /
   dispatch / complete / cancel). Filter by user, action, area and date.
   Written via `audit.record(...)` in the same DB transaction as the change, so
   the log can never disagree with what happened.
2. **Stock Card** (`/products/{id}/stock-card`, admin-only, "Stock card" link per
   inventory row) — surfaces the per-product `StockMovement` ledger that was being
   recorded but never shown: every in/out (sale, refund, exchange, purchase,
   return, manual adjustment) with a running balance anchored to the current
   on-hand total. Manual stock edits now also write a movement (they previously
   left no trace), so the ledger reconciles going forward; the implied opening
   balance is shown for pre-tracking history (e.g. bulk imports).
3. **Growth comparison + Sales-by-Product** — the Dashboard's period KPIs (Sales,
   Gross Profit, Expenses) now show a **▲/▼ % vs the previous period of equal
   length** — the direct "are we moving or not" read. Direction is taken from the
   raw values so recovering from a loss reads correctly, and the % is suppressed
   when the prior base is zero/negative (shown as "new" / arrow instead). New
   **Sales by Product** report (`/reports/sales-by-product`) lists units sold,
   revenue and gross profit per product for any date range, with Excel export —
   surfacing slow movers and loss-making lines.

### 🔜 Tier 3 — still open
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
