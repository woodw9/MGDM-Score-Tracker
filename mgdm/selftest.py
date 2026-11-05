"""Practice sheets + accuracy test.

  python run.py practice          make filled-in practice sheets as "phone photos"
                                  in output/practice (drop them in data/inbox to demo)
  python run.py selftest          same, then read each one and compare to the truth
                                  (QR only without an API key; full marks with one)
"""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import numpy as np
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .config import ROOT, Config, Entry
from .reader import api_available, read_sheet
from .sheets_pdf import draw_sheet, paginate, random_marks

OUT = ROOT / "output" / "practice"

# $ per million tokens (input, output) - standard API prices, Nov 2026
PRICES = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.10, 0.50),
          "claude-fable-5-1": (10, 50)}


def phone_photo(png: bytes, seed: int) -> bytes:
    """Make a clean render look like a hand-held phone photo."""
    rnd = random.Random(seed)
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    pad = int(0.08 * w)
    canvas_img = np.full((h + 2 * pad, w + 2 * pad, 3), (70, 85, 95), np.uint8)   # table top
    canvas_img[pad:pad + h, pad:pad + w] = img
    H, W = canvas_img.shape[:2]
    j = lambda: rnd.uniform(-0.035, 0.035) * W  # noqa: E731
    src = np.float32([[pad, pad], [pad + w, pad], [pad + w, pad + h], [pad, pad + h]])
    dst = np.float32([[pad + j(), pad + j()], [pad + w + j(), pad + j()],
                      [pad + w + j(), pad + h + j()], [pad + j(), pad + h + j()]])
    warped = cv2.warpPerspective(canvas_img, cv2.getPerspectiveTransform(src, dst), (W, H),
                                 borderValue=(70, 85, 95))
    # uneven lighting + slight blur + noise
    grad = np.tile(np.linspace(rnd.uniform(0.78, 0.92), 1.0, W, dtype=np.float32), (H, 1))[..., None]
    out = np.clip(warped.astype(np.float32) * grad + np.random.default_rng(seed).normal(0, 4, warped.shape), 0, 255)
    out = cv2.GaussianBlur(out.astype(np.uint8), (3, 3), 0.8)
    ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return jpg.tobytes()


def make_practice(cfg: Config, entries: dict, per_event: int = 1) -> list:
    """-> list of (image path, truth dict)."""
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    seed = 1
    for eid, ev in cfg.events.items():
        mine = sorted([e for e in entries.values() if e.event == eid], key=lambda e: e.entry_id)[:per_event]
        if not mine:
            mine = [Entry("TEST" + ev.abbr, "Practice School", eid)]
        for entry in mine:
            for judge in range(1, ev.judges + 1):
                marks = random_marks(ev, judge, seed)
                buf = io.BytesIO()
                c = canvas.Canvas(buf, pagesize=letter)
                draw_sheet(c, cfg, ev, judge, entry, marks)
                c.save()
                doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
                for pno in range(len(doc)):
                    png = doc[pno].get_pixmap(dpi=200).tobytes("png")
                    name = f"{ev.abbr}_{entry.entry_id}_J{judge}_P{pno + 1}.jpg"
                    path = OUT / name
                    path.write_bytes(phone_photo(png, seed * 10 + pno))
                    truth = {"entry_id": entry.entry_id, "event": eid, "judge": judge, "page": pno + 1,
                             "marks": marks}
                    (OUT / (name + ".truth.json")).write_text(json.dumps(truth))
                    made.append((path, truth))
                seed += 1
    return made


def selftest(cfg: Config, entries: dict, limit: int = 0) -> None:
    from .sheets_pdf import page_fields
    made = make_practice(cfg, entries)
    if limit:
        made = made[:limit]
    print(f"{len(made)} practice photos in {OUT}")
    use_api = api_available()
    if not use_api:
        print("No ANTHROPIC_API_KEY: testing QR identification only.\n")
    tot_f = tot_ok = qr_ok = 0
    tok_in = tok_out = 0
    import time
    t0 = time.time()
    for path, truth in made:
        res = read_sheet(cfg, path.read_bytes())
        if res.get("usage"):
            tok_in += res["usage"]["input"]
            tok_out += res["usage"]["output"]
        ident = res["ident"] or {}
        id_ok = (ident.get("entry_id"), ident.get("judge"), ident.get("page")) == \
                (truth["entry_id"], truth["judge"], truth["page"])
        qr_ok += id_ok
        line = f"{path.name:32s} QR {'OK ' if res['qr'] else 'MISS'} id {'OK' if id_ok else 'WRONG'}"
        if use_api and not res["error"]:
            ev = cfg.events[truth["event"]]
            fields = page_fields(ev, truth["judge"], truth["page"])
            wrong = []
            for f in fields:
                want = truth["marks"].get(f.key)
                got = res["values"].get(f.key)
                if f.kind == "multi":
                    want, got = sorted(want or []), sorted(got or [])
                if f.kind == "flag":
                    want, got = bool(want), bool(got)
                if f.kind == "text":
                    continue
                tot_f += 1
                if want == got:
                    tot_ok += 1
                else:
                    wrong.append(f"{f.key}: sheet={want} read={got}" + (" (flagged)" if f.key in res["uncertain"] else ""))
            line += f"  marks {len(fields) - len(wrong)}/{len(fields)}"
            for w in wrong:
                line += "\n      x " + w
        elif res["error"]:
            line += "  " + res["error"]
        print(line)
    print(f"\nIdentified {qr_ok}/{len(made)} pages.")
    if tot_f:
        print(f"Marks read correctly: {tot_ok}/{tot_f} ({100 * tot_ok / tot_f:.1f}%)")
    if tok_in:
        rs = cfg.settings.get("reader", {})
        model = rs.get("model", "")
        pin, pout = PRICES.get(model, (0, 0))
        cost = tok_in / 1e6 * pin + tok_out / 1e6 * pout
        n = len(made)
        print(f"Model {model} (effort {rs.get('effort')}): {tok_in:,} input + {tok_out:,} output tokens, "
              f"{time.time() - t0:.0f}s total")
        if pin:
            print(f"Cost: ${cost:.2f} for {n} pages = {100 * cost / n:.2f} cents/page "
                  f"(~${cost / n * 650:.2f} for a 650-page meet)")
