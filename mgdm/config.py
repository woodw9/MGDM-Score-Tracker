"""Load config/events.yaml + config/settings.yaml into a normalized model.

Every scoreable mark on a sheet becomes a Field with a stable key. The PDF
generator, the photo reader, the review form, and the scoring engine all work
from these same Field objects, so editing the YAML updates everything at once.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field as dfield
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def parse_scale(spec) -> Optional[list]:
    """'1-10' -> [1..10]; '0-3' -> [0,1,2,3]; list stays list; None stays None."""
    if spec is None:
        return None
    if isinstance(spec, list):
        return spec
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(spec))
    if not m:
        raise ValueError(f"Bad scale {spec!r} (use e.g. 1-10)")
    lo, hi = int(m.group(1)), int(m.group(2))
    return list(range(lo, hi + 1))


def fmt_pts(p: float) -> str:
    return (f"{p:.1f}".rstrip("0").rstrip(".")) if p != int(p) else str(int(p))


@dataclass
class Option:
    label: str
    points: float


@dataclass
class Field:
    """One answer on a sheet.

    kind:
      choice -> one value from `values` (bubble row)
      multi  -> list of 1-based option indexes that are marked
                (criteria: marked = MISSED; grid: marked = discrepancy)
      flag   -> True/False
      count  -> integer 0..max_count
      text   -> handwriting (judge name, drill time)
    """
    key: str
    kind: str
    label: str
    section: str
    number: Optional[int] = None          # printed row number
    group: Optional[str] = None           # inspection category / table column
    values: Optional[list] = None         # choice
    options: list = dfield(default_factory=list)  # multi
    max_count: int = 9                    # count
    digits: int = 1                       # count: bubble rows printed
    points: float = 0.0                   # penalty points per unit
    apply_to: str = "once"                # penalty scope
    role: str = "score"                   # score | deduct_grid | penalty | members | info

    @property
    def max_points(self) -> float:
        if self.kind == "choice":
            return float(max(self.values))
        if self.kind == "multi":
            return float(sum(o.points for o in self.options))
        return 0.0


@dataclass
class Section:
    key: str
    title: str
    type: str
    fields: list
    columns: list = dfield(default_factory=list)  # for tables/grids

    @property
    def max_points(self) -> float:
        return sum(f.max_points for f in self.fields if f.role in ("score", "deduct_grid"))


@dataclass
class Event:
    id: str
    name: str
    abbr: str
    judges: int
    combine: str
    sections: dict           # key -> Section
    sheets: dict             # judge # -> [section keys]
    members: Optional[dict] = None
    write_ins: list = dfield(default_factory=list)
    individual: bool = False

    def sheet_sections(self, judge: int) -> list:
        return [self.sections[k] for k in self.sheets[judge]]

    def sheet_fields(self, judge: int) -> list:
        """All fields on judge N's sheet, in printed order."""
        out = []
        if judge == 1 and self.members:
            out.append(Field(key="members", kind="count", label="TOTAL # OF MEMBERS PRESENT",
                             section="_header", max_count=99, digits=2, role="members"))
        if judge == 1:
            for w in self.write_ins:
                out.append(Field(key="write." + slug(w), kind="text", label=w,
                                 section="_header", role="info"))
        for s in self.sheet_sections(judge):
            out.extend(s.fields)
        return out

    def sheet_max(self, judge: int) -> float:
        return sum(s.max_points for s in self.sheet_sections(judge))

    @property
    def max_possible(self) -> float:
        tot = sum(self.sheet_max(j) for j in range(1, self.judges + 1))
        return tot / self.judges if self.combine == "average" else tot


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _build_section(key: str, raw: dict) -> Section:
    t = raw["type"]
    title = raw.get("title", key.upper())
    fields = []
    columns = []
    if t == "ratings":
        for i, (label, scale) in enumerate(raw["items"], 1):
            fields.append(Field(key=f"{key}.{i:02d}", kind="choice", label=label,
                                section=key, number=i, values=parse_scale(scale)))
    elif t == "rating_table":
        columns = list(raw["columns"])
        for r, (label, scales) in enumerate(raw["rows"], 1):
            for c, sc in enumerate(scales, 1):
                if sc is None:
                    continue
                fields.append(Field(key=f"{key}.{r:02d}.{c}", kind="choice", label=label,
                                    section=key, number=r, group=columns[c - 1],
                                    values=parse_scale(sc)))
    elif t == "criteria":
        for i, (label, crit) in enumerate(raw["items"], 1):
            fields.append(Field(key=f"{key}.{i:02d}", kind="multi", label=label,
                                section=key, number=i,
                                options=[Option(str(c[0]), float(c[1])) for c in crit]))
    elif t == "grid":
        columns = [Option(str(c[0]), float(c[1])) for c in raw["columns"]]
        n = 0
        for group, items in raw["rows"]:
            for item in items:
                n += 1
                fields.append(Field(key=f"{key}.{n:02d}", kind="multi", label=item,
                                    section=key, number=n, group=group,
                                    options=columns, role="deduct_grid"))
    elif t == "penalties":
        for i, p in enumerate(raw["items"], 1):
            kind = p.get("kind", "count")
            fields.append(Field(key=f"{key}.{i:02d}", kind="flag" if kind == "flag" else "count",
                                label=p["label"], section=key, number=i,
                                points=float(p.get("points", 5)),
                                apply_to=p.get("apply_to", "once"),
                                max_count=int(p.get("max", 9)), role="penalty"))
    else:
        raise ValueError(f"Unknown section type {t!r} in {key}")
    return Section(key=key, title=title, type=t, fields=fields, columns=columns)


