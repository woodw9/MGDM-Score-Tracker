"""Glue: database -> scoring -> result tables -> Excel master + Google mirror."""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import db
from .config import ROOT, Config, fmt_pts, load_config, load_entries
from .scoring import compute_all
from .sheets_pdf import paginate
from . import live

log = logging.getLogger(__name__)


class State:
    """Config + entries, reloadable without restarting."""
    def __init__(self):
        self.reload()
        self.sync_status = {"excel": None, "google": None}

    def reload(self):
        cfg = load_config()              # load both first so a bad file changes nothing
        entries = load_entries(cfg)
        self.cfg, self.entries = cfg, entries


STATE = State()


# ------------------------------------------------------------ compute
def compute():
    cfg, entries = STATE.cfg, STATE.entries
    vals, names, pages_seen = db.confirmed_sheet_values()
    complete_vals, partial = {}, {}
    for (entry_id, judge), v in vals.items():
        e = entries.get(entry_id)
        if not e:
            continue
        need = len(paginate(cfg.events[e.event], judge))
        if len(pages_seen[(entry_id, judge)]) >= need:
            complete_vals[(entry_id, judge)] = v
        else:
            partial[(entry_id, judge)] = need - len(pages_seen[(entry_id, judge)])
    event_results, overall = compute_all(cfg, entries, complete_vals)
    for res in event_results.values():
        for r in res:
            for (eid, j), n in partial.items():
                if eid == r.entry.entry_id:
                    r.notes.append(f"judge #{j}: {n} page(s) not yet confirmed")
    return event_results, overall, names


def _multi_text(f, v):
    return "; ".join(f.options[i - 1].label for i in (v or []))


def build_tables() -> dict:
    """-> {tab name: [header row, *rows]} in display order."""
    cfg = STATE.cfg
    event_results, overall, names = compute()
    tables = {}

    counted = [e for e in cfg.overall["events"] if e not in (cfg.overall.get("excluded_events") or [])]
    hdr = ["Rank", "School"]
    for eid in counted:
        hdr += [f"{cfg.events[eid].name} (raw)", f"{cfg.events[eid].name} (scaled)"]
    hdr += ["OVERALL", f"Tiebreak: {cfg.events[cfg.overall['tiebreak_event']].name}"]
    rows = [hdr]
    for s in overall:
        row = [s.rank if s.per_event else "", s.school]
        for eid in counted:
            raw, scaled, _ = s.per_event.get(eid, ("", "", ""))
            row += [raw, scaled]
        rows.append(row + [s.total, s.tiebreak])
    tables["Overall"] = rows

    for eid, ev in cfg.events.items():
        res = event_results.get(eid, [])
        hdr = ["Rank", "Entry", "School", "Participant" if ev.individual else "Team"]
        hdr += [f"Judge {j}" for j in range(1, ev.judges + 1)]
        hdr += ["Judges combined" if ev.combine == "sum" else "Judge average", "Penalties", "TOTAL",
                "Max possible", "% of max", "Status / notes"]
        rows = [hdr]
        for r in res:
            row = [r.rank or ("DQ" if r.dq else ""), r.entry.entry_id, r.entry.school,
                   r.entry.participant if ev.individual else r.entry.team]
            row += [r.judge_points.get(j, "") for j in range(1, ev.judges + 1)]
            pen = "; ".join(f"{l} -{fmt_pts(a)}" for l, a in r.penalty_lines)
            row += [round(r.combined, 2), (f"-{fmt_pts(r.penalty_total)}  ({pen})" if pen else 0),
                    r.total if r.sheets else "", cfg.event_max(eid),
                    round(100 * r.total / cfg.event_max(eid), 1) if r.sheets else "",
                    "; ".join(r.notes) or ("Complete" if r.complete else "")]
            rows.append(row)
        tables[f"{ev.abbr} - {ev.name}"[:31]] = rows

    # raw marks per event (one row per judge sheet) for further grading
    vals, _, _ = db.confirmed_sheet_values()
    comments = db.confirmed_comments()
    for eid, ev in cfg.events.items():
        tables[f"Raw {ev.abbr}"] = raw_rows(ev, event_results.get(eid, []), vals, names, comments)

    audit = [["Page ID", "Status", "Entry", "Event", "Judge", "Page", "Uncertain marks",
              "Marks changed by tabulator", "Confirmed by", "Updated", "Photo"]]
    for p in db.list_pages(limit=100000):
        changed = sum(1 for k, v in (p["values"] or {}).items() if (p["ai_values"] or {}).get(k, v) != v)
        audit.append([p["id"], p["status"] + (" (replaced)" if p["superseded"] else ""), p["entry_id"],
                      p["event"], p["judge"], p["page"], len(p["uncertain"] or []), changed,
                      p["confirmed_by"] or "", p["updated_at"], p["original_name"] or ""])
    tables["Audit Log"] = audit
    return tables


