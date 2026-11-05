"""Flask web app: phone capture, batch upload, review queue, standings."""
from __future__ import annotations

import io
import logging
import secrets
import zipfile
from datetime import date
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request, send_file, session,
                   url_for)

from . import db
from .config import DATA_DIR, ROOT, fmt_pts
from .reader import api_available, read_sheet
from .service import STATE, build_tables, publish, safe_name, school_workbook, schools
from .sheets_pdf import generate, page_fields, paginate

log = logging.getLogger(__name__)
PHOTO_DIR = DATA_DIR / "photos"
INBOX_DIR = DATA_DIR / "inbox"
SHEETS_DIR = ROOT / "output" / "sheets"
ALLOWED = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".pdf"}

app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024 * 1024
_key_file = DATA_DIR / "secret.key"
DATA_DIR.mkdir(parents=True, exist_ok=True)
if not _key_file.exists():
    _key_file.write_text(secrets.token_hex(32))
app.secret_key = _key_file.read_text().strip()

_pool = ThreadPoolExecutor(max_workers=int(STATE.cfg.settings.get("reader", {}).get("workers", 4)))


# ------------------------------------------------------------ helpers
def lan_url() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except Exception:  # noqa: BLE001
        ip = "127.0.0.1"
    return f"http://{ip}:{STATE.cfg.settings.get('app', {}).get('port', 5050)}"


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        code = str(STATE.cfg.settings.get("app", {}).get("passcode") or "")
        if code and session.get("ok") != code:
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def reread_options() -> list:
    """[(value, label)] for the re-read dropdown; first one = settings.yaml default."""
    rs = STATE.cfg.settings.get("reader", {})
    out = [("", f"Default ({rs.get('model', '')} / {rs.get('effort', 'medium')})")]
    for o in rs.get("reread_options") or []:
        out.append((f"{o['model']}|{o['effort']}", o.get("label") or f"{o['model']} / {o['effort']}"))
    return out


def process_page(pid: int, hint=None, model=None, effort=None) -> None:
    p = db.get_page(pid)
    try:
        data = Path(p["image_path"]).read_bytes()
        res = read_sheet(STATE.cfg, data, hint, model=model, effort=effort)
        ident = res["ident"] or {}
        entry_id = ident.get("entry_id")
        if entry_id and entry_id not in STATE.entries:
            res["notes"] = (res["notes"] + f" | entry {entry_id} not in entries.csv").strip(" |")
        db.update_page(pid, status="review", qr_text=res["qr"], entry_id=entry_id,
                       event=ident.get("event"), judge=ident.get("judge"), page=ident.get("page"),
                       values=res["values"], ai_values=res["values"], uncertain=res["uncertain"],
                       judge_name=res["judge_name"], comments=res["comments"], notes=res["notes"],
                       error=res["error"], model=res["model"])
    except Exception as e:  # noqa: BLE001
        log.exception("reading page %s failed", pid)
        db.update_page(pid, status="review", error=f"Reader crashed: {e}")


def _is_blank(jpg: bytes) -> bool:
    """Blank backs from a duplex scan: almost no dark pixels."""
    import cv2
    import numpy as np
    g = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
    if g is None:
        return False
    g = cv2.resize(g, (400, int(400 * g.shape[0] / g.shape[1])))
    return float((g < 140).mean()) < 0.004


def _pdf_pages(data: bytes) -> list:
    """-> JPEG bytes for every page of a scanned PDF."""
    import fitz
    dpi = int(STATE.cfg.settings.get("batch", {}).get("pdf_dpi", 220))
    out = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for pg in doc:
            out.append(pg.get_pixmap(dpi=dpi, colorspace=fitz.csRGB).tobytes("jpeg", jpg_quality=90))
    return out


def ingest(data: bytes, original_name: str) -> tuple:
    """Store an upload (image or multi-page PDF) and queue each sheet for reading.
    -> (number of sheets queued, number of blank pages skipped)"""
    ext = Path(original_name or "photo.jpg").suffix.lower() or ".jpg"
    if ext not in ALLOWED:
        raise ValueError(f"{original_name}: not an image or PDF")
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    if ext == ".pdf":
        pages = _pdf_pages(data)
        skip_blank = STATE.cfg.settings.get("batch", {}).get("skip_blank_pages", True)
        items, blanks = [], 0
        for i, jpg in enumerate(pages, 1):
            if skip_blank and _is_blank(jpg):
                blanks += 1
                continue
            items.append((jpg, ".jpg", f"{original_name} p{i}" if len(pages) > 1 else original_name))
    else:
        items, blanks = [(data, ext, original_name)], 0
    for blob, x, name in items:
        path = PHOTO_DIR / f"{uuid.uuid4().hex}{x}"
        path.write_bytes(blob)
        pid = db.create_page(str(path), name)
        _pool.submit(process_page, pid)
    return len(items), blanks


