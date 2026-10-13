"""OCR for scanned / image-only PDF pages (used by ``kg_ingest.load_pdf``).

Strategy: **read the PDF as text first**. Only pages whose text layer is empty (or garbage)
*and* that are mostly covered by images are OCR'd -- or every page with ``mode="force"``.
Everything runs locally:

* ``TesseractEngine``  PyMuPDF's Tesseract integration (needs the ``tesseract`` program and
                       its language data; ``apt install tesseract-ocr`` / ``brew install tesseract``)
* ``RapidOCREngine``   ONNX models, a pure ``pip install rapidocr-onnxruntime`` (no system package)

``auto`` prefers Tesseract when it is installed, else RapidOCR. The OCR lines of a page are
turned into the same ``Element`` objects the text parsers produce (headings, paragraphs; two
column pages are read column by column; running headers/page numbers are dropped). Tables on
scanned pages come out as paragraphs (no cell structure is recovered).
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("kg_ocr")

MIN_TEXT_CHARS = 40          # fewer characters than this on a page = "no usable text layer"
MIN_IMAGE_COVER = 0.25       # ... and images must cover this share of the page
BAD_TEXT_RATIO = 0.30        # share of replacement/(cid:) garbage that makes a text layer unusable
CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table)\s*\d+[.:]?", re.I)
SECTION_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,5})[.)]?\s+\S")
PAGE_NO_RE = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.I)


@dataclass
class OCRLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: Optional[float] = None       # 0..1 when the engine reports it

    @property
    def h(self) -> float:
        return self.y1 - self.y0


class OCREngine:
    name = "base"

    def lines(self, page: Any, dpi: int, lang: str) -> list[OCRLine]:        # page = pymupdf.Page
        raise NotImplementedError


class TesseractEngine(OCREngine):
    name = "tesseract"

    @staticmethod
    def available() -> bool:
        try:
            import pymupdf
            return bool(pymupdf.get_tessdata())
        except Exception:
            return False

    def lines(self, page, dpi, lang):
        tp = page.get_textpage_ocr(language=lang, dpi=dpi, full=True)
        out = []
        for b in page.get_text("dict", textpage=tp)["blocks"]:
            for ln in b.get("lines", []):
                text = " ".join(s["text"].strip() for s in ln["spans"] if s["text"].strip())
                if text:
                    x0, y0, x1, y1 = ln["bbox"]
                    out.append(OCRLine(text, x0, y0, x1, y1))
        return out


class RapidOCREngine(OCREngine):
    name = "rapidocr"
    _instance: Any = None

    @staticmethod
    def available() -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except ImportError:
            try:
                import rapidocr  # noqa: F401
                return True
            except ImportError:
                return False

    def _ocr(self):
        if RapidOCREngine._instance is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                from rapidocr import RapidOCR
            RapidOCREngine._instance = RapidOCR()
        return RapidOCREngine._instance

    def lines(self, page, dpi, lang):
        png = page.get_pixmap(dpi=dpi).tobytes("png")
        res = self._ocr()(png)
        scale = 72.0 / dpi
        out = []
        if isinstance(res, tuple):                         # rapidocr_onnxruntime: (list, timings)
            items = res[0] or []
            triples = [(b, t, s) for b, t, s in items]
        else:                                              # rapidocr >= 2: object with boxes/txts/scores
            triples = list(zip(res.boxes if res.boxes is not None else [],
                               res.txts or [], res.scores or []))
        for box, text, score in triples:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            if text and text.strip():
                out.append(OCRLine(text.strip(), min(xs) * scale, min(ys) * scale,
                                   max(xs) * scale, max(ys) * scale, float(score)))
        return out


ENGINE_OVERRIDE: Optional[OCREngine] = None       # tests / callers can inject their own engine


def get_engine(name: str = "auto") -> Optional[OCREngine]:
    """The requested OCR engine, or None if none is installed."""
    if ENGINE_OVERRIDE is not None:
        return ENGINE_OVERRIDE
    name = (name or "auto").lower()
    if name in ("auto", "tesseract") and TesseractEngine.available():
        return TesseractEngine()
    if name in ("auto", "rapidocr") and RapidOCREngine.available():
        return RapidOCREngine()
    return None


# --------------------------------------------------------------------------
# Which pages need OCR?
# --------------------------------------------------------------------------


def page_text_state(page: Any) -> tuple[int, float, float]:
    """(characters of text, share of garbage, share of the page covered by images)."""
    text = re.sub(r"\s+", "", page.get_text("text") or "")
    bad = text.count("�") + 5 * text.count("(cid:")
    cover = 0.0
    area = max(page.rect.width * page.rect.height, 1.0)
    try:
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                cover += (r & page.rect).get_area()
    except Exception:
        pass
    return len(text), (bad / len(text) if text else 0.0), min(cover / area, 1.0)


def pages_needing_ocr(doc: Any, mode: str = "auto") -> list[int]:
    """1-based page numbers to OCR. ``off`` -> none, ``force`` -> all, ``auto`` -> pages
    without a usable text layer that are mostly image."""
    if mode == "off":
        return []
    if mode == "force":
        return list(range(1, len(doc) + 1))
    need = []
    for i, page in enumerate(doc, start=1):
        n, bad, cover = page_text_state(page)
        if (n < MIN_TEXT_CHARS and cover >= MIN_IMAGE_COVER) or (n >= MIN_TEXT_CHARS and bad >= BAD_TEXT_RATIO):
            need.append(i)
    return need


# --------------------------------------------------------------------------
# OCR lines -> document elements
# --------------------------------------------------------------------------


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def order_lines(lines: list[OCRLine], width: float) -> list[OCRLine]:
    """Reading order: top to bottom; on two-column pages column by column between full-width lines."""
    lines = sorted(lines, key=lambda l: (round(l.y0 / max(l.h, 1.0)), l.x0))
    if len(lines) < 8:
        return lines
    mid = width / 2
    left = [l for l in lines if l.x1 <= mid * 1.1]
    right = [l for l in lines if l.x0 >= mid * 0.9]
    span = [l for l in lines if l not in left and l not in right]
    if len(left) < 4 or len(right) < 4 or len(span) > 0.25 * len(lines):
        return sorted(lines, key=lambda l: (l.y0, l.x0))
    out: list[OCRLine] = []
    band_l: list[OCRLine] = []
    band_r: list[OCRLine] = []
    for l in sorted(lines, key=lambda l: l.y0):
        if l in span:
            out += sorted(band_l, key=lambda x: x.y0) + sorted(band_r, key=lambda x: x.y0)
            band_l, band_r = [], []
            out.append(l)
        elif l in left:
            band_l.append(l)
        else:
            band_r.append(l)
    return out + sorted(band_l, key=lambda x: x.y0) + sorted(band_r, key=lambda x: x.y0)


def extract_tables(lines: list[OCRLine], h_med: float) -> tuple[list[tuple[list[list[str]], OCRLine]], list[OCRLine]]:
    """Find table-like blocks: >= 3 consecutive rows in which the engine returned several separate
    boxes (cells) per row. Returns ([(rows, bounding box)], the remaining lines). Needs an engine
    that reports boxes per cell (RapidOCR does; Tesseract's whole lines hide the cell gaps)."""
    rows: list[list[OCRLine]] = []
    for l in sorted(lines, key=lambda l: (l.y0 + l.y1) / 2):
        yc = (l.y0 + l.y1) / 2
        if rows and abs(yc - sum((m.y0 + m.y1) / 2 for m in rows[-1]) / len(rows[-1])) < 0.6 * h_med:
            rows[-1].append(l)
        else:
            rows.append([l])
    for r in rows:
        r.sort(key=lambda l: l.x0)
    tabular = [len(r) >= 2 and max(b.x0 - a.x1 for a, b in zip(r, r[1:])) >= 0.7 * h_med for r in rows]

    tables, used = [], set()
    i = 0
    while i < len(rows):
        if not tabular[i]:
            i += 1
            continue
        j = i
        while (j + 1 < len(rows) and tabular[j + 1]
               and min(l.y0 for l in rows[j + 1]) - max(l.y1 for l in rows[j]) < 2.5 * h_med):
            j += 1
        if j - i + 1 >= 3:
            block = rows[i:j + 1]
            anchor = max(block, key=len)                      # the row with the most cells defines the columns
            cols = [(a.x0, a.x1) for a in anchor]
            grid = []
            for r in block:
                cells = [""] * len(cols)
                for b in r:
                    cx = (b.x0 + b.x1) / 2
                    k = min(range(len(cols)), key=lambda c: abs((cols[c][0] + cols[c][1]) / 2 - cx))
                    cells[k] = f"{cells[k]} {_clean(b.text)}".strip()
                grid.append(cells)
            allb = [b for r in block for b in r]
            tables.append((grid, OCRLine("", min(b.x0 for b in allb), min(b.y0 for b in allb),
                                         max(b.x1 for b in allb), max(b.y1 for b in allb))))
            used.update(id(b) for b in allb)
        i = j + 1
    return tables, [l for l in lines if id(l) not in used]


def lines_to_elements(lines: list[OCRLine], page_w: float, page_h: float, pno: int,
                      new_id: Callable[[str], str], element_cls: Any, engine: str) -> list[Any]:
    """Group OCR lines into headings / paragraphs (``element_cls`` is ``kg_ingest.Element``)."""
    # running headers, footers, page numbers
    keep = [l for l in lines if _clean(l.text)
            and not ((l.y1 < 0.06 * page_h or l.y0 > 0.94 * page_h) and (len(l.text) < 60 or PAGE_NO_RE.match(l.text)))]
    if not keep:
        return []
    h_med = statistics.median(l.h for l in keep) or 10.0
    tables, keep = extract_tables(keep, h_med)
    ordered = order_lines(keep, page_w) if keep else []

    groups: list[list[OCRLine]] = []
    for l in ordered:
        prev = groups[-1][-1] if groups else None
        new_para = (prev is None or l.y0 - prev.y1 > 0.7 * h_med or l.y0 < prev.y0 - 0.5 * h_med   # next column
                    or abs(l.h - prev.h) > 0.3 * h_med                                            # size change
                    or l.x0 - prev.x0 > 0.9 * h_med)                  # indented: list item / first line of a paragraph
        if new_para:
            groups.append([l])
        else:
            groups[-1].append(l)

    out = []
    for g in groups:
        text = ""
        for l in g:
            t = _clean(l.text)
            text = (text[:-1] + t if text.endswith("-") and t[:1].islower() and len(text) > 1 and text[-2].isalpha()
                    else f"{text} {t}".strip())
        h = statistics.mean(l.h for l in g)
        confs = [l.conf for l in g if l.conf is not None]
        meta = {"ocr": True, "ocr_engine": engine, "bbox": [round(min(l.x0 for l in g), 1), round(min(l.y0 for l in g), 1),
                                                          round(max(l.x1 for l in g), 1), round(max(l.y1 for l in g), 1)]}
        if confs:
            meta["ocr_conf"] = round(sum(confs) / len(confs), 2)
        numbered = SECTION_NUM_RE.match(text)
        short = len(text) < 100 and not text.endswith((".", ";", ","))
        is_head = len(g) <= 2 and short and not CAPTION_RE.match(text) and (
            h >= 1.25 * h_med or (numbered and h >= 1.05 * h_med) or (text.isupper() and len(text) > 3 and h >= 1.05 * h_med))
        if is_head:
            level = (min(numbered.group(1).count(".") + 1, 6) if numbered
                     else 1 if h >= 1.7 * h_med else 2 if h >= 1.4 * h_med else 3)
            out.append(element_cls(new_id("h"), "heading", text, level=level, page=pno, meta=meta))
        else:
            meta["role"] = "caption" if CAPTION_RE.match(text) else "text"
            out.append(element_cls(new_id("p"), "paragraph", text, page=pno, meta=meta))

    for grid, box in tables:                    # insert in reading order; adopt a "Table N" caption above it
        cap = ""
        for e in list(out):
            if (e.kind == "paragraph" and e.meta.get("role") == "caption" and re.match(r"\s*table", e.text, re.I)
                    and 0 <= box.y0 - e.meta["bbox"][3] < 4 * h_med):
                cap = e.text
                out.remove(e)
        tmeta = {"ocr": True, "ocr_engine": engine, "bbox": [round(box.x0, 1), round(box.y0, 1), round(box.x1, 1), round(box.y1, 1)]}
        el = element_cls(new_id("tbl"), "table", text=cap or " | ".join(grid[0]), page=pno, table=grid,
                         caption=cap, meta=tmeta)
        idx = next((i for i, e in enumerate(out) if e.meta["bbox"][1] > box.y0), len(out))
        out.insert(idx, el)
    return out
