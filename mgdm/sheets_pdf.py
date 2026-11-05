"""Printable bubble scoresheets with QR codes.

`paginate()` decides exactly which fields land on which page. The photo reader
calls the same function, so it always knows what a given page contains.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .config import Config, Entry, Event, Field, fmt_pts

PAGE_W, PAGE_H = letter
MARGIN = 36
CONTENT_TOP = PAGE_H - 132        # below the header block
CONTENT_BOTTOM = 52               # above the footer
LABEL_W = 170                     # left label column
FONT, BOLD = "Helvetica", "Helvetica-Bold"

ROW_H = 17
GRID_ROW_H = 13
CHIP_FONT = 6.6
CHIP_LINE = 12


@dataclass
class Block:
    kind: str
    height: float
    fields: list
    section: object = None
    first_in_group: bool = False


def sheet_id(cfg: Config, entry_id: Optional[str], event: Event, judge: int, page: int, pages: int) -> str:
    return f"{cfg.meet['code']}|{entry_id or 'BLANK'}|{event.abbr}|J{judge}|P{page}of{pages}"


def parse_sheet_id(text: str) -> Optional[dict]:
    try:
        code, entry, abbr, j, p = text.strip().split("|")
        page, pages = p[1:].split("of")
        return {"code": code, "entry_id": None if entry == "BLANK" else entry, "abbr": abbr,
                "judge": int(j[1:]), "page": int(page), "pages": int(pages)}
    except Exception:
        return None


# ------------------------------------------------------------ layout
def _chip_w(f: Field, i: int) -> float:
    o = f.options[i]
    return 12 + stringWidth(f"{o.label} {fmt_pts(o.points)}", FONT, CHIP_FONT) + 9


def _criteria_lines(f: Field) -> int:
    avail = PAGE_W - 2 * MARGIN - LABEL_W
    lines, x = 1, 0.0
    for i in range(len(f.options)):
        w = _chip_w(f, i)
        if x + w > avail and x > 0:
            lines, x = lines + 1, 0.0
        x += w
    return lines


def _blocks(event: Event, judge: int) -> list:
    out = []
    head = [f for f in event.sheet_fields(judge) if f.section == "_header"]
    for f in head:
        out.append(Block("members" if f.role == "members" else "writein", 34 if f.role == "members" else 24, [f]))
    for s in event.sheet_sections(judge):
        out.append(Block("title", 20, [], s))
        if s.type == "ratings":
            out += [Block("rating", ROW_H, [f], s) for f in s.fields]
        elif s.type == "penalties":
            out += [Block("penalty", ROW_H, [f], s) for f in s.fields]
        elif s.type == "criteria":
            out += [Block("criteria", max(22, 6 + CHIP_LINE * _criteria_lines(f)), [f], s) for f in s.fields]
        elif s.type == "rating_table":
            out.append(Block("table_header", 30, [], s))
            rows = {}
            for f in s.fields:
                rows.setdefault(f.number, []).append(f)
            out += [Block("table_row", ROW_H + 2, fs, s) for _, fs in sorted(rows.items())]
        elif s.type == "grid":
            out.append(Block("grid_header", 30, [], s))
            last = None
            for f in s.fields:
                out.append(Block("grid_row", GRID_ROW_H, [f], s, first_in_group=f.group != last))
                last = f.group
    return out


def paginate(event: Event, judge: int) -> list:
    """-> list of pages; each page is a list of Blocks."""
    pages, cur, y = [], [], CONTENT_TOP
    blocks = _blocks(event, judge)
    for i, b in enumerate(blocks):
        need = b.height
        if b.kind in ("title", "table_header", "grid_header") and i + 1 < len(blocks):
            need += blocks[i + 1].height        # keep headings with their first row
        if y - need < CONTENT_BOTTOM and cur:
            pages.append(cur)
            cur, y = [], CONTENT_TOP
            if b.kind in ("table_row", "grid_row"):   # repeat column headers
                hk = "table_header" if b.kind == "table_row" else "grid_header"
                cur.append(Block(hk, 30, [], b.section))
                y -= 30
        cur.append(b)
        y -= b.height
    if cur:
        pages.append(cur)
    return pages


def page_fields(event: Event, judge: int, page: int) -> list:
    pages = paginate(event, judge)
    return [f for b in pages[page - 1] for f in b.fields]


# ------------------------------------------------------------ drawing
def _qr_image(text: str):
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=1)
    q.add_data(text)
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


# Practice/test sheets: values to pre-fill like a judge would (see make_practice)
_MARKS: dict = {}


def _pen_fill(c, x, y, r):
    import random
    c.setFillColorRGB(0.08, 0.08, 0.2)
    c.circle(x + random.uniform(-0.6, 0.6), y + random.uniform(-0.6, 0.6), r * random.uniform(0.85, 1.05),
             stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)


def _bubble(c, x, y, r=5.4, text="", filled=False):
    c.setLineWidth(0.8)
    c.setStrokeColorRGB(0, 0, 0)
    c.circle(x, y, r, stroke=1, fill=0)
    if filled:
        _pen_fill(c, x, y, r)
        return
    if text != "":
        c.setFillColorRGB(0.35, 0.35, 0.35)
        size = 5.6 if len(str(text)) < 2 else 5.0
        c.setFont(FONT, size)
        c.drawCentredString(x, y - size * 0.36, str(text))
        c.setFillColorRGB(0, 0, 0)


def _bubble_row(c, x, y, values, gap=17, selected=None):
    for i, v in enumerate(values):
        _bubble(c, x + i * gap, y, text=v, filled=selected is not None and selected is not True and v == selected)
    return x + len(values) * gap


def _header(c, cfg, event, judge, entry: Optional[Entry], page, pages, sid, sheet_max):
    # corner registration marks (help straighten photos)
    c.setFillColorRGB(0, 0, 0)
    for (x, y) in [(14, 14), (PAGE_W - 28, 14), (14, PAGE_H - 28), (PAGE_W - 28, PAGE_H - 28)]:
        c.rect(x, y, 14, 14, stroke=0, fill=1)
    qr = 82
    c.drawImage(_qr_image(sid), PAGE_W - MARGIN - qr, PAGE_H - MARGIN - qr, qr, qr)
    c.setFont(FONT, 6.5)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - qr - 7, sid)

    x, y = MARGIN, PAGE_H - MARGIN - 14
    c.setFont(BOLD, 15)
    c.drawString(x, y, f"{cfg.meet['short']}  -  {event.name.upper()}")
    y -= 19
    c.setFont(BOLD, 12.5)
    pg = f"   (page {page} of {pages})" if pages > 1 else ""
    c.drawString(x, y, f"JUDGE SHEET #{judge}{pg}")
    c.setFont(FONT, 9)
    c.drawString(x + 250, y, f"Max points on this sheet: {fmt_pts(sheet_max)}")
    y -= 22
    c.setFont(BOLD, 13)
    label = "PARTICIPANT:" if event.individual else "SCHOOL:"
    if entry:
        c.drawString(x, y, f"{label} {entry.display}")
    else:
        c.drawString(x, y, label)
        c.setLineWidth(0.6)
        c.line(x + stringWidth(label, BOLD, 13) + 6, y - 2, PAGE_W - MARGIN - qr - 12, y - 2)
    y -= 20
    c.setFont(FONT, 10)
    c.drawString(x, y, "JUDGE NAME:")
    c.line(x + 66, y - 2, x + 300, y - 2)
    if _MARKS.get("_judge_name"):
        c.setFont("Helvetica-Oblique", 12)
        c.setFillColorRGB(0.08, 0.08, 0.3)
        c.drawString(x + 75, y, _MARKS["_judge_name"])
        c.setFillColorRGB(0, 0, 0)
    y -= 13
    c.setFont(FONT, 7.5)
    c.setFillColorRGB(0.25, 0.25, 0.25)
    c.drawString(x, y, "Use a dark pen. Fill bubbles completely.  To change an answer, draw an X through the "
                       "wrong bubble and fill the correct one.")
    c.setFillColorRGB(0, 0, 0)
    c.setLineWidth(1.2)
    c.line(MARGIN, CONTENT_TOP + 1, PAGE_W - MARGIN, CONTENT_TOP + 1)


def _footer(c, sid):
    c.setFont(FONT, 6.5)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(MARGIN, 30, "Tabulation: photograph the whole page, all four black corner squares visible.")
    c.drawRightString(PAGE_W - MARGIN, 30, sid)
    c.setFillColorRGB(0, 0, 0)


def _draw_block(c, b: Block, y: float):
    x0 = MARGIN
    top = y
    mid = y - b.height / 2
    if b.kind == "title":
        c.setFillColorRGB(0.88, 0.88, 0.88)
        c.rect(x0, y - 16, PAGE_W - 2 * MARGIN, 15, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        c.setFont(BOLD, 9.5)
        sec = b.section
        mx = f"   (max {fmt_pts(sec.max_points)})" if sec.max_points else ""
        c.drawString(x0 + 4, y - 12, sec.title + mx)
    elif b.kind == "members":
        f = b.fields[0]
        c.setFont(BOLD, 9)
        c.drawString(x0, top - 12, f.label)
        c.setFont(FONT, 7)
        c.drawString(x0, top - 22, "tens / ones")
        mv = _MARKS.get(f.key)
        _bubble_row(c, x0 + LABEL_W, top - 9, list(range(0, 10)), gap=16, selected=None if mv is None else mv // 10)
        _bubble_row(c, x0 + LABEL_W, top - 24, list(range(0, 10)), gap=16, selected=None if mv is None else mv % 10)
        c.setFont(FONT, 6.5)
        c.drawString(x0 + LABEL_W + 165, top - 11, "<- tens")
        c.drawString(x0 + LABEL_W + 165, top - 26, "<- ones   (e.g. 17 = tens 1, ones 7)")
    elif b.kind == "writein":
        f = b.fields[0]
        c.setFont(BOLD, 9)
        c.drawString(x0, mid - 3, f.label + ":")
        c.setLineWidth(0.7)
        c.rect(x0 + LABEL_W, mid - 9, 120, 18, stroke=1, fill=0)
        if _MARKS.get(f.key):
            c.setFont("Helvetica-Oblique", 11)
            c.setFillColorRGB(0.08, 0.08, 0.3)
            c.drawString(x0 + LABEL_W + 6, mid - 4, str(_MARKS[f.key]))
            c.setFillColorRGB(0, 0, 0)
    elif b.kind == "rating":
        f = b.fields[0]
        c.setFont(FONT, 8.5)
        c.drawString(x0 + 4, mid - 3, f"{f.number}. {f.label}")
        _bubble_row(c, x0 + LABEL_W + 6, mid, f.values, selected=_MARKS.get(f.key))
    elif b.kind == "penalty":
        f = b.fields[0]
        c.setFont(FONT, 8.5)
        c.drawString(x0 + 4, mid - 3, f"{f.label}")
        if f.kind == "flag":
            _bubble(c, x0 + LABEL_W + 6, mid, text="Y", filled=bool(_MARKS.get(f.key)))
            c.setFont(FONT, 7)
            c.drawString(x0 + LABEL_W + 16, mid - 2.5, f"YES  (-{fmt_pts(f.points)})")
        else:
            end = _bubble_row(c, x0 + LABEL_W + 6, mid, list(range(0, f.max_count + 1)), selected=_MARKS.get(f.key))
            c.setFont(FONT, 7)
            c.drawString(end, mid - 2.5, f"count  x {fmt_pts(f.points)}")
    elif b.kind == "criteria":
        f = b.fields[0]
        txt = f"{f.number}. {f.label}"
        fs = 8
        while stringWidth(txt, BOLD, fs) > LABEL_W - 8 and fs > 6:
            fs -= 0.25
        c.setFont(BOLD, fs)
        c.drawString(x0 + 2, top - 10, txt)
        c.setFont(FONT, 6.5)
        c.drawString(x0 + 12, top - 18, f"max {fmt_pts(f.max_points)}")
        avail = PAGE_W - 2 * MARGIN - LABEL_W
        x, line = 0.0, 0
        for i, o in enumerate(f.options):
            w = _chip_w(f, i)
            if x + w > avail and x > 0:
                x, line = 0.0, line + 1
            cy = top - 8 - line * CHIP_LINE
            _bubble(c, x0 + LABEL_W + x + 5, cy, r=4.6, filled=(i + 1) in (_MARKS.get(f.key) or []))
            c.setFont(FONT, CHIP_FONT)
            c.drawString(x0 + LABEL_W + x + 12, cy - 2.3, f"{o.label} {fmt_pts(o.points)}")
            x += w
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setLineWidth(0.4)
        c.line(x0, top - b.height, PAGE_W - MARGIN, top - b.height)
        c.setStrokeColorRGB(0, 0, 0)
    elif b.kind == "table_header":
        cols = b.section.columns
        cw = (PAGE_W - 2 * MARGIN - LABEL_W + 40) / len(cols)
        c.setFont(BOLD, 6.8)
        for i, col in enumerate(cols):
            cx = x0 + LABEL_W - 40 + i * cw + cw / 2
            words = col.split()
            half = (len(words) + 1) // 2
            c.drawCentredString(cx, top - 11, " ".join(words[:half]))
            c.drawCentredString(cx, top - 20, " ".join(words[half:]))
    elif b.kind == "table_row":
        cols = b.section.columns
        cw = (PAGE_W - 2 * MARGIN - LABEL_W + 40) / len(cols)
        f0 = b.fields[0]
        c.setFont(FONT, 8.3)
        c.drawString(x0 + 2, mid - 3, f"{f0.number}. {f0.label}")
        for f in b.fields:
            ci = cols.index(f.group)
            n = len(f.values)
            gap = 14.5
            sx = x0 + LABEL_W - 40 + ci * cw + cw / 2 - (n - 1) * gap / 2
            _bubble_row(c, sx, mid, f.values, gap=gap, selected=_MARKS.get(f.key))
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.4)
        c.line(x0, top - b.height, PAGE_W - MARGIN, top - b.height)
        c.setStrokeColorRGB(0, 0, 0)
    elif b.kind == "grid_header":
        cols = b.section.columns
        gx = x0 + LABEL_W + 20
        cw = (PAGE_W - MARGIN - gx) / len(cols)
        for i, o in enumerate(cols):
            cx = gx + i * cw + cw / 2
            c.setFont(BOLD, 7.5)
            c.drawCentredString(cx, top - 12, o.label)
            c.setFont(FONT, 6.3)
            c.drawCentredString(cx, top - 22, f"{fmt_pts(o.points)} pt each")
    elif b.kind == "grid_row":
        f = b.fields[0]
        cols = b.section.columns
        gx = x0 + LABEL_W + 20
        cw = (PAGE_W - MARGIN - gx) / len(cols)
        if b.first_in_group:
            c.setStrokeColorRGB(0.5, 0.5, 0.5)
            c.setLineWidth(0.6)
            c.line(x0, top, PAGE_W - MARGIN, top)
            c.setStrokeColorRGB(0, 0, 0)
            c.setFont(BOLD, 7.2)
            c.drawString(x0, mid - 2.5, f.group)
        c.setFont(FONT, 7.4)
        c.drawString(x0 + 62, mid - 2.5, f"{f.number}. {f.label}")
        for i in range(len(cols)):
            _bubble(c, gx + i * cw + cw / 2, mid, r=4.3, filled=(i + 1) in (_MARKS.get(f.key) or []))


def draw_sheet(c, cfg: Config, event: Event, judge: int, entry: Optional[Entry], marks: Optional[dict] = None):
    global _MARKS
    _MARKS = marks or {}
    pages = paginate(event, judge)
    for p, blocks in enumerate(pages, 1):
        sid = sheet_id(cfg, entry.entry_id if entry else None, event, judge, p, len(pages))
        _header(c, cfg, event, judge, entry, p, len(pages), sid, event.sheet_max(judge))
        y = CONTENT_TOP
        for b in blocks:
            _draw_block(c, b, y)
            y -= b.height
        if p == len(pages) and y - CONTENT_BOTTOM > 45:     # comments box if room
            c.setFont(BOLD, 8.5)
            c.drawString(MARGIN, y - 14, "COMMENTS:")
            c.setLineWidth(0.6)
            c.rect(MARGIN, CONTENT_BOTTOM, PAGE_W - 2 * MARGIN, y - 20 - CONTENT_BOTTOM, stroke=1, fill=0)
        _footer(c, sid)
        c.showPage()


def _safe(text: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "school"


def generate(cfg: Config, entries: dict, out_dir: Path, blanks: int = 2, by: str = "event") -> list:
    """by="event": one PDF per event (every entry x judge, then `blanks` blank sets).
    by="school": output/sheets/by_school/<School>.pdf with every sheet that school needs
    (event by event, entry by entry, judge by judge) + _BLANK_spares.pdf."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    ordered = sorted(entries.values(), key=lambda e: (list(cfg.events).index(e.event)
                                                      if e.event in cfg.events else 99, e.entry_id))
    if by == "school":
        out_dir = out_dir / "by_school"
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.pdf"):        # drop packets for schools no longer in entries.csv
            old.unlink()
        for school in sorted({e.school for e in ordered}, key=str.lower):
            path = out_dir / f"{_safe(school)}.pdf"
            c = canvas.Canvas(str(path), pagesize=letter)
            c.setTitle(f"{cfg.meet['short']} scoresheets - {school}")
            for e in ordered:
                if e.school == school:
                    ev = cfg.events[e.event]
                    for j in range(1, ev.judges + 1):
                        draw_sheet(c, cfg, ev, j, e)
            c.save()
            written.append(path)
        if blanks:
            path = out_dir / "_BLANK_spares.pdf"
            c = canvas.Canvas(str(path), pagesize=letter)
            c.setTitle(f"{cfg.meet['short']} blank scoresheets")
            for ev in cfg.events.values():
                for _ in range(blanks):
                    for j in range(1, ev.judges + 1):
                        draw_sheet(c, cfg, ev, j, None)
            c.save()
            written.append(path)
        return written

    for eid, ev in cfg.events.items():
        path = out_dir / f"{ev.abbr}_{eid}_sheets.pdf"
        c = canvas.Canvas(str(path), pagesize=letter)
        c.setTitle(f"{cfg.meet['short']} {ev.name} scoresheets")
        for e in (e for e in ordered if e.event == eid):
            for j in range(1, ev.judges + 1):
                draw_sheet(c, cfg, ev, j, e)
        for _ in range(blanks):
            for j in range(1, ev.judges + 1):
                draw_sheet(c, cfg, ev, j, None)
        c.save()
        written.append(path)
    return written


def random_marks(event: Event, judge: int, seed: int = 0) -> dict:
    """Plausible judge marks for practice/accuracy-test sheets."""
    import random
    rnd = random.Random(seed)
    m = {"_judge_name": rnd.choice(["SSgt Ramirez", "GySgt Allen", "Capt Nguyen", "MSgt Brooks"])}
    for f in event.sheet_fields(judge):
        if f.kind == "choice":
            m[f.key] = rnd.choice(f.values[len(f.values) // 3:])
        elif f.kind == "multi":
            p = 0.08 if f.role == "deduct_grid" else 0.18
            m[f.key] = [i for i in range(1, len(f.options) + 1) if rnd.random() < p]
        elif f.kind == "flag":
            m[f.key] = rnd.random() < 0.2
        elif f.kind == "count" and f.digits == 2:
            req = (event.members or {}).get("required", 17)
            m[f.key] = rnd.choice([req, req, req - 1, req + 2])
        elif f.kind == "count":
            m[f.key] = rnd.choice([0, 0, 0, 1, 2])
        elif f.kind == "text":
            m[f.key] = rnd.choice(["7:42", "8:15", "6:51", "8:58"])
    return m