def save_upload(fs) -> tuple:
    return ingest(fs.read(), fs.filename or "photo.jpg")


@app.context_processor
def inject():
    return {"cfg": STATE.cfg, "counts": db.counts(), "api_on": api_available(),
            "sync": STATE.sync_status, "fmt_pts": fmt_pts}


# ------------------------------------------------------------ auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("code", "").strip() == str(STATE.cfg.settings.get("app", {}).get("passcode", "")):
            session["ok"] = request.form["code"].strip()
            session["name"] = request.form.get("name", "").strip()
            session.permanent = True
            return redirect(request.args.get("next") or url_for("home"))
        flash("Wrong passcode")
    return render_template("login.html")


# ------------------------------------------------------------ pages
@app.route("/")
@login_required
def home():
    return render_template("home.html", url=lan_url(), entries=len(STATE.entries))


@app.route("/capture")
@login_required
def capture():
    return render_template("capture.html")


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    ok, blank, bad = 0, 0, []
    for f in files:
        try:
            n, b = save_upload(f)
            ok, blank = ok + n, blank + b
        except Exception as e:  # noqa: BLE001
            bad.append(str(e))
    flash(f"{ok} sheet page(s) uploaded - reading now."
          + (f" {blank} blank page(s) skipped." if blank else "")
          + (" Problems: " + "; ".join(bad) if bad else ""))
    return redirect(request.form.get("back") or url_for("capture"))


_inbox_lock = threading.Lock()


def _inbox_files(min_age: float = 0) -> list:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    return sorted(p for p in INBOX_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in ALLOWED and now - p.stat().st_mtime >= min_age)


def scan_inbox(min_age: float = 0) -> tuple:
    """Take every finished file out of data/inbox. -> (sheets queued, blanks skipped, files, errors)"""
    with _inbox_lock:
        done, failed = INBOX_DIR / "processed", INBOX_DIR / "failed"
        sheets = blanks = files = 0
        errors = []
        for p in _inbox_files(min_age):
            try:
                size = p.stat().st_size
                time.sleep(0.2)
                if p.stat().st_size != size:          # scanner still writing it
                    continue
                n, b = ingest(p.read_bytes(), p.name)
                done.mkdir(parents=True, exist_ok=True)
                p.rename(_unique(done / p.name))
                sheets, blanks, files = sheets + n, blanks + b, files + 1
            except Exception as e:  # noqa: BLE001
                log.exception("inbox file %s failed", p.name)
                failed.mkdir(parents=True, exist_ok=True)
                p.rename(_unique(failed / p.name))
                errors.append(f"{p.name}: {e}")
        if files:
            log.info("inbox: %d file(s) -> %d sheet page(s), %d blank skipped", files, sheets, blanks)
        return sheets, blanks, files, errors


def _unique(path: Path) -> Path:
    i = 1
    out = path
    while out.exists():
        out = path.with_name(f"{path.stem}_{i}{path.suffix}")
        i += 1
    return out


WATCH = {"on": False, "last": None}


def start_inbox_watcher() -> None:
    """Poll data/inbox every few seconds so a scanner can save straight into it."""
    b = STATE.cfg.settings.get("batch", {})
    if not b.get("watch_inbox", True) or WATCH["on"]:
        return
    every = float(b.get("watch_seconds", 5))

    def loop():
        while True:
            try:
                n, blank, files, errs = scan_inbox(min_age=3)   # 3s quiet = scanner finished the file
                if files or errs:
                    WATCH["last"] = {"at": time.strftime("%H:%M:%S"), "sheets": n, "blank": blank,
                                     "files": files, "errors": errs}
            except Exception:  # noqa: BLE001
                log.exception("inbox watcher")
            time.sleep(every)

    WATCH["on"] = True
    threading.Thread(target=loop, daemon=True, name="inbox-watcher").start()
    log.info("watching %s for scans every %ss", INBOX_DIR, every)


@app.route("/batch")
@login_required
def batch():
    waiting = [p.name for p in _inbox_files()]
    return render_template("batch.html", inbox=str(INBOX_DIR), waiting=waiting, watch=WATCH)


@app.route("/batch/inbox", methods=["POST"])
@login_required
def batch_inbox():
    n, blank, files, errs = scan_inbox()
    flash(f"{files} file(s) taken from the inbox: {n} sheet page(s) reading now."
          + (f" {blank} blank page(s) skipped." if blank else "")
          + (" Problems (moved to inbox/failed): " + "; ".join(errs) if errs else ""))
    return redirect(url_for("review_list"))


