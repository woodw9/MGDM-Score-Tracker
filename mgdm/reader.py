"""Read a photographed scoresheet.

1. Decode the QR code locally (OpenCV) -> which entry / event / judge / page.
2. Send the photo to Claude with a description of exactly which bubbles are on
   that page, and get back structured JSON (validated against a schema).
3. Values are then shown to a tabulator for review before they count.

Without an API key, step 2 is skipped and the review form starts blank.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageOps

from .config import Config, Field, fmt_pts
from .sheets_pdf import page_fields, paginate, parse_sheet_id

log = logging.getLogger(__name__)

try:  # iPhone HEIC photos, if the optional package is installed
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:
    pass


# ------------------------------------------------------------ image + QR
try:                                    # iPhone photos are HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover
    pass


def load_image(data: bytes, max_px: int = 2400) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_px, max_px))
    return img


def decode_qr(img: Image.Image) -> Optional[str]:
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    detectors = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectors.append(cv2.QRCodeDetectorAruco())
    # try full size, the top-right corner (where the code is printed), and smaller sizes
    h, w = arr.shape
    candidates = [arr, arr[: h // 3, w // 2:]]
    for scale in (0.6, 0.4):
        candidates.append(cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA))
    for cand in candidates:
        for d in detectors:
            try:
                txt, _, _ = d.detectAndDecode(cand)
            except cv2.error:
                txt = ""
            if txt:
                return txt
            # thresholded version helps with glare / shadows
            try:
                th = cv2.adaptiveThreshold(cand, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY, 51, 10)
                txt, _, _ = d.detectAndDecode(th)
            except cv2.error:
                txt = ""
            if txt:
                return txt
    return None


def to_jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.standard_b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------ prompt + schema
def describe_field(f: Field) -> str:
    where = f'row "{f.number}. {f.label}"' if f.number else f'"{f.label}"'
    if f.group and f.kind == "choice":
        where += f', column "{f.group}"'
    elif f.group:
        where += f" (category {f.group})"
    if f.kind == "choice":
        how = f"ONE filled bubble; answer its printed value {f.values}, or null if none is filled"
    elif f.kind == "multi" and f.role == "deduct_grid":
        cols = ", ".join(f"{i}={o.label}" for i, o in enumerate(f.options, 1))
        how = f"list the column numbers whose bubble is filled ({cols}); [] if none"
    elif f.kind == "multi":
        crit = ", ".join(f"{i}={o.label} {fmt_pts(o.points)}" for i, o in enumerate(f.options, 1))
        how = f"list the numbers of the criteria whose bubble is filled ({crit}); [] if none"
    elif f.kind == "flag":
        how = "true if the YES bubble is filled, else false"
    elif f.kind == "count" and f.digits == 2:
        how = "two bubble rows (tens row, then ones row); answer the integer (tens*10 + ones), null if blank"
    elif f.kind == "count":
        how = "ONE filled bubble 0-9; answer the integer, null if none filled"
    else:
        how = "transcribe the handwriting exactly; empty string if blank"
    return f"- `{f.key}`: {where}. {how}"


def value_schema(f: Field) -> dict:
    # Kept deliberately loose (no enums): big sheets otherwise exceed the API's
    # schema-size limit. Allowed values are in the prompt and checked in clean_values().
    if f.kind == "choice":
        return {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    if f.kind == "multi":
        return {"type": "array", "items": {"type": "integer"}}
    if f.kind == "flag":
        return {"type": "boolean"}
    if f.kind == "count":
        return {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    return {"type": "string"}


def build_schema(fields: list, read_comments: bool) -> dict:
    # One small generic shape: a list of {key, value} pairs. (A property per mark
    # makes the grammar too large for big sheets.) clean_values() validates each one.
    value = {"anyOf": [{"type": "integer"}, {"type": "boolean"}, {"type": "string"}, {"type": "null"},
                       {"type": "array", "items": {"type": "integer"}}]}
    props = {
        "sheet_id_text": {"type": "string"},
        "judge_name": {"type": "string"},
        "fields": {"type": "array", "items": {
            "type": "object", "properties": {"key": {"type": "string"},
                                             "printed_row": {"type": "string"}, "value": value},
            "required": ["key", "printed_row", "value"], "additionalProperties": False}},
        "uncertain": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    }
    if read_comments:
        props["comments"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM = """You read photographed paper scoresheets from a military drill competition and report \
