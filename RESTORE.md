# Backup & Restore — runbook

Print this or keep it open. Written for **Windows PowerShell**, which is what
the shop PC runs.

---

## The mental model

A backup is one plain-text `.sql` file. It is not a copy of the app — it is a
list of instructions that rebuilds the database from nothing.

Restoring is therefore always the same three moves:

> **empty the database → feed the file in → check the numbers**

Everything below is that, with the safety rails.

---

## Taking a backup

**Use the app.** Log in as admin → **Backup**:

- **Save backup now** — writes into the server's `backups/` folder, alongside
  the automatic daily ones.
- **Download backup now** — sends the file to your PC (Downloads).

Both write correct UTF-8. Prefer these over typing commands.

### If you must use the command line

```powershell
docker exec hardware-erp-db pg_dump -U erp hardware_erp | Out-File -Encoding utf8 backup.sql
```

⚠️ **Do NOT use `>` in PowerShell.**

```powershell
docker exec hardware-erp-db pg_dump -U erp hardware_erp > backup.sql   # BROKEN
```

PowerShell's `>` writes **UTF-16**, and Postgres cannot read it back. The file
looks fine — roughly double the correct size — and fails only when you try to
restore it, which is the worst possible moment to find out. This has already
happened once on this project (`before_restore.sql`, 274 KB instead of 133 KB,
restored 0 tables with `invalid byte sequence for encoding "UTF8": 0xff`).

`>` is fine in Git Bash. It is only PowerShell that breaks it.

---

## Restoring

### 0. Take a backup of what's there now

Even when restoring. If the restore goes wrong you need a way back.
Use the app's **Download backup now**, or the `Out-File` command above.

### 1. Go to the project folder

```powershell
cd C:\Users\PerezJulius\erp-system
```

### 2. Make sure the database is running

```powershell
docker compose up -d
```

### 3. Check the file isn't truncated

```powershell
findstr /C:"PostgreSQL database dump complete" "C:\path\to\backup.sql"
```

It must print a line. **Nothing printed = broken file. Stop. Use another one.**

### 4. Empty the database ⚠️ destructive

```powershell
docker exec hardware-erp-db psql -U erp -d hardware_erp -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
```

From here until step 5 finishes, the app shows **Internal Server Error**.
That is expected — there are no tables yet. Don't panic and don't stop.

### 5. Load the file, capturing errors

```powershell
type "C:\path\to\backup.sql" | docker exec -i hardware-erp-db psql -U erp -d hardware_erp 2> restore_errors.txt
```

### 6. Restart the app

```powershell
docker compose restart app
```

### 7. Verify — never assume

```powershell
docker exec hardware-erp-db psql -U erp -d hardware_erp -c "select count(*) as tables, sum(rows) as total_rows from (select (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text::bigint as rows from information_schema.tables where table_schema='public') t;"
```

Two numbers covering the whole database, however many tables it grows to.
Compare against the same command run before the backup was taken.

Then open http://localhost:8000 and log in.

---

## Reading `restore_errors.txt`

Row counts tell you what landed. **Errors tell you what didn't** — a restore
can throw hundreds of errors and still leave most tables populated, so counts
alone can look reassuring while data is quietly missing.

| What you see | Meaning |
|---|---|
| Empty file, or only `transaction_timeout` | ✅ Clean. `transaction_timeout` is a harmless version mismatch — ignore it. |
| `invalid byte sequence for encoding "UTF8"` | ❌ UTF-16 file. The PowerShell `>` trap. Re-take the backup properly. |
| `relation ... already exists` | ❌ Step 4 was skipped. Empty the schema and retry. |
| `COPY` / constraint / relation errors | ❌ Real data problem. Investigate before trusting it. |

---

## Practising safely (no risk to live data)

Restore into a throwaway container instead of the real one:

```powershell
docker run -d --name restore-test -e POSTGRES_USER=erp -e POSTGRES_PASSWORD=erp -e POSTGRES_DB=hardware_erp postgres:16
type "C:\path\to\backup.sql" | docker exec -i restore-test psql -U erp -d hardware_erp
docker exec restore-test psql -U erp -d hardware_erp -c "select count(*) from information_schema.tables where table_schema='public';"
docker rm -f restore-test
```

Same commands, different container name. The live database is never touched.
Do this whenever you want to prove a backup is good — especially before
relying on one at a client site.

---

## Rules that keep you out of trouble

1. **Never run `docker compose down -v`.** The `-v` deletes the data volume.
   That is the one command that loses everything with no backup involved.
   `docker compose down` on its own is safe.
2. **Never trust an untested backup.** A file that exists is not a backup; a
   file that has been restored is.
3. **Restoring rewinds time.** Everything entered after the backup was taken
   is gone. Always take a fresh one first (step 0).
4. **Keep a copy off the machine.** Database and backups on the same laptop
   means theft, drive failure or ransomware takes both. One copy on a USB or
   in a synced cloud folder fixes this.
5. **Prefer the app's Backup page** over typed commands. It writes correct
   UTF-8 server-side and skips every shell quirk above.

---

## Quick reference

| Task | Command |
|---|---|
| Back up (safe) | App → Backup → **Download backup now** |
| Back up (CLI) | `docker exec hardware-erp-db pg_dump -U erp hardware_erp \| Out-File -Encoding utf8 backup.sql` |
| Check file is whole | `findstr /C:"PostgreSQL database dump complete" "file.sql"` |
| Empty DB ⚠️ | `docker exec hardware-erp-db psql -U erp -d hardware_erp -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"` |
| Restore | `type "file.sql" \| docker exec -i hardware-erp-db psql -U erp -d hardware_erp 2> restore_errors.txt` |
| Restart app | `docker compose restart app` |
| Is it running? | `docker compose ps` |
| Why is it broken? | `docker logs hardware-erp-app --tail 30` |