@app.route("/review")
@login_required
def review_list():
    return render_template("review_list.html", pages=db.list_pages("review"),
                           reading=db.list_pages("reading"), entries=STATE.entries)


@app.route("/history")
@login_required
def history():
    return render_template("history.html", pages=db.list_pages(limit=2000), entries=STATE.entries)


@app.route("/photo/<int:pid>")
@login_required
def photo(pid):
    p = db.get_page(pid) or abort(404)
    path = Path(p["image_path"])
    if path.suffix.lower() in (".heic", ".heif", ".tif", ".tiff"):   # browsers can't show these
        jpg = path.with_suffix(".view.jpg")
        if not jpg.exists():
            from PIL import Image, ImageOps
            ImageOps.exif_transpose(Image.open(path)).convert("RGB").save(jpg, "JPEG", quality=88)
        path = jpg
    return send_file(path)


def _form_values(fields, form) -> dict:
    out = {}
    for f in fields:
        k = f.key
        if f.kind == "choice":
            v = form.get(k, "")
            out[k] = None if v == "" else type(f.values[0])(v)
        elif f.kind == "multi":
            out[k] = sorted(int(x) for x in form.getlist(k))
        elif f.kind == "flag":
            out[k] = form.get(k) == "1"
        elif f.kind == "count":
            v = form.get(k, "").strip()
            out[k] = int(v) if v.isdigit() else None
        else:
            out[k] = form.get(k, "")
    return out


@app.route("/page/<int:pid>", methods=["GET", "POST"])
@login_required
def page(pid):
    p = db.get_page(pid) or abort(404)
    cfg = STATE.cfg
    if request.method == "POST":
        action = request.form.get("action")
        if action == "identity":
            ev = request.form.get("event") or None
            judge = int(request.form.get("judge") or 1)
            pg = int(request.form.get("page") or 1)
            entry = request.form.get("entry_id") or None
            if ev and judge > cfg.events[ev].judges:
                judge = 1
            if ev and pg > len(paginate(cfg.events[ev], judge)):
                pg = 1
            db.update_page(pid, event=ev, judge=judge, page=pg, entry_id=entry)
            return redirect(url_for("page", pid=pid))
        if action == "reread":
            db.update_page(pid, status="reading", error=None)
            hint = None
            if p["event"]:
                hint = {"event": p["event"], "judge": p["judge"] or 1, "page": p["page"] or 1,
                        "entry_id": p["entry_id"]}
            choice = request.form.get("reread_with", "")
            if choice and choice not in {v for v, _ in reread_options()}:
                choice = ""
            model, effort = choice.split("|") if choice else (None, None)
            _pool.submit(process_page, pid, hint, model, effort)
            flash(f"Re-reading page {pid}" + (f" with {model} / {effort}" if model else "") + "...")
            return redirect(url_for("review_list"))
        if action == "reject":
            db.update_page(pid, status="rejected")
            publish()
            flash(f"Page {pid} rejected (kept in history).")
            return redirect(_next_review(pid))
        if not p["event"]:
            flash("Pick the event / judge / page first.")
            return redirect(url_for("page", pid=pid))
        ev = cfg.events[p["event"]]
        fields = page_fields(ev, p["judge"], p["page"])
        db.update_page(pid, values=_form_values(fields, request.form),
                       judge_name=request.form.get("judge_name", ""),
                       comments=request.form.get("comments", "").strip())
        if action == "confirm":
            e = STATE.entries.get(p["entry_id"] or "")
            if not e or e.event != p["event"]:
                flash("Choose the school/entry for this sheet before confirming.")
                return redirect(url_for("page", pid=pid))
            db.confirm_page(pid, session.get("name", ""))
            publish()
            flash(f"Page {pid} confirmed: {e.display}, {ev.abbr} judge #{p['judge']}.")
            return redirect(_next_review(pid))
        flash("Saved.")
        return redirect(url_for("page", pid=pid))

    ev = cfg.events.get(p["event"]) if p["event"] else None
    fields = page_fields(ev, p["judge"], p["page"]) if ev else []
    n_pages = len(paginate(ev, p["judge"])) if ev else 1
    groups = []
    for f in fields:
        title = ev.sections[f.section].title if f.section in ev.sections else "Sheet header"
        if not groups or groups[-1][0] != title:
            groups.append((title, []))
        groups[-1][1].append(f)
    event_entries = sorted([e for e in STATE.entries.values() if not ev or e.event == ev.id],
                           key=lambda e: e.display)
    return render_template("page.html", p=p, ev=ev, groups=groups, n_pages=n_pages,
                           entries=event_entries, entry=STATE.entries.get(p["entry_id"] or ""),
                           dup=db.find_duplicate(pid), reread_opts=reread_options())