exactly which bubbles the judge filled in. Accuracy matters more than speed: these numbers decide awards.

Rules:
- A bubble counts as filled if it is clearly darkened/shaded/checked by pen. Printed numbers inside empty bubbles are NOT marks.
- If a bubble is filled AND crossed out with an X, the judge changed their mind: treat it as NOT filled.
- If a row that should have one answer shows two filled bubbles (neither crossed out), pick the darker one and list that key in `uncertain`.
- If a mark is ambiguous, faint, cut off, or blurred, give your best reading and list the key in `uncertain`.
- Rows with repeated names (e.g. several "COLUMN RIGHT") are distinguished by their printed row number.
- `sheet_id_text` is the small code printed under the QR code (e.g. MGDM27|E004|CG|J1|P1of1).
- Use `notes` for anything a tabulator should know (page cut off, wrong sheet, stray marks)."""


def build_prompt(fields: list, event_name: str, judge: int, page: int, pages: int, read_comments: bool) -> str:
    lines = "\n".join(describe_field(f) for f in fields)
    extra = "\nAlso transcribe the COMMENTS box into `comments`." if read_comments else ""
    return (f"This photo should be {event_name}, judge sheet #{judge}, page {page} of {pages}.\n"
            f"Report EVERY field below as one item in `fields`, in this order, using these exact keys. "
            f"In `printed_row`, first copy the row number and label exactly as you see them printed at the "
            f"left of that row in the photo, then read the bubbles on that same printed line - this keeps "
            f"you from drifting onto a neighbouring row:\n{lines}\n{extra}")


# ------------------------------------------------------------ main entry
class ReadResult(dict):
    """keys: qr, ident, values, uncertain, judge_name, notes, comments, error, model"""


def identify(cfg: Config, qr_text: Optional[str]) -> Optional[dict]:
    ident = parse_sheet_id(qr_text) if qr_text else None
    if not ident or ident["code"] != cfg.meet["code"]:
        return None
    ev = cfg.event_by_abbr(ident["abbr"])
    if not ev:
        return None
    ident["event"] = ev.id
    return ident


def api_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def read_sheet(cfg: Config, image_bytes: bytes, hint: Optional[dict] = None,
               model: Optional[str] = None, effort: Optional[str] = None) -> ReadResult:
    """hint: optional {'event','judge','page'} when the QR can't be read.
    model / effort: override settings.yaml for this one read (the re-read dropdown)."""
    rs = dict(cfg.settings.get("reader", {}))
    if model:
        rs["model"] = model
    if effort:
        rs["effort"] = effort
    img = load_image(image_bytes, int(rs.get("max_image_px", 2400)))
    qr = decode_qr(img)
    ident = identify(cfg, qr) or (hint and dict(hint)) or None
    res = ReadResult(qr=qr, ident=ident, values={}, uncertain=[], judge_name="", notes="",
                     comments="", error=None, model=None, usage=None)

    if not api_available():
        res["notes"] = "No API key configured - enter values by hand."
        return res

    import anthropic  # imported lazily so the app runs without it configured
    client = anthropic.Anthropic()
    b64 = to_jpeg_b64(img)
    read_comments = bool(rs.get("read_comments", False))

    if not ident:
        # Ask Claude for just the printed sheet ID so we know what to read.
        try:
            r = client.messages.create(
                model=rs.get("model", "claude-opus-5-5"), max_tokens=2000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": {
                    "type": "object", "properties": {"sheet_id_text": {"type": "string"}},
                    "required": ["sheet_id_text"], "additionalProperties": False}}},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": "Transcribe the small code printed under the QR code at the top "
                                             "right of this scoresheet (looks like MGDM27|E004|CG|J1|P1of1). "
                                             "Empty string if not visible."}]}])
            txt = next((b.text for b in r.content if b.type == "text"), "{}")
            ident = identify(cfg, json.loads(txt).get("sheet_id_text", ""))
            res["ident"] = ident
        except Exception as e:  # noqa: BLE001
            log.warning("sheet id fallback failed: %s", e)
        if not ident:
            res["error"] = "Could not identify the sheet (QR unreadable). Pick the event/judge/page by hand."
            return res

    ev = cfg.events[ident["event"]]
    pages = len(paginate(ev, ident["judge"]))
    if ident["page"] > pages:
        res["error"] = "Page number on sheet doesn't match the current layout - was it printed from an older config?"
        return res
    fields = page_fields(ev, ident["judge"], ident["page"])
    schema = build_schema(fields, read_comments)
    prompt = build_prompt(fields, ev.name, ident["judge"], ident["page"], pages, read_comments)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
               {"type": "text", "text": prompt}]
    model = rs.get("model", "claude-opus-5-5")
    try:
        try:
            kw = {}
            if model in FALLBACK_MODELS:   # server-side refusal fallback (not available on Haiku)
                kw = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
            r = client.beta.messages.create(
                model=model, max_tokens=16000, system=SYSTEM,
                output_config={"effort": rs.get("effort", "medium"),
                               "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content}], **kw)
        except anthropic.BadRequestError as e:
            # Schema too complex or a feature not enabled for this account: retry with plain JSON instructions.
            log.warning("structured request rejected (%s); retrying without schema", e)
            r = client.messages.create(
                model=model, max_tokens=16000, system=SYSTEM,
                output_config={"effort": rs.get("effort", "medium")},
                messages=[{"role": "user", "content": content + [{"type": "text", "text":
                          "Reply with ONLY a JSON object matching this JSON Schema, no other text:\n"
                          + json.dumps(schema)}]}])
        if r.stop_reason == "refusal":
            res["error"] = "The model declined to read this image. Enter values by hand."
            return res
        txt = next((b.text for b in r.content if b.type == "text"), "")
        txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(txt)
    except anthropic.AuthenticationError:
        res["error"] = "API key rejected - check ANTHROPIC_API_KEY."
        return res
    except anthropic.RateLimitError:
        res["error"] = "Rate limited by the API - click Re-read in a minute."
        return res
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        res["error"] = f"API error: {e}"
        return res
    except json.JSONDecodeError:
        res["error"] = "Could not parse the reader's answer - enter values by hand or Re-read."
        return res

    res["model"] = getattr(r, "model", model)
    u = getattr(r, "usage", None)
    res["usage"] = {"input": getattr(u, "input_tokens", 0) or 0, "output": getattr(u, "output_tokens", 0) or 0}
    raw = data.get("fields", {})
    if isinstance(raw, list):   # [{key, value}, ...] -> {key: value}
        raw = {d.get("key"): d.get("value") for d in raw if isinstance(d, dict)}
    res["values"] = clean_values(fields, raw, res)
    res["uncertain"] = [k for k in data.get("uncertain", []) if k in res["values"]] + res["uncertain"]
    res["judge_name"] = data.get("judge_name", "")
    res["notes"] = data.get("notes", "")
    res["comments"] = data.get("comments", "")
    sid = parse_sheet_id(data.get("sheet_id_text", "") or "")
    if qr and sid and sid.get("entry_id") != ident.get("entry_id"):
        res["notes"] = (res["notes"] + " | printed ID differs from QR - check the sheet").strip(" |")
    return res


def clean_values(fields: list, raw: dict, res: dict) -> dict:
    """Coerce/validate the model's answers; anything invalid becomes blank + uncertain."""
    out = {}
    for f in fields:
        v = raw.get(f.key)
        ok = f.key in raw or f.kind == "text"   # a mark the reader skipped gets flagged
        if f.kind == "choice":
            if isinstance(v, bool) or (v is not None and v not in f.values):
                ok, v = False, None
        elif f.kind == "multi":
            items = v if isinstance(v, list) else []
            good = {int(i) for i in items if isinstance(i, (int, float)) and not isinstance(i, bool)
                    and 1 <= int(i) <= len(f.options)}
            ok = ok and isinstance(v, list) and len(good) == len(items)
            v = sorted(good)
        elif f.kind == "flag":
            v = bool(v)
        elif f.kind == "count":
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= f.max_count):
                ok, v = False, None
        else:
            v = "" if v is None else str(v)
        out[f.key] = v
        if not ok:
            res["uncertain"].append(f.key)
    return out