def raw_rows(ev, results, vals, names, comments, with_judge_names=True) -> list:
    """One row per confirmed judge sheet: every mark, sheet points and the judge's comments."""
    all_fields, seen = [], set()
    for j in range(1, ev.judges + 1):
        for f in ev.sheet_fields(j):
            if f.key not in seen:
                seen.add(f.key)
                all_fields.append(f)
    hdr = ["Entry", "School", "Team/Participant", "Judge"] + (["Judge name"] if with_judge_names else [])
    hdr += ["Sheet points", "Judge comments"]
    for f in all_fields:
        lab = f"{f.number}. {f.label}" if f.number else f.label
        if f.group:
            lab += f" [{f.group}]"
        if f.kind == "multi":
            lab += " (missed)" if f.role != "deduct_grid" else " (discrepancies)"
        hdr.append(f"{f.section}: {lab}" if f.section != "_header" else lab)
    rows = [hdr]
    for r in results:
        for j, s in sorted(r.sheets.items()):
            k = (r.entry.entry_id, j)
            v = vals.get(k, {})
            row = [r.entry.entry_id, r.entry.school, r.entry.participant or r.entry.team, j]
            row += [names.get(k, "")] if with_judge_names else []
            row += [s.points, comments.get(k, "")]
            for f in all_fields:
                x = v.get(f.key, "")
                row.append(_multi_text(f, x) if f.kind == "multi" else ("YES" if x is True else
                           ("" if x in (None, False) else x)))
            rows.append(row)
    return rows


def schools() -> list:
    return sorted({e.school for e in STATE.entries.values()}, key=str.lower)


def school_tables(school: str) -> dict:
    """Score report for one school: summary of every entry + per-event judge sheets and comments."""
    cfg = STATE.cfg
    event_results, overall, names = compute()
    vals, _, _ = db.confirmed_sheet_values()
    comments = db.confirmed_comments()
    tables = {}

    summ = [["Event", "Entry", "Team / Participant", "Place", "Teams ranked", "Judge scores",
             "Judges combined", "Penalties", "TOTAL", "Max possible", "% of max", "Status / notes"]]
    for eid, ev in cfg.events.items():
        res = event_results.get(eid, [])
        ranked = sum(1 for r in res if r.rank)
        for r in res:
            if r.entry.school != school:
                continue
            pen = "; ".join(f"{l} -{fmt_pts(a)}" for l, a in r.penalty_lines)
            js = ", ".join(f"J{j}: {fmt_pts(p)}" for j, p in sorted(r.judge_points.items()))
            summ.append([ev.name, r.entry.entry_id, r.entry.participant if ev.individual else r.entry.team,
                         r.rank or ("DQ" if r.dq else ""), ranked or "", js,
                         round(r.combined, 2) if r.sheets else "",
                         (f"-{fmt_pts(r.penalty_total)}  ({pen})" if pen else 0) if r.sheets else "",
                         r.total if r.sheets else "", cfg.event_max(eid),
                         round(100 * r.total / cfg.event_max(eid), 1) if r.sheets else "",
                         "; ".join(r.notes) or ("Complete" if r.complete else "No sheets confirmed yet")])
    mine = next((s for s in overall if s.school == school), None)
    if mine:
        n = sum(1 for s in overall if s.per_event)
        summ += [[], ["OVERALL", "", "", (mine.rank if mine.per_event else ""), n or "", "", "", "",
                      mine.total, "", "", "Best team per event counts toward overall"]]
    tables["Summary"] = summ

    for eid, ev in cfg.events.items():
        res = [r for r in event_results.get(eid, []) if r.entry.school == school and r.sheets]
        if res:
            tables[f"{ev.abbr} judge sheets"] = raw_rows(ev, res, vals, names, comments, with_judge_names=False)
    return tables


