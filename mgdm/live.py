"""Live Scoresheet tabs, styled like 'Live Scoresheet - 2027 MGDM.xlsx'.

Two tabs, rebuilt from the scores every time something is confirmed:
  OVERALL SCORE AND INDIVIDUAL EX   every school's points per event, total, rank
                                    + the Individual Exhibition list
  TOP THREE FOR EACH EVENT          1st/2nd/3rd for every event and overall

A tab is described once as a Grid (values, styles, merges, sizes) and written
by both the Excel writer (openpyxl) and the Google Sheets writer (batchUpdate).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import Config

TAB_OVERALL = "OVERALL SCORE AND INDIVIDUAL EX"
TAB_TOP3 = "TOP THREE FOR EACH EVENT"
LIVE_TABS = (TAB_OVERALL, TAB_TOP3)

NAVY, NAVY2, ALT, LINE, TEXT = "1F3864", "2E4E7E", "F3F5F8", "B9BFC8", "1F1F1F"
MEDAL = {1: "FFE9A8", 2: "E6E6E6", 3: "E8C9A0"}

# name -> fill, font colour, bold, size, horizontal align, border, wrap, italic
STYLES = {
    "banner":   dict(fill="FDEDEC", color="C00000", bold=True, size=12, h="center"),
    "title":    dict(fill=NAVY, color="FFFFFF", bold=True, size=16, h="center"),
    "subtitle": dict(color=NAVY, bold=True, size=12, h="center"),
    "updated":  dict(color="6B7280", size=9, h="center", italic=True),
    "section":  dict(fill=NAVY2, color="FFFFFF", bold=True, size=10, h="center", border=True, wrap=True),
    "head":     dict(fill=NAVY, color="FFFFFF", bold=True, size=10, h="center", border=True, wrap=True),
    "cell":     dict(fill="FFFFFF", color=TEXT, size=10, h="center", border=True),
    "cell_l":   dict(fill="FFFFFF", color=TEXT, size=10, h="left", border=True),
    "cell_b":   dict(fill="FFFFFF", color=TEXT, size=10, h="center", border=True, bold=True),
    "alt":      dict(fill=ALT, color=TEXT, size=10, h="center", border=True),
    "alt_l":    dict(fill=ALT, color=TEXT, size=10, h="left", border=True),
    "alt_b":    dict(fill=ALT, color=TEXT, size=10, h="center", border=True, bold=True),
    **{f"medal{k}": dict(fill=v, color=TEXT, size=10, h="center", border=True, bold=True) for k, v in MEDAL.items()},
}

# Column headings used on the live sheet (falls back to the event name)
SHORT = {
    "platoon_inspection": "PLATOON\nINSPECTION",
    "platoon_basic": "PLATOON\nBASIC",
    "platoon_exhibition": "PLATOON\nEXHIBITION",
    "color_guard": "COLOR GUARD",
    "squad_basic": "SQUAD BASIC",
    "individual_exhibition": "INDIVIDUAL EXHIBITION",
}


def short(cfg: Config, eid: str, one_line: bool = False) -> str:
    s = SHORT.get(eid) or cfg.events[eid].name.upper()
    return s.replace("\n", " ") if one_line else s


@dataclass
class Grid:
    title: str
    nrows: int = 0
    ncols: int = 0
    cells: dict = field(default_factory=dict)       # (row, col) -> value   (1-based)
    style: dict = field(default_factory=dict)       # (row, col) -> style name
    merges: list = field(default_factory=list)      # (r1, c1, r2, c2)
    widths: dict = field(default_factory=dict)      # col -> Excel width
    heights: dict = field(default_factory=dict)     # row -> points
    freeze: tuple = (0, 0)                          # rows, cols frozen

    def put(self, r, c, value=None, st=None):
        if value is not None:
            self.cells[(r, c)] = value
        if st:
            self.style[(r, c)] = st
        self.nrows, self.ncols = max(self.nrows, r), max(self.ncols, c)

    def merge(self, r1, c1, r2, c2, value=None, st=None):
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                self.put(r, c, None, st)
        self.put(r1, c1, value, st)
        if (r1, c1) != (r2, c2):
            self.merges.append((r1, c1, r2, c2))


def _header(g: Grid, cfg: Config, ncols: int, subtitle: str) -> None:
    g.merge(1, 1, 1, ncols, "RESULTS ARE NOT FINAL UNTIL THE AWARDS CEREMONY", "banner")
    g.merge(2, 1, 2, ncols, cfg.meet["name"].upper(), "title")
    g.merge(3, 1, 3, ncols, subtitle, "subtitle")
    g.merge(4, 1, 4, ncols, f"Last updated {datetime.now().strftime('%-I:%M %p')}", "updated")
    g.heights.update({1: 24, 2: 26, 3: 20})


def _fmt(x):
    return "" if x in (None, "") else round(float(x), 2)


def overall_grid(cfg: Config, entries: dict, event_results: dict, overall: list) -> Grid:
    excluded = set(cfg.overall.get("excluded_events") or [])
    counted = [e for e in cfg.overall["events"] if e not in excluded and e in cfg.events]
    indiv = [eid for eid, ev in cfg.events.items() if ev.individual]
    k = len(counted)
    c_total, c_rank = 3 + k, 4 + k
    c_ie = c_rank + 3                       # two empty spacer columns, like the original
    ncols = c_ie + 2 if indiv else c_rank
    g = Grid(TAB_OVERALL)
    _header(g, cfg, ncols, "LIVE SCORING TRACKER")

    g.merge(5, 2, 5, c_rank, "OVERALL SCORE", "section")
    heads = ["#", "REGISTERED SCHOOL"] + [short(cfg, e) for e in counted] + ["TOTAL\n(GRADED EVENTS)", "RANK"]
    for i, h in enumerate(heads, 1):
        g.put(6, i, h, "head")
    if indiv:
        g.merge(5, c_ie, 5, c_ie + 2, short(cfg, indiv[0], True), "section")
        for i, h in enumerate(["#", "SCHOOL AND NAME", "TOTAL SCORE"]):
            g.put(6, c_ie + i, h, "head")
    g.heights[6] = 42

    # schools in registration order (order of entries.csv)
    schools = list(dict.fromkeys(e.school for e in entries.values()
                                 if e.event in cfg.events and not cfg.events[e.event].individual))
    so = {s.school: s for s in overall}
    ie = []
    if indiv:
        ie_res = {r.entry.entry_id: r for r in event_results.get(indiv[0], [])}
        ie = [(e, ie_res.get(e.entry_id)) for e in entries.values() if e.event == indiv[0]]
    rows = max(len(schools), len(ie), 1)
    for i in range(rows):
        r = 7 + i
        a = i % 2 == 1
        base, left, bold = ("alt", "alt_l", "alt_b") if a else ("cell", "cell_l", "cell_b")
        if i < len(schools):
            s = so.get(schools[i])
            scored = bool(s and s.per_event)
            g.put(r, 1, i + 1, base)
            g.put(r, 2, schools[i], left)
            for j, eid in enumerate(counted):
                g.put(r, 3 + j, _fmt(s.per_event[eid][1]) if s and eid in s.per_event else None, base)
            g.put(r, c_total, _fmt(s.total) if scored else None, bold)
            rank = s.rank if scored else None
            g.put(r, c_rank, rank if rank else "-", f"medal{rank}" if rank in MEDAL else bold)
        else:
            for c in range(1, c_rank + 1):
                g.put(r, c, None, left if c == 2 else (bold if c >= c_total else base))
        if indiv:
            if i < len(ie):
                e, res = ie[i]
                name = f"{e.school} - {e.participant}" if e.participant else e.school
                g.put(r, c_ie, i + 1, base)
                g.put(r, c_ie + 1, name, left)
                g.put(r, c_ie + 2, _fmt(res.total) if res and res.complete and not res.dq
                      else ("DQ" if res and res.dq else None), base)
            else:
                for c, st in ((c_ie, base), (c_ie + 1, left), (c_ie + 2, base)):
                    g.put(r, c, None, st)

    g.widths = {1: 4, 2: 32, c_total: 11, c_rank: 7}
    for j in range(k):
        g.widths[3 + j] = 10
    if indiv:
        g.widths.update({c_rank + 1: 4, c_rank + 2: 4, c_ie: 4, c_ie + 1: 34, c_ie + 2: 10})
    g.freeze = (6, 2)
    return g


def _top3_names(results: list, label) -> dict:
    """rank -> 'name' (or 'A / B (TIE)') for ranks 1-3."""
    out = {}
    for rk in (1, 2, 3):
        names = [label(r) for r in results if getattr(r, "rank", None) == rk]
        if names:
            out[rk] = " / ".join(names) + (" (TIE)" if len(names) > 1 else "")
    return out


def top3_grid(cfg: Config, event_results: dict, overall: list) -> Grid:
    g = Grid(TAB_TOP3)
    ncols = 15
    _header(g, cfg, ncols, "TOP 3 BY EVENT")
    excluded = set(cfg.overall.get("excluded_events") or [])
    order = [e for e in cfg.overall["events"] if e in cfg.events and e not in excluded]
    order += [e for e in cfg.events if e not in order and e not in excluded]
    blocks = []
    for eid in order:
        ev = cfg.events[eid]
        res = [r for r in event_results.get(eid, []) if r.complete and not r.dq]
        lab = (lambda r: f"{r.entry.school} - {r.entry.participant}") if ev.individual else (lambda r: r.entry.display)
        blocks.append((short(cfg, eid, True), _top3_names(res, lab)))
    scored = [s for s in overall if s.per_event]
    blocks.append(("OVERALL", _top3_names(scored, lambda s: s.school)))

    slots = (1, 6, 11)                      # columns A, F, K
    for band in range(0, len(blocks), 3):
        chunk = blocks[band:band + 3]
        cols = [slots[1]] if len(chunk) == 1 else list(slots[:len(chunk)])
        top = 5 + 6 * (band // 3)
        for (title, names), c in zip(chunk, cols):
            g.merge(top, c, top, c + 2, title, "head")
            for rk in (1, 2, 3):
                g.put(top + rk, c, rk, f"medal{rk}")
                g.merge(top + rk, c + 1, top + rk, c + 2, names.get(rk, ""), "cell_l")
    g.widths = {1: 4, 2: 24, 3: 6, 6: 4, 7: 24, 8: 6, 11: 4, 12: 24, 13: 6}
    for c in (4, 5, 9, 10, 14, 15):
        g.widths.setdefault(c, 3)
    g.ncols = max(g.ncols, ncols)
    return g


def build_live(cfg: Config, entries: dict, event_results: dict, overall: list) -> list:
    return [overall_grid(cfg, entries, event_results, overall), top3_grid(cfg, event_results, overall)]


# ------------------------------------------------------------ Excel
def write_xlsx(wb, g: Grid, index: int = None) -> None:
    ws = wb.create_sheet(g.title[:31], index)
    side = Side(style="thin", color="FF" + LINE)
    cache = {}
    for (r, c), name in g.style.items():
        st = STYLES[name]
        if name not in cache:
            cache[name] = dict(
                fill=PatternFill("solid", fgColor="FF" + st["fill"]) if st.get("fill") else PatternFill(),
                font=Font(name="Calibri", size=st.get("size", 10), bold=st.get("bold", False),
                          italic=st.get("italic", False), color="FF" + st.get("color", TEXT)),
                alignment=Alignment(horizontal=st.get("h", "center"), vertical="center", wrap_text=st.get("wrap", False)),
                border=Border(left=side, right=side, top=side, bottom=side) if st.get("border") else Border())
        cell = ws.cell(row=r, column=c)
        for k, v in cache[name].items():
            setattr(cell, k, v)
    for (r, c), v in g.cells.items():
        ws.cell(row=r, column=c, value=v)
    for r1, c1, r2, c2 in g.merges:
        ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)
    for c, w in g.widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    for r, h in g.heights.items():
        ws.row_dimensions[r].height = h
    if g.freeze != (0, 0):
        ws.freeze_panes = ws.cell(row=g.freeze[0] + 1, column=g.freeze[1] + 1)
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True


# ------------------------------------------------------------ Google Sheets
def _rgb(hexs: str) -> dict:
    return {"red": int(hexs[0:2], 16) / 255, "green": int(hexs[2:4], 16) / 255, "blue": int(hexs[4:6], 16) / 255}


def _gfmt(name: str) -> dict:
    st = STYLES[name]
    f = {"textFormat": {"fontFamily": "Calibri", "fontSize": st.get("size", 10), "bold": st.get("bold", False),
                        "italic": st.get("italic", False), "foregroundColor": _rgb(st.get("color", TEXT))},
         "horizontalAlignment": st.get("h", "center").upper(), "verticalAlignment": "MIDDLE",
         "wrapStrategy": "WRAP" if st.get("wrap") else "OVERFLOW_CELL"}
    if st.get("fill"):
        f["backgroundColor"] = _rgb(st["fill"])
    return f


def google_requests(g: Grid, sheet_id: int) -> list:
    """batchUpdate requests that give an (already sized) sheet the grid's look."""
    rng = lambda r1, c1, r2, c2: {"sheetId": sheet_id, "startRowIndex": r1 - 1, "endRowIndex": r2,  # noqa: E731
                                  "startColumnIndex": c1 - 1, "endColumnIndex": c2}
    full = {"sheetId": sheet_id}
    reqs = [
        {"unmergeCells": {"range": full}},
        {"repeatCell": {"range": full, "cell": {"userEnteredFormat": {}}, "fields": "userEnteredFormat"}},
        {"updateBorders": {"range": rng(1, 1, g.nrows, g.ncols), **{k: {"style": "NONE"} for k in
                           ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")}}},
        # Google can't freeze part of a merged cell, so only rows are frozen there
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
            "frozenRowCount": g.freeze[0], "frozenColumnCount": 0, "hideGridlines": True}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines"}},
    ]
    for c in range(1, g.ncols + 1):
        px = int(g.widths.get(c, 9) * 7 + 5)
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                     "startIndex": c - 1, "endIndex": c}, "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    for r in range(1, g.nrows + 1):
        px = int(g.heights.get(r, 15) * 4 / 3)
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                     "startIndex": r - 1, "endIndex": r}, "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    # one request per run of same-styled cells along a row
    border = {"style": "SOLID", "color": _rgb(LINE)}
    for r in range(1, g.nrows + 1):
        c = 1
        while c <= g.ncols:
            name = g.style.get((r, c))
            if not name:
                c += 1
                continue
            end = c
            while end + 1 <= g.ncols and g.style.get((r, end + 1)) == name:
                end += 1
            reqs.append({"repeatCell": {"range": rng(r, c, r, end), "cell": {"userEnteredFormat": _gfmt(name)},
                                        "fields": "userEnteredFormat"}})
            if STYLES[name].get("border"):
                reqs.append({"updateBorders": {"range": rng(r, c, r, end), "top": border, "bottom": border,
                                               "left": border, "right": border, "innerVertical": border}})
            c = end + 1
    for r1, c1, r2, c2 in g.merges:
        reqs.append({"mergeCells": {"range": rng(r1, c1, r2, c2), "mergeType": "MERGE_ALL"}})
    return reqs


def google_values(g: Grid) -> list:
    return [["" if g.cells.get((r, c)) is None else g.cells[(r, c)] for c in range(1, g.ncols + 1)]
            for r in range(1, g.nrows + 1)]


def a1(title: str) -> str:
    return "'" + title.replace("'", "''") + "'!A1"