@dataclass
class Config:
    raw: dict
    settings: dict
    events: dict  # id -> Event

    @property
    def meet(self) -> dict:
        return self.raw["meet"]

    @property
    def overall(self) -> dict:
        return self.raw["overall"]

    def event_by_abbr(self, abbr: str) -> Optional[Event]:
        for e in self.events.values():
            if e.abbr.upper() == abbr.upper():
                return e
        return None

    def benchmark(self, event_id: str) -> float:
        b = self.overall["events"].get(event_id, {}).get("benchmark", "auto")
        return self.events[event_id].max_possible if b in (None, "auto") else float(b)

    def event_max(self, event_id: str) -> float:
        """Max shown on score reports: the overall benchmark if one is set, else what the sheets add up to."""
        return self.benchmark(event_id) if event_id in self.overall["events"] else self.events[event_id].max_possible


def load_config(events_path: Path = CONFIG_DIR / "events.yaml",
                settings_path: Path = CONFIG_DIR / "settings.yaml") -> Config:
    raw = yaml.safe_load(events_path.read_text())
    settings = yaml.safe_load(settings_path.read_text()) if settings_path.exists() else {}
    app_s = settings.setdefault("app", {}) or {}
    for key, env in (("passcode", "MGDM_PASSCODE"), ("admin_password", "MGDM_ADMIN_PASSWORD")):
        if os.environ.get(env):                     # config/.env wins (keeps passwords out of git)
            app_s[key] = os.environ[env]
    default_combine = raw.get("judge_combine", "sum")
    events = {}
    for eid, e in raw["events"].items():
        sections = {k: _build_section(k, v) for k, v in e["sections"].items()}
        sheets = {int(j): list(v) for j, v in e["sheets"].items()}
        if sorted(sheets) != list(range(1, int(e["judges"]) + 1)):
            raise ValueError(f"{eid}: 'sheets' must list judges 1..{e['judges']}")
        events[eid] = Event(id=eid, name=e["name"], abbr=e["abbr"], judges=int(e["judges"]),
                            combine=e.get("combine", default_combine), sections=sections,
                            sheets=sheets, members=e.get("members"),
                            write_ins=e.get("write_ins", []) or [],
                            individual=bool(e.get("individual", False)))
    return Config(raw=raw, settings=settings or {}, events=events)


# ---------------------------------------------------------------- entries
@dataclass
class Entry:
    entry_id: str
    school: str
    event: str
    team: str = ""
    participant: str = ""

    @property
    def display(self) -> str:
        bits = [self.school]
        if self.team:
            bits.append(f"Team {self.team}")
        if self.participant:
            bits.append(self.participant)
        return " - ".join(bits)


def load_entries(cfg: Config, path: Path = CONFIG_DIR / "entries.csv") -> dict:
    out = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("entry_id"):
                continue
            ev = row.get("event", "")
            if ev not in cfg.events:
                e = cfg.event_by_abbr(ev)
                if not e:
                    raise ValueError(f"entries.csv: unknown event {ev!r} for {row['entry_id']}")
                ev = e.id
            out[row["entry_id"]] = Entry(row["entry_id"], row.get("school", ""), ev,
                                         row.get("team", ""), row.get("participant", ""))
    return out