# ------------------------------------------------------------ Excel
HEAD_FILL = PatternFill("solid", fgColor="4B1F78")
HEAD_FONT = Font(bold=True, color="FFFFFF")


def _style_sheets(wb, tables: dict) -> None:
    for name, rows in tables.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
        for c in ws[1]:
            c.fill, c.font = HEAD_FILL, HEAD_FONT
            c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "C2" if not (name.startswith("Raw") or name.endswith("judge sheets")) else "E2"
        for i, col in enumerate(ws.columns, 1):
            width = max((len(str(c.value)) for c in col[1:] if c.value is not None), default=6)
            width = max(width, min(len(str(col[0].value or "")), 18))
            ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 7), 60 if i <= 4 else 28)
            if str(col[0].value) == "Judge comments":
                ws.column_dimensions[get_column_letter(i)].width = 50
                for c in col[1:]:
                    c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[1].height = 42


def live_grids() -> list:
    """The two Live Scoresheet tabs (see live.py)."""
    event_results, overall, _ = compute()
    return live.build_live(STATE.cfg, STATE.entries, event_results, overall)


def write_excel(tables: dict, grids: list = None) -> Path:
    cfg = STATE.cfg
    path = ROOT / cfg.settings.get("excel", {}).get("path", "output/MGDM_2027_Master_Scores.xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for g in grids or []:
        live.write_xlsx(wb, g)
    _style_sheets(wb, tables)
    ws = wb.create_sheet("Info")
    ws.append(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    ws.append(["Judge combine rule", cfg.raw.get("judge_combine")])
    ws.append(["Overall method", cfg.overall.get("method")])
    for eid, o in cfg.overall["events"].items():
        ws.append([f"Benchmark {cfg.events[eid].name}", cfg.benchmark(eid), "weight", o.get("weight", 100)])
    ws.append(["Excluded events", ", ".join(cfg.overall.get("excluded_events") or []) or "none"])
    ws.append(["NOTE", "This file is rebuilt automatically from the scoring database. "
                       "Edits made here are overwritten - copy it before adding your own formulas."])
    tmp = path.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, path)
    STATE.sync_status["excel"] = {"ok": True, "at": datetime.now().strftime("%H:%M:%S"), "path": str(path)}
    return path


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "school"


def school_workbook(school: str) -> bytes:
    """-> .xlsx bytes of one school's score report."""
    wb = Workbook()
    wb.remove(wb.active)
    _style_sheets(wb, school_tables(school))
    ws = wb["Summary"]
    ws.insert_rows(1, 2)
    ws["A1"] = f"{STATE.cfg.meet['name']} - score report for {school}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Generated {datetime.now().strftime('%d %b %Y %H:%M')}"
    ws.freeze_panes = "C4"
    ws.row_dimensions[1].height = ws.row_dimensions[2].height = 18
    ws.row_dimensions[3].height = 42
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------ Google Sheets
_gs_lock = threading.Lock()


def _sync_spreadsheet(sh, tables: dict, grids: list) -> None:
    """Write every tab with a handful of API calls (Google allows 60 writes/minute)."""
    titles = [g.title for g in grids] + list(tables)
    have = {w.title: w for w in sh.worksheets()}
    missing = [t for t in titles if t not in have]
    if missing:                                                      # write 1
        sh.batch_update({"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]})
        have = {w.title: w for w in sh.worksheets()}
    plain = {n: [["" if v is None else v for v in r] for r in rows] for n, rows in tables.items()}
    sizes = {g.title: (g.nrows, g.ncols) for g in grids}
    sizes.update({n: (len(rows) + 1, max(len(r) for r in rows)) for n, rows in plain.items()})  # +1: frozen header can't be every row
    reqs = []
    for i, t in enumerate(titles):
        sid = have[t].id
        nr, nc = sizes[t]
        grid = {"rowCount": nr, "columnCount": nc}
        if t in plain:
            grid["frozenRowCount"] = 1
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "index": i, "gridProperties": grid},
                     "fields": "index," + ",".join("gridProperties." + k for k in grid)}})
    sh.batch_update({"requests": reqs})                              # write 2: sizes + tab order
    if plain:
        sh.values_batch_clear(body={"ranges": [f"'{t}'" for t in plain]})  # write 3
    data = [{"range": live.a1(g.title), "values": live.google_values(g)} for g in grids]
    data += [{"range": live.a1(n), "values": rows} for n, rows in plain.items()]
    sh.values_batch_update({"valueInputOption": "RAW", "data": data})  # write 4
    fmt = []
    for g in grids:
        fmt += live.google_requests(g, have[g.title].id)
    if fmt:
        sh.batch_update({"requests": fmt})                           # write 5: live-tab formatting


