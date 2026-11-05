"""Scoring engine: judge sheet -> team event score -> event rankings -> overall.

All rules come from config/events.yaml. Nothing here is MGDM-specific beyond
what that file says, so scoring changes are made in the YAML, not here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .config import Config, Event, Entry


# ------------------------------------------------------------ judge sheet
@dataclass
class SheetScore:
    judge: int
    points: float = 0.0                  # scored points on this sheet (before penalties)
    max_points: float = 0.0
    by_section: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)   # keys left blank
    penalties: list = field(default_factory=list)  # (label, amount, apply_to)
    members: Optional[int] = None


def score_sheet(event: Event, judge: int, values: dict) -> SheetScore:
    s = SheetScore(judge=judge, max_points=event.sheet_max(judge))
    for f in event.sheet_fields(judge):
        v = values.get(f.key)
        if f.role == "members":
            s.members = int(v) if v not in (None, "") else None
            if s.members is None:
                s.missing.append(f.key)
            continue
        if f.role == "info":
            continue
        if f.role == "penalty":
            n = (1 if v else 0) if f.kind == "flag" else int(v or 0)
            if n:
                s.penalties.append((f.label, n * f.points, f.apply_to))
            continue
        if f.kind == "choice":
            if v in (None, ""):
                s.missing.append(f.key)
                pts = 0.0
            else:
                pts = float(v)
        elif f.kind == "multi":
            marked = set(int(i) for i in (v or []))
            lost = sum(o.points for i, o in enumerate(f.options, 1) if i in marked)
            pts = f.max_points - lost
        else:
            continue
        s.by_section[f.section] = s.by_section.get(f.section, 0.0) + pts
        s.points += pts
    return s


# ------------------------------------------------------------ team result
@dataclass
class TeamResult:
    entry: Entry
    event: Event
    sheets: dict                       # judge -> SheetScore
    judge_points: dict                 # judge -> points
    combined: float = 0.0
    penalty_total: float = 0.0
    penalty_lines: list = field(default_factory=list)
    total: float = 0.0
    complete: bool = False
    dq: bool = False
    notes: list = field(default_factory=list)
    rank: Optional[int] = None


def score_team(cfg: Config, entry: Entry, sheet_values: dict) -> TeamResult:
    """sheet_values: judge # -> merged values dict for that judge's sheet."""
    ev = cfg.events[entry.event]
    sheets = {j: score_sheet(ev, j, v) for j, v in sheet_values.items()}
    r = TeamResult(entry=entry, event=ev, sheets=sheets,
                   judge_points={j: s.points for j, s in sheets.items()})
    present = sorted(sheets)
    r.complete = present == list(range(1, ev.judges + 1))
    if not r.complete:
        missing = [j for j in range(1, ev.judges + 1) if j not in sheets]
        r.notes.append("waiting on judge sheet(s) " + ", ".join(f"#{j}" for j in missing))
    if present:
        pts = [sheets[j].points for j in present]
        r.combined = sum(pts) / len(pts) if ev.combine == "average" else sum(pts)

    # "all_judges" penalties are taken off every judge sheet; when sheets are
    # summed that means (penalty x number of judges).
    factor = ev.judges if ev.combine == "sum" else 1
    s1 = sheets.get(1)
    if s1:
        for label, amt, scope in s1.penalties:
            mult = factor if scope == "all_judges" else 1
            r.penalty_lines.append((label if mult == 1 else f"{label} (x{mult} sheets)", amt * mult))
        if ev.members:
            m = ev.members
            if s1.members is None:
                r.notes.append("member count not marked")
            else:
                short = max(0, int(m["required"]) - s1.members)
                if short:
                    mult = factor if m.get("apply_to") == "all_judges" else 1
                    r.penalty_lines.append(
                        (f"{short} member(s) short of {m['required']}", short * float(m["points_per_missing"]) * mult))
                if s1.members < int(m["minimum"]):
                    r.dq = True
                    r.notes.append(f"DISQUALIFIED: {s1.members} members (minimum {m['minimum']})")
    for s in sheets.values():
        if s.missing:
            r.notes.append(f"judge #{s.judge}: {len(s.missing)} item(s) left blank (scored 0)")
    r.penalty_total = sum(a for _, a in r.penalty_lines)
    r.total = round(r.combined - r.penalty_total, 2)
    return r


def rank_event(results: list) -> list:
    """Rank complete, non-DQ teams by total (ties share a rank)."""
    ok = sorted([r for r in results if r.complete and not r.dq], key=lambda r: -r.total)
    prev, rank = None, 0
    for i, r in enumerate(ok, 1):
        if r.total != prev:
            rank, prev = i, r.total
        r.rank = rank
    rest = [r for r in results if not (r.complete and not r.dq)]
    return ok + sorted(rest, key=lambda r: r.entry.school)


# ------------------------------------------------------------ overall
@dataclass
class SchoolOverall:
    school: str
    per_event: dict = field(default_factory=dict)   # event_id -> (raw, scaled, entry label)
    total: float = 0.0
    tiebreak: float = 0.0
    rank: Optional[int] = None


def overall_standings(cfg: Config, event_results: dict) -> list:
    ov = cfg.overall
    excluded = set(ov.get("excluded_events") or [])
    counted = [e for e in ov["events"] if e not in excluded and e in cfg.events]
    out = {}
    for eid in counted:
        best = {}
        for r in event_results.get(eid, []):
            if not r.complete or r.dq:
                continue
            cur = best.get(r.entry.school)
            if cur is None or r.total > cur.total:
                best[r.entry.school] = r
        if not ov.get("best_team_only", True):
            raise NotImplementedError("best_team_only: false is not supported yet")
        bench = cfg.benchmark(eid)
        weight = float(ov["events"][eid].get("weight", 100))
        for school, r in best.items():
            so = out.setdefault(school, SchoolOverall(school))
            scaled = (r.total / bench * weight) if ov.get("method", "percent") == "percent" else r.total
            so.per_event[eid] = (r.total, round(scaled, 3), r.entry.display)
    # schools with entries but no completed events still show up
    for eid, res in event_results.items():
        for r in res:
            out.setdefault(r.entry.school, SchoolOverall(r.entry.school))
    tb = ov.get("tiebreak_event")
    for so in out.values():
        so.total = round(sum(v[1] for v in so.per_event.values()), 3)
        so.tiebreak = so.per_event.get(tb, (0, 0, ""))[0]
    ordered = sorted(out.values(), key=lambda s: (-s.total, -s.tiebreak, s.school))
    prev, rank = None, 0
    for i, s in enumerate(ordered, 1):
        key = (s.total, s.tiebreak)
        if key != prev:
            rank, prev = i, key
        s.rank = rank
    return ordered


def compute_all(cfg: Config, entries: dict, sheet_values: dict):
    """sheet_values: (entry_id, judge) -> merged values.  Returns (event_results, overall)."""
    by_entry = defaultdict(dict)
    for (eid, judge), vals in sheet_values.items():
        by_entry[eid][judge] = vals
    event_results = defaultdict(list)
    for entry_id, entry in entries.items():
        event_results[entry.event].append(score_team(cfg, entry, by_entry.get(entry_id, {})))
    for k in list(event_results):
        event_results[k] = rank_event(event_results[k])
    return dict(event_results), overall_standings(cfg, event_results)
