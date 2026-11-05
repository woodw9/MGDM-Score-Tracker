"""SQLite store for every photographed page (the source of truth).

Excel and Google Sheets are always rebuilt from this database, so they can be
deleted or overwritten at any time without losing scores.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DATA_DIR

DB_PATH = DATA_DIR / "mgdm.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,            -- reading | review | confirmed | rejected
  image_path TEXT NOT NULL,
  original_name TEXT,
  qr_text TEXT,
  entry_id TEXT,
  event TEXT,
  judge INTEGER,
  page INTEGER,
  values_json TEXT,                -- current values (AI read, then tabulator-edited)
  ai_values_json TEXT,             -- untouched AI read, for audit
  uncertain_json TEXT,
  judge_name TEXT,
  comments TEXT,
  notes TEXT,
  error TEXT,
  model TEXT,
  superseded INTEGER DEFAULT 0,
  confirmed_by TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_pages_key ON pages(entry_id, event, judge, page, status);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


_con: Optional[sqlite3.Connection] = None


def db() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = connect()
    return _con


def row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("values_json", "ai_values_json", "uncertain_json"):
        d[k[:-5]] = json.loads(d[k]) if d.get(k) else ({} if k != "uncertain_json" else [])
    return d


def create_page(image_path: str, original_name: str) -> int:
    with _lock:
        cur = db().execute(
            "INSERT INTO pages(status,image_path,original_name,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("reading", image_path, original_name, _now(), _now()))
        db().commit()
        return cur.lastrowid


def update_page(pid: int, **kw) -> None:
    for k in ("values", "ai_values", "uncertain"):
        if k in kw:
            kw[k + "_json"] = json.dumps(kw.pop(k))
    kw["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in kw)
    with _lock:
        db().execute(f"UPDATE pages SET {cols} WHERE id=?", (*kw.values(), pid))
        db().commit()


def get_page(pid: int) -> Optional[dict]:
    r = db().execute("SELECT * FROM pages WHERE id=?", (pid,)).fetchone()
    return row_to_dict(r) if r else None


def list_pages(status: Optional[str] = None, limit: int = 500) -> list:
    if status:
        rows = db().execute("SELECT * FROM pages WHERE status=? AND superseded=0 ORDER BY id DESC LIMIT ?",
                            (status, limit)).fetchall()
    else:
        rows = db().execute("SELECT * FROM pages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [row_to_dict(r) for r in rows]


def counts() -> dict:
    rows = db().execute("SELECT status, COUNT(*) n FROM pages WHERE superseded=0 GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def confirm_page(pid: int, by: str = "") -> None:
    """Mark confirmed; any earlier confirmed copy of the same page is superseded."""
    with _lock:
        p = get_page(pid)
        db().execute("UPDATE pages SET superseded=1, updated_at=? WHERE status='confirmed' AND id<>? "
                     "AND entry_id=? AND event=? AND judge=? AND page=?",
                     (_now(), pid, p["entry_id"], p["event"], p["judge"], p["page"]))
        db().execute("UPDATE pages SET status='confirmed', superseded=0, confirmed_by=?, updated_at=? WHERE id=?",
                     (by, _now(), pid))
        db().commit()


def find_duplicate(pid: int) -> Optional[dict]:
    p = get_page(pid)
    if not p or not p["entry_id"]:
        return None
    r = db().execute("SELECT * FROM pages WHERE status='confirmed' AND superseded=0 AND id<>? "
                     "AND entry_id=? AND event=? AND judge=? AND page=?",
                     (pid, p["entry_id"], p["event"], p["judge"], p["page"])).fetchone()
    return row_to_dict(r) if r else None


def confirmed_sheet_values() -> tuple:
    """-> ({(entry_id, judge): merged values}, {(entry_id, judge): judge_name})"""
    rows = db().execute("SELECT * FROM pages WHERE status='confirmed' AND superseded=0 "
                        "ORDER BY entry_id, judge, page").fetchall()
    vals, names, pages_seen = {}, {}, {}
    for r in rows:
        d = row_to_dict(r)
        k = (d["entry_id"], d["judge"])
        vals.setdefault(k, {}).update(d["values"])
        pages_seen.setdefault(k, set()).add(d["page"])
        if d.get("judge_name"):
            names[k] = d["judge_name"]
    return vals, names, pages_seen


def confirmed_comments() -> dict:
    """-> {(entry_id, judge): comments text from all confirmed pages of that judge's sheet}"""
    rows = db().execute("SELECT entry_id, judge, comments FROM pages WHERE status='confirmed' AND superseded=0 "
                        "AND comments IS NOT NULL AND TRIM(comments)<>'' ORDER BY entry_id, judge, page").fetchall()
    out = {}
    for r in rows:
        k = (r["entry_id"], r["judge"])
        out[k] = (out[k] + "\n" if k in out else "") + r["comments"].strip()
    return out


def clear_all(photo_dir: Path) -> Path:
    """Back up the database + photos to data/backups/<time>/, then empty the pages table.
    Nothing is deleted: copy the backup's mgdm.db (and photos) back to undo."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = DATA_DIR / "backups" / f"cleared_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    with _lock:
        bak = sqlite3.connect(str(dest / "mgdm.db"))
        db().backup(bak)
        bak.close()
        if photo_dir.exists():
            shutil.move(str(photo_dir), str(dest / "photos"))
        db().execute("DELETE FROM pages")
        db().commit()
    return dest