def _next_review(pid):
    pending = [x for x in db.list_pages("review") if x["id"] != pid]
    return url_for("page", pid=pending[-1]["id"]) if pending else url_for("review_list")


@app.route("/standings")
@login_required
def standings():
    return render_template("standings.html", tables=build_tables())


@app.route("/download/excel")
@login_required
def download_excel():
    publish(background=True)
    path = Path(STATE.sync_status["excel"]["path"])
    return send_file(path, as_attachment=True, download_name=path.name)


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.route("/schools")
@login_required
def schools_page():
    rows = []
    for sch in schools():
        mine = [e for e in STATE.entries.values() if e.school == sch]
        events = sorted({STATE.cfg.events[e.event].abbr for e in mine})
        rows.append((sch, len(mine), ", ".join(events)))
    return render_template("schools.html", rows=rows)


@app.route("/download/school")
@login_required
def download_school():
    sch = request.args.get("school", "")
    if sch not in schools():
        abort(404)
    name = f"{STATE.cfg.meet['code']}_{safe_name(sch)}_Scores.xlsx"
    return send_file(io.BytesIO(school_workbook(sch)), mimetype=XLSX, as_attachment=True, download_name=name)


@app.route("/download/schools.zip")
@login_required
def download_all_schools():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sch in schools():
            z.writestr(f"{STATE.cfg.meet['code']}_{safe_name(sch)}_Scores.xlsx", school_workbook(sch))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{STATE.cfg.meet['code']}_school_reports_{date.today():%Y%m%d}.zip")


@app.route("/sheets", methods=["GET", "POST"])
@login_required
def sheets():
    if request.method == "POST":
        STATE.reload()
        blanks = int(request.form.get("blanks") or 2)
        by = "school" if request.form.get("by") == "school" else "event"
        written = generate(STATE.cfg, STATE.entries, SHEETS_DIR, blanks=blanks, by=by)
        flash(f"{len(written)} scoresheet PDF(s) generated, grouped by {by}.")
        return redirect(url_for("sheets"))
    files = sorted(SHEETS_DIR.glob("**/*.pdf")) if SHEETS_DIR.exists() else []
    names = [f.relative_to(SHEETS_DIR).as_posix() for f in files]
    return render_template("sheets.html", ev_files=[n for n in names if "/" not in n],
                           sch_files=[n for n in names if n.startswith("by_school/")])


@app.route("/sheets/<path:name>")
@login_required
def sheet_file(name):
    path = (SHEETS_DIR / name).resolve()
    if SHEETS_DIR.resolve() not in path.parents or path.suffix != ".pdf" or not path.exists():
        abort(404)
    return send_file(path)


@app.route("/reload", methods=["POST"])
@login_required
def reload_config():
    try:
        STATE.reload()
        publish()
        flash(f"Config reloaded: {len(STATE.cfg.events)} events, {len(STATE.entries)} entries. Scores recalculated.")
    except Exception as e:  # noqa: BLE001
        flash(f"Config error - nothing changed: {e}")
    return redirect(url_for("home"))


@app.route("/admin/clear", methods=["GET", "POST"])
@login_required
def clear_scores():
    master = str(STATE.cfg.settings.get("app", {}).get("admin_password") or "")
    if request.method == "POST":
        if not master:
            flash("Clearing is switched off: set admin_password in config/settings.yaml first.")
        elif not secrets.compare_digest(request.form.get("password", ""), master):
            log.warning("clear-all refused: wrong master password (by %s)", session.get("name", "?"))
            flash("Wrong master password - nothing was cleared.")
        elif request.form.get("confirm", "").strip() != "CLEAR ALL SCORES":
            flash('Type CLEAR ALL SCORES exactly to confirm - nothing was cleared.')
        else:
            n = sum(db.counts().values())
            dest = db.clear_all(PHOTO_DIR)
            publish()
            log.warning("ALL SCORES CLEARED by %s; backup in %s", session.get("name", "?"), dest)
            flash(f"Cleared {n} page(s). A full backup was saved to {dest.relative_to(ROOT)}.")
            return redirect(url_for("home"))
        return redirect(url_for("clear_scores"))
    return render_template("clear.html", enabled=bool(master), total=sum(db.counts().values()))


@app.route("/publish", methods=["POST"])
@login_required
def publish_now():
    publish(background=False)
    flash("Excel rebuilt" + (" and Google Sheet updated." if (STATE.sync_status.get("google") or {}).get("ok")
                             else "."))
    return redirect(url_for("home"))
