"""Database backup: on-demand download plus a view of the automatic daily backups.

The automatic backups are written by the `backup` service in docker-compose into
a shared folder (BACKUP_DIR). This module lets the owner download a fresh dump
at any time, and re-download any of the scheduled ones.
"""
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

BACKUP_DIR = Path("/backups")
# Only ever serve files that look like our own backups.
SAFE_NAME = re.compile(r"^hardware_erp_[0-9]{4}-[0-9]{2}-[0-9]{2}\.sql$")


def _db_parts():
    """Host/port/user/password/dbname from the SQLAlchemy URL."""
    url = settings.database_url.replace("postgresql+psycopg2://", "postgresql://")
    p = urlparse(url)
    return {
        "host": p.hostname or "db",
        "port": str(p.port or 5432),
        "user": unquote(p.username or "erp"),
        "password": unquote(p.password or ""),
        "dbname": (p.path or "/hardware_erp").lstrip("/"),
    }


def run_pg_dump(timeout: int = 120):
    """Return (sql_bytes, error_message)."""
    d = _db_parts()
    env = {**os.environ, "PGPASSWORD": d["password"]}
    cmd = ["pg_dump", "-h", d["host"], "-p", d["port"], "-U", d["user"], d["dbname"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, env=env, timeout=timeout)
    except FileNotFoundError:
        return None, "pg_dump is not installed in the app container."
    except subprocess.TimeoutExpired:
        return None, "The backup took too long and was stopped."
    if proc.returncode != 0:
        return None, (proc.stderr or b"").decode("utf-8", "replace").strip() or "pg_dump failed."
    return proc.stdout, None


def latest_backup():
    """Most recent backup file, or None. Used by the dashboard staleness alert."""
    files = _list_backups()
    return files[0] if files else None


def _list_backups():
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("hardware_erp_*.sql"), reverse=True):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "name": f.name,
            "size": st.st_size,
            "when": datetime.fromtimestamp(st.st_mtime),
        })
    return out


@router.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, error: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    files = _list_backups()
    return templates.TemplateResponse(
        "backup.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "files": files, "latest": files[0] if files else None,
            "folder_mounted": BACKUP_DIR.is_dir(), "error": error,
        },
    )


@router.get("/backup/download")
def backup_download(user=Depends(get_current_user)):
    """Make a fresh dump right now and send it to the browser."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    sql, err = run_pg_dump()
    if err:
        return RedirectResponse(f"/backup?error={err[:200]}", status_code=302)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return Response(
        content=sql,
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="hardware_erp_{stamp}.sql"'},
    )


@router.get("/backup/file/{name}")
def backup_file(name: str, user=Depends(get_current_user)):
    """Re-download one of the automatic daily backups."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    if not SAFE_NAME.match(name):
        return RedirectResponse("/backup?error=Invalid+file+name.", status_code=302)
    path = (BACKUP_DIR / name).resolve()
    # Belt and braces: never serve anything outside the backup folder.
    if not str(path).startswith(str(BACKUP_DIR.resolve())) or not path.is_file():
        return RedirectResponse("/backup?error=That+backup+no+longer+exists.", status_code=302)
    return Response(
        content=path.read_bytes(),
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