def sync_google(tables: dict, grids: list = None) -> None:
    """spreadsheet_id      -> internal sheet: Live Scoresheet tabs + every data tab
    live_spreadsheet_id -> public sheet for the schools: Live Scoresheet tabs only"""
    gs = STATE.cfg.settings.get("google_sheets", {}) or {}
    if not gs.get("enabled"):
        STATE.sync_status["google"] = {"ok": None, "msg": "disabled in settings.yaml"}
        return
    with _gs_lock:
        try:
            import gspread
            client = gspread.service_account(filename=str(ROOT / gs["credentials_file"]))
            done = []
            if gs.get("spreadsheet_id"):
                _sync_spreadsheet(client.open_by_key(gs["spreadsheet_id"]), tables, grids or [])
                done.append("internal")
            if gs.get("live_spreadsheet_id") and grids:
                _sync_spreadsheet(client.open_by_key(gs["live_spreadsheet_id"]), {}, grids)
                done.append("live")
            STATE.sync_status["google"] = {"ok": True, "at": datetime.now().strftime("%H:%M:%S"),
                                           "msg": " + ".join(done)}
        except Exception as e:  # noqa: BLE001
            log.exception("Google Sheets sync failed")
            STATE.sync_status["google"] = {"ok": False, "msg": str(e)[:300],
                                           "at": datetime.now().strftime("%H:%M:%S")}


# Confirming many sheets quickly would otherwise start one sync per click.
# One worker syncs the newest scores, then waits before syncing again.
_pending = {"data": None}
_pending_lock = threading.Lock()
_worker = {"running": False}


def _google_worker() -> None:
    gap = float((STATE.cfg.settings.get("google_sheets") or {}).get("min_seconds_between_syncs", 15))
    while True:
        with _pending_lock:
            data, _pending["data"] = _pending["data"], None
            if data is None:
                _worker["running"] = False
                return
        sync_google(*data)
        time.sleep(gap)


def queue_google(tables: dict, grids: list) -> None:
    with _pending_lock:
        _pending["data"] = (tables, grids)
        if _worker["running"]:
            return
        _worker["running"] = True
    threading.Thread(target=_google_worker, daemon=True, name="google-sync").start()


def publish(background: bool = True) -> None:
    """Rebuild Excel now, push to Google (in the background by default)."""
    try:
        tables = build_tables()
        grids = live_grids()
        write_excel(tables, grids)
    except Exception as e:  # noqa: BLE001
        log.exception("Excel export failed")
        STATE.sync_status["excel"] = {"ok": False, "msg": str(e)[:300]}
        return
    if background:
        queue_google(tables, grids)
    else:
        sync_google(tables, grids)
