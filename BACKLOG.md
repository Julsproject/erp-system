# Backlog — deferred work and known gaps

Things deliberately left undone, with the reason. Most are waiting on the
same thing: **the shop is currently back-entering months of past sales and
purchases (January onward)**, so anything that measures live, day-to-day
operation has nothing real to measure yet.

Revisit this list once the backtracking is finished and the shop is running
on the system in real time.

Last reviewed: 2026-08-19

---

## 1. Blocked until the shop runs live

### 1.1 Cash Drawer (shifts) — built, deliberately unlinked
**Status:** complete and working; no menu entry.

`app/shifts.py` records an opening float and an end-of-shift physical cash
count, then reports the variance (short/over). Verified working end to end:
opening ₱1,000, counting ₱950 correctly recorded a −₱50.00 shortage.

It is not in the sidebar because an opening float and a closing count mean
nothing while the entries being made are historical.

**To enable:** add a nav item in `app/templates/base.html` (there is a
comment marking the spot) pointing at:
- `/shifts` for manager/admin — history of everyone's shifts
- `/shifts/current` for cashiers — their own drawer

The history page will also want an "Open my drawer" button for staff, since
they work the till too; that needs `my_open_shift` passed from
`shifts.shift_history`.

### 1.2 POS does not require an open drawer
**Status:** the module docstring says it does. The code does not.

`app/shifts.py`'s docstring claims the drawer is "enforced by gating
`/pos`", but `app/pos.py` contains no such check — the only mention of
"shift" there is an unrelated comment. So a cashier can sell without ever
opening a drawer, which makes the variance meaningless.

**Decide when enabling 1.1:** either enforce it (redirect `/pos` to
`/shifts/open` when the user has no open shift) or correct the docstring.
Don't leave the two disagreeing.

---

## 2. Manual steps that could be automated

### 2.1 Bank Transfer / GCash / Maya sales don't reach the Banking register
**Status:** by design, but it means double entry.

A sale paid by Bank Transfer posts correctly to the **accounting ledger**
(`SALE_BANK_TRANSFER` → BANK account), so the Balance Sheet and P&L are
right. But the **Banking module** (`/banking`) is a separate manual
register — POS never writes to it. The money really did land in the bank,
so someone has to record that deposit by hand or the register drifts.

**Workaround in use:** batch it — one deposit entry per week covering all
bank-transfer sales, rather than one per sale.

**Option when ready:** auto-create the matching deposit in the linked bank
account when a sale is paid by Bank Transfer / GCash / Maya. This is a
behaviour change (entries start appearing that nobody typed), so confirm
before building.

### 2.2 Bank reconciliation is CSV-only — and that is the right call
**Not a gap.** `/banking/accounts/{id}/reconcile` imports a CSV statement
and matches against recorded transactions.

A live bank API was considered and rejected: Philippine banks don't grant
API access to a single merchant's custom software, it would mean storing
bank credentials (the app currently makes **zero** outbound network calls),
and it would break whenever the bank changed anything. Recorded here so the
decision isn't relitigated.

Revisit only if the shop grows to multiple branches with high transaction
volume, or a prospective buyer demands it.

---

## 3. Security and safety — do before wider rollout

### 3.1 SECRET_KEY is still the shipped default
**Status:** open. There is no `.env` file, so `docker-compose.yml` falls
back to `SECRET_KEY: change-me-in-production`.

That key signs login session cookies. Because the value is public (it's in
the repo), a forged cookie could in principle impersonate an admin. Low risk
on a closed shop LAN, but it's a two-minute fix.

**Fix:** create `.env` next to `docker-compose.yml` with one line:

```
SECRET_KEY=<random string>
```

Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Then `docker compose up -d`.

- Do **not** set `POSTGRES_PASSWORD` there — the database was created with
  the default and would stop accepting connections.
- `ADMIN_PASSWORD` only applies on first-ever run; change passwords in-app.
- Everyone gets logged out once. No data is affected.

### 3.2 Backups have gaps, live on the same PC, and have never been restored
**Status:** open. Three separate problems.

1. **Gaps.** The daily backup only runs when the PC and Docker are on. As of
   2026-08-19 the August files skip the 2nd, 10th, 12th, 13th, 15th, 16th
   and 17th.
2. **Same disk.** Every backup sits in `./backups` on the same machine as
   the database. Theft, drive failure or ransomware takes the live data
   *and* every backup together.
3. **Never tested.** A backup nobody has restored is a guess. Restore one
   into a throwaway container once, before relying on it.

**Fix, roughly in order of value:** copy backups off the machine (USB taken
home, or a synced cloud folder), then do one restore drill.

### 3.3 No automated tests
**Status:** open. ~31,000 lines, zero tests.

Two real bugs this month were found by luck rather than by a suite: sales
being wiped of their shelf on re-import, and voided sales inflating cashier
totals by ₱3,300. Now that every transaction auto-posts to a double-entry
ledger, a silent bug in one posting path could corrupt the books unnoticed
for months.

**Highest value if picked up:** cover the money paths — sale → stock →
journal entry, void, refund, purchase receive, credit settlement.

---

## 4. Decisions to review once staff are using it daily

### 4.1 Cashiers can't collect credit payments
Credits is manager+ (deliberate — collecting money against utang is
trust-sensitive). The consequence: a customer walking in to pay their
balance needs a manager logged in.

If that becomes a daily bottleneck, the middle ground is to let cashiers
**record** a payment but not **undo** one (undo is already manager-only).

### 4.2 "Cashier Activity" is a misnomer
The list covers *everyone* who sold that day — managers and admins included,
not just cashiers. Pre-existing. Consider renaming to "Sales Activity".

### 4.3 Managers have no personal activity view
`/activity` redirects staff to `/activity/all`, so a manager can't open a
"My Activity" page. Their own numbers *are* visible — they appear as a row
in the all-staff list and can be drilled into — so this is one extra click,
not missing data.

### 4.4 Stock Count shelf assignment only fills blanks
"Assign counted items with no shelf yet" never moves a product that already
belongs to another shelf — same rule as the Excel import, so a mis-click
can't silently relocate stock. To actually move a product between shelves,
edit the product.

If moving stock between shelves becomes routine, this may want an explicit
"move to shelf" action with a confirmation.

---

## 5. Ideas raised but not scheduled

- **Period lock / "close the month".** Once a month is closed, its sales and
  purchases can't be edited or voided. Prevents someone accidentally voiding
  a three-month-old sale and reshaping a P&L the accountant already
  finalised. Worth considering once backtracking is done and the historical
  data is meant to be final.
- **Payroll.** Discussed; no biometric device needed (login-based time
  tracking would do). Out of scope for now.
- **Productising for other hardware stores.** Nothing in the system is
  specific to this client — same multi-unit conversions, VAT, utang
  workflow and receipt numbering would suit any PH hardware store.
