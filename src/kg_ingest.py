"""Ingest a PDF or HTML file and convert it into a Knowledge Graph.

Pipeline (orchestrated with LangGraph)::

    load -> structure -> extract -> build -> END

* load       : parse the file into ordered ``Element`` objects
               (headings, paragraphs, tables, figures) -- PyMuPDF for PDF,
               BeautifulSoup for HTML.
* structure  : assign every element to its enclosing section.
* extract    : pull entities / relations out of text, tables and captions.
               Uses a pluggable ``Extractor``: the default is a dependency-free
               rule-based one; ``LLMExtractor`` accepts any ``prompt -> str``
               callable; ``OllamaExtractor`` uses a local Ollama model; and
               ``HybridExtractor`` combines an LLM with the rules.
* build      : merge duplicate entities (case/punctuation-insensitive) and
               emit a ``networkx.MultiDiGraph``.

Usage::

    from kg_ingest import file_to_knowledge_graph, export_graph
    kg = file_to_knowledge_graph("paper.pdf")
    export_graph(kg, "paper.graphml")        # or .json

Install: ``pip install langgraph networkx pymupdf beautifulsoup4 lxml``
Better PDF parsing (layout analysis; AGPL-3.0 or commercial licence):
``pip install pymupdf-layout pymupdf4llm``
For reading URLs through a headless browser (default; see ``KG_FETCH``):
``pip install playwright && playwright install chromium``
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import os
import re
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import networkx as nx
from bs4 import BeautifulSoup, Tag
from langgraph.graph import END, StateGraph

import kg_layout as _kg_layout
import kg_predicates as _pred
import kg_telemetry as _telemetry
import kg_tablecheck as _tablecheck

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------


@dataclass
class Element:
    """A structural unit of the source document, in reading order."""

    id: str
    kind: str  # heading | paragraph | table | figure
    text: str = ""
    level: int = 0  # heading level (1 = top)
    page: Optional[int] = None
    section_id: Optional[str] = None
    table: Optional[list[list[str]]] = None  # rows; first row is the header
    caption: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Triple:
    subject: str
    predicate: str
    object: str
    subject_type: str = "Entity"
    object_type: str = "Entity"
    strict: bool = True        # rule-based predicate (a text span): must contain a known verb; LLM predicates only pass the quality filter


class KGState(TypedDict, total=False):
    path: str
    elements: list[Element]
    triples: list[tuple[Element, list[Triple]]]  # (source element, triples)
    graph: nx.MultiDiGraph
    assets_dir: str  # where figures/tables were exported (if requested)
    label: str  # display name of the document node (default: file name)


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

_CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table)\s*\d+[.:]?", re.I)


def _eid(prefix: str, n: int) -> str:
    return f"{prefix}{n:04d}"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def is_url(src: str) -> bool:
    return urlparse(src).scheme in ("http", "https")


# Many sites (e.g. nrc.gov, behind a CDN/WAF) answer 403 to non-browser clients,
# so URLs are fetched with a headless browser (Playwright/Chromium) by default.
# $KG_FETCH (or ``set_fetch_mode``) selects the strategy:
#   "browser" (default): headless Chromium; falls back to plain HTTP with a
#                        warning if Playwright/Chromium isn't installed
#   "http"             : plain urllib request with browser-like headers
#   "auto"             : plain HTTP first, headless browser if that fails
# Setup: ``pip install playwright && playwright install chromium``.
FETCH_MODES = ("browser", "http", "auto")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36")


def set_fetch_mode(mode: str) -> None:
    """Choose how URLs are downloaded: "browser", "http" or "auto"."""
    if mode not in FETCH_MODES:
        raise ValueError(f"fetch mode must be one of {FETCH_MODES}")
    os.environ["KG_FETCH"] = mode


def _fetch_http(url: str, timeout: float) -> bytes:
    req = Request(url, headers={
        "User-Agent": os.environ.get("KG_USER_AGENT") or DEFAULT_USER_AGENT,
        "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_browser(url: str, timeout: float) -> bytes:
    """Download ``url`` with headless Chromium. HTML pages come back as the
    rendered DOM (after JavaScript ran); PDFs and other files as raw bytes.

    Set ``$KG_BROWSER_PATH`` to use a specific Chromium/Chrome executable."""
    from playwright.sync_api import Error as PWError  # ImportError -> caller
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    ms = int(timeout * 1000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, executable_path=os.environ.get("KG_BROWSER_PATH") or None,
            args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context(
                user_agent=os.environ.get("KG_USER_AGENT") or DEFAULT_USER_AGENT,
                locale="en-US", accept_downloads=True)
            page = ctx.new_page()
            page.set_default_timeout(ms)
            downloads: list[Any] = []
            page.on("download", lambda d: downloads.append(d))  # PDFs often arrive as downloads
            resp = None
            try:
                resp = page.goto(url, wait_until="load")
            except PWError as exc:
                if "Download is starting" not in str(exc):
                    raise
            if not downloads and resp is not None:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status} {resp.status_text}")
                ctype = (resp.headers.get("content-type") or "").lower()
                if "html" in ctype:
                    try:  # let client-side rendering settle
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except PWTimeout:
                        pass
                    return page.content().encode("utf-8")
                body = resp.body()
                if "pdf" in ctype and not body.startswith(b"%PDF"):
                    # Chromium's inline PDF viewer wraps the file in HTML; re-read
                    # the raw bytes through the browser's own network stack.
                    b64 = page.evaluate(
                        """async (u) => { const r = await fetch(u, {credentials: 'include'});
                           if (!r.ok) throw new Error('HTTP ' + r.status);
                           const b = new Uint8Array(await r.arrayBuffer()); let s = '';
                           for (let i = 0; i < b.length; i += 32768)
                             s += String.fromCharCode(...b.subarray(i, i + 32768));
                           return btoa(s); }""", url)
                    body = base64.b64decode(b64)
                return body
            if not downloads:
                page.wait_for_event("download", timeout=ms)
            return Path(downloads[0].path()).read_bytes()
        finally:
            browser.close()


@lru_cache(maxsize=4)  # the pipeline reads a URL more than once (type sniffing)
def _fetch_url(url: str, mode: str, timeout: float) -> bytes:
    if mode == "http":
        return _fetch_http(url, timeout)
    try:
        return _fetch_browser(url, timeout)
    except ImportError:
        reason = "Playwright is not installed (pip install playwright && playwright install chromium)"
    except Exception as exc:
        reason = f"browser fetch failed: {exc}"
        if "Executable doesn't exist" not in str(exc) and mode == "browser":
            raise RuntimeError(f"Could not download {url}: {reason}") from exc
    log.warning("%s; falling back to plain HTTP for %s", reason, url)
    try:
        return _fetch_http(url, timeout)
    except HTTPError as exc:
        raise RuntimeError(
            f"Could not download {url}: HTTP {exc.code} {exc.reason} ({reason}). "
            "Install Playwright, or download the file in a browser and pass the "
            "local path.") from exc


_PREFETCHED: dict[str, bytes] = {}


def prefetch(url: str, data: bytes) -> None:
    """Register already-downloaded bytes for ``url`` so the pipeline doesn't
    download it again (used by callers that fetch conditionally themselves)."""
    _PREFETCHED[url] = data


def clear_prefetched() -> None:
    _PREFETCHED.clear()


def read_source(src: str, timeout: float = 60.0) -> bytes:
    """Return the raw bytes of a local file path or an http(s) URL."""
    if not is_url(src):
        return Path(src).read_bytes()
    if src in _PREFETCHED:
        return _PREFETCHED[src]
    mode = (os.environ.get("KG_FETCH") or "browser").lower()
    if mode not in FETCH_MODES:
        raise ValueError(f"KG_FETCH must be one of {FETCH_MODES}, got {mode!r}")
    if mode == "auto":
        try:
            return _fetch_url(src, "http", timeout)
        except Exception as exc:
            log.info("plain HTTP fetch failed (%s); retrying with headless browser", exc)
            mode = "browser"
    return _fetch_url(src, mode, timeout)


_UNIT_LIKE = re.compile(r"^[A-Za-zµΩ°/]{1,4}$")
_EXPONENT_BEFORE = re.compile(r"(?:^|[\s(×x*])(?:10|e)\s*$", re.I)


def _is_footnote_marker(prev: str, sup: str) -> bool:
    """A raised, small trailing run is a footnote marker unless it is an exponent (10^-4) or a unit power (m^2)."""
    sup = sup.strip()
    if not sup or len(sup) > 2 or not re.fullmatch(r"[0-9a-z*†‡§]{1,2}", sup):
        return False
    prev = prev.rstrip()
    if _EXPONENT_BEFORE.search(prev):
        return False
    last = re.split(r"[\s(/]", prev)[-1] if prev else ""
    return not (sup.isdigit() and _UNIT_LIKE.match(last) and len(last) <= 3 and last.islower())


def _split_superscripts(page: Any, table: Any, rows: list[list[str]]) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """Take footnote markers out of the cells of a PDF table. The text extractor glues a raised marker to the value ('NDC-2' + 4 -> 'NDC-24',
    'Worker' + 2 -> 'Worker2'), which looks valid and is wrong. Trailing superscript runs (smaller font, raised baseline or the superscript flag)
    are removed from the cell and returned as ``footnotes`` [{row, col, marker}] (row 0 is the header row). Empty rows are dropped as before."""
    try:
        trows = list(table.rows)
        if len(trows) != len(rows):
            raise ValueError("row count differs")
        notes: list[dict[str, Any]] = []
        kept: list[list[str]] = []
        for i, (row, tr) in enumerate(zip(rows, trows)):
            row = list(row)
            cells = getattr(tr, "cells", None) or []
            for j, text in enumerate(row):
                bbox = cells[j] if j < len(cells) else None
                if not text or bbox is None:
                    continue
                spans = [sp for b in page.get_text("dict", clip=bbox)["blocks"] if b["type"] == 0 for ln in b["lines"] for sp in ln["spans"] if sp["text"].strip()]
                if len(spans) < 2:
                    continue
                main = max(spans, key=lambda sp: sp["size"])
                tail = spans[-1]
                raised = tail["origin"][1] < main["origin"][1] - 0.15 * main["size"]
                small = tail["size"] <= 0.8 * main["size"]
                if tail is not main and (bool(tail["flags"] & 1) or (raised and small)):
                    mark = tail["text"].strip()
                    base = text[: len(text) - len(mark)] if text.endswith(mark) else None
                    if base is not None and base.strip() and _is_footnote_marker(base, mark):
                        row[j] = base.strip()
                        notes.append({"row": len(kept), "col": j, "marker": mark})
            if any(row):
                kept.append(row)
            else:
                notes = [x for x in notes if x["row"] != len(kept)]
        return kept, notes
    except Exception:                                    # no cell geometry: keep the extracted text as it is
        return [r for r in rows if any(r)], []


def _load_pdf_basic(path: str) -> list[Element]:
    """Basic PDF parser using plain PyMuPDF (font-size heading heuristics,
    ``find_tables``, embedded images). Fallback for ``load_pdf``."""
    import pymupdf  # lazy: only needed for PDFs

    doc = pymupdf.open(stream=read_source(path), filetype="pdf")
    elements: list[Element] = []
    n = 0

    # Body font size = median span size, used to detect headings.
    sizes = [
        s["size"]
        for page in doc
        for b in page.get_text("dict")["blocks"] if b["type"] == 0
        for l in b["lines"] for s in l["spans"] if s["text"].strip()
    ]
    body = statistics.median(sizes) if sizes else 10.0

    for pno, page in enumerate(doc, start=1):
        page_items: list[tuple[float, Element]] = []  # (y, element)

        # Tables
        table_boxes: list[Any] = []
        try:
            for t in page.find_tables().tables:
                rows = [[_clean(c or "") for c in r] for r in t.extract()]
                rows, notes = _split_superscripts(page, t, rows)
                if len(rows) < 2:
                    continue
                table_boxes.append(pymupdf.Rect(t.bbox))
                n += 1
                page_items.append((tuple(t.bbox), Element(
                    _eid("tbl", n), "table", page=pno, table=rows,
                    text=" | ".join(rows[0]), meta=({"footnotes": notes} if notes else {}))))
        except Exception:  # find_tables unavailable / failed on this page
            pass

        # Text blocks (skip those inside tables)
        captions: list[tuple[tuple, str]] = []              # (bbox, text)
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            if any(pymupdf.Rect(b["bbox"]).intersects(tb) for tb in table_boxes):
                continue
            spans = [s for l in b["lines"] for s in l["spans"] if s["text"].strip()]
            text = _clean(" ".join(s["text"] for s in spans))
            if not spans or not text:
                continue
            size = max(s["size"] for s in spans)
            bold = all(s["flags"] & 16 for s in spans)
            n += 1
            if _CAPTION_RE.match(text):
                captions.append((tuple(b["bbox"]), text))
                continue
            if len(text) < 120 and (size >= body * 1.15 or (bold and size >= body)):
                level = 1 if size >= body * 1.5 else 2 if size >= body * 1.25 else 3
                el = Element(_eid("h", n), "heading", text, level=level, page=pno)
            else:
                el = Element(_eid("p", n), "paragraph", text, page=pno)
            page_items.append((tuple(b["bbox"]), el))

        # Images -> figures, matched to the nearest caption on the page
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                r = rects[0]
                if r.width < 40 or r.height < 40:  # icons / rules
                    continue
                data = doc.extract_image(xref)["image"]
            except Exception:
                continue
            cap = ""
            if captions:
                dist, cap = min(((abs(bb[1] - r.y1), c) for bb, c in captions),
                                key=lambda t: t[0])
            n += 1
            page_items.append(((r.x0, r.y0, r.x1, r.y1), Element(
                _eid("fig", n), "figure", text=cap, caption=cap, page=pno,
                meta={"sha1": hashlib.sha1(data).hexdigest()[:12],
                      "bbox": [round(v, 1) for v in r],
                      "bytes": data})))

        # Captions not claimed by a figure are kept as paragraphs
        claimed = {e.caption for _, e in page_items if e.kind == "figure"}
        for bb, c in captions:
            if c not in claimed:
                n += 1
                page_items.append((bb, Element(_eid("p", n), "paragraph", c, page=pno)))

        # reading order: column by column on multi-column pages (a plain sort by y interleaves the columns)
        elements.extend(e for _, e in _kg_layout.order(page_items, page.rect.width, lambda it: it[0]))
    return elements


# --------------------------------------------------------------------------
# Layout-aware PDF parsing (pymupdf-layout + pymupdf4llm)
# --------------------------------------------------------------------------
# Layout analysis (an ONNX model) classifies every page region as title /
# section-header / text / list-item / table / picture / caption / footnote /
# formula / page-header / page-footer and returns them in reading order (also
# for multi-column pages). On top of that we: drop running headers/footers,
# repair heading levels (outline > section numbering > layout), attach captions
# to the nearest table/figure, merge tables and paragraphs that continue across
# pages, and render figures (raster *and* vector) to PNG.
# Install: ``pip install pymupdf-layout pymupdf4llm`` (AGPL-3.0 or Artifex
# commercial licence). Select with ``set_pdf_engine`` / ``$KG_PDF_ENGINE``.

PDF_ENGINES = ("auto", "layout", "basic")
_SECTION_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,5})[.)]?\s+\S")
_SKIP_CLASSES = {"page-header", "page-footer"}
_TEXT_CLASSES = {"text", "list-item", "footnote", "formula", "code", "other"}


def set_pdf_engine(engine: str) -> None:
    """Choose the PDF parser: "layout", "basic" or "auto" (layout if installed)."""
    if engine not in PDF_ENGINES:
        raise ValueError(f"pdf engine must be one of {PDF_ENGINES}")
    os.environ["KG_PDF_ENGINE"] = engine


def _box_text(box: dict) -> str:
    """Join a layout box's text lines, repairing end-of-line hyphenation."""
    out = ""
    for tl in box.get("textlines") or []:
        line = _clean("".join(sp.get("text", "") for sp in tl.get("spans", [])))
        if not line:
            continue
        if out.endswith("-") and line[:1].islower() and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + line  # "inspec-" + "tion" -> "inspection"
        else:
            out = f"{out} {line}".strip()
    return out


def _toc_levels(toc: list) -> dict[str, int]:
    """Outline entries ``[level, title, page]`` -> {normalised title: level}."""
    return {canonical(t[1]): int(t[0]) for t in toc or [] if len(t) >= 2 and t[1]}


def _heading_level(text: str, box: dict, toc: dict[str, int]) -> int:
    """Outline level if the heading is in the PDF outline; else section
    numbering depth ("2.1.3" -> 3); else the layout model's level."""
    key = canonical(text)
    if key in toc:
        return max(1, min(6, toc[key]))
    m = _SECTION_NUM_RE.match(text)
    if m:
        return max(1, min(6, m.group(1).count(".") + 1))
    return max(1, min(6, int(box.get("header_level") or 1)))


def _render_clip(page: Any, bbox: list[float], dpi: int = 150) -> Optional[bytes]:
    """Render a page region (picture/figure) to PNG bytes."""
    import pymupdf
    try:
        rect = pymupdf.Rect(bbox) & page.rect
        if rect.is_empty or rect.width < 40 or rect.height < 40:
            return None
        return page.get_pixmap(clip=rect, dpi=dpi).tobytes("png")
    except Exception:
        return None


def _nearest_caption(bbox: list[float], cands: list[tuple[int, dict, str]],
                     claimed: set[int], want: str, max_gap: float = 80.0) -> Optional[int]:
    """Index of the closest unclaimed caption candidate above/below ``bbox``.
    ``want`` is "table" or "figure": candidates that explicitly say the other
    kind ("Table 3" for a figure) are ignored."""
    best, best_gap = None, max_gap
    for idx, box, text in cands:
        if idx in claimed:
            continue
        kind = re.match(r"^\s*(figure|fig\.|table)", text, re.I)
        if kind and (kind.group(1).lower() == "table") != (want == "table"):
            continue
        gap = (box["y0"] - bbox[3]) if box["y0"] >= bbox[3] - 2 else (bbox[1] - box["y1"])
        if gap < -2:
            continue  # overlaps the object vertically: not a caption of it
        if min(box["x1"], bbox[2]) - max(box["x0"], bbox[0]) <= 0:
            gap += 25  # different column / not aligned: accept only if clearly closest
        if gap < best_gap:
            best, best_gap = idx, gap
    return best


def load_pdf_layout(path: str) -> list[Element]:
    """Layout-aware PDF parser (see section comment above). Raises ImportError
    if ``pymupdf-layout``/``pymupdf4llm`` aren't installed."""
    import pymupdf
    import pymupdf.layout  # noqa: F401  (activates the layout analyser)
    import pymupdf4llm

    doc = pymupdf.open(stream=read_source(path), filetype="pdf")
    try:                                   # pass 1 is text only; OCR is decided afterwards (see load_pdf)
        from pymupdf4llm.ocr import OCRMode
        data = pymupdf4llm.to_json(doc, use_ocr=OCRMode.NEVER)
    except ImportError:
        data = pymupdf4llm.to_json(doc)
    data = json.loads(data) if isinstance(data, str) else data
    toc = _toc_levels(data.get("toc") or doc.get_toc())

    elements: list[Element] = []
    counter = 0

    def new_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return _eid(prefix, counter)

    for pg in data["pages"]:
        pno, height = pg["page_number"], pg.get("height", 792)
        page = doc[pno - 1]
        boxes = [b for b in pg["boxes"] if b.get("boxclass") not in _SKIP_CLASSES]

        # Caption candidates: layout "caption" boxes and "Figure N"/"Table N" lines.
        cands = []
        for i, b in enumerate(boxes):
            t = _box_text(b)
            if t and len(t) < 400 and (b["boxclass"] == "caption" or _CAPTION_RE.match(t)):
                cands.append((i, b, t))
        claimed: set[int] = set()
        caption_of: dict[int, tuple[int, str]] = {}  # object box idx -> (caption idx, text)
        for i, b in enumerate(boxes):
            if b["boxclass"] in ("table", "picture"):
                bb = [b["x0"], b["y0"], b["x1"], b["y1"]]
                c = _nearest_caption(bb, cands, claimed, "table" if b["boxclass"] == "table" else "figure")
                if c is not None:
                    claimed.add(c)
                    caption_of[i] = (c, next(t for j, _, t in cands if j == c))

        for i, b in enumerate(boxes):
            cls = b["boxclass"]
            bbox = [round(b["x0"], 1), round(b["y0"], 1), round(b["x1"], 1), round(b["y1"], 1)]
            meta = {"bbox": bbox, "boxclass": cls, "page_height": height}
            if i in claimed:
                continue
            if cls == "table" and b.get("table"):
                rows = [[_clean(str(c or "")) for c in r] for r in (b["table"].get("extract") or [])]
                rows = [r for r in rows if any(r)]
                if len(rows) < 2:
                    continue
                cap = caption_of.get(i, (None, ""))[1]
                elements.append(Element(new_id("tbl"), "table", text=cap or " | ".join(rows[0]),
                                        page=pno, table=rows, caption=cap, meta=meta))
            elif cls == "picture":
                png = _render_clip(page, bbox)
                if png is None:
                    continue
                cap = caption_of.get(i, (None, ""))[1]
                meta.update({"sha1": hashlib.sha1(png).hexdigest()[:12], "bytes": png})
                elements.append(Element(new_id("fig"), "figure", text=cap, caption=cap,
                                        page=pno, meta=meta))
            elif cls in ("title", "section-header"):
                text = _box_text(b)
                if text:
                    el = Element(new_id("h"), "heading", text, page=pno, meta=meta,
                                 level=_heading_level(text, b, toc))
                    meta["fontsize"] = b.get("max_fontsize")
                    elements.append(el)
            elif cls in _TEXT_CLASSES or cls == "caption":
                text = _box_text(b)
                if not text:
                    continue
                meta["role"] = {"list-item": "list-item", "footnote": "footnote",
                                "formula": "formula", "code": "code"}.get(cls, "text")
                if cls == "list-item":
                    text = re.sub(r"^[\u2022\u25cf\u25aa\u25e6\u2023*\-\u2013\u2014]\s*", "", text) or text
                prev = elements[-1] if elements else None
                # Paragraph continuing from the previous column/page: the previous
                # one stops without sentence punctuation near the bottom of its
                # column/page and this one starts at the top of the next column
                # (higher on the page) or the next page, or simply starts lower-case.
                if (prev and prev.kind == "paragraph" and prev.meta.get("role") == "text"
                        and meta["role"] == "text"
                        and not re.search(r"[.!?:;\"')\]\u201d]$", prev.text)):
                    last = prev.meta.get("last_page", prev.page)
                    nxt_col = pno == last + 1 or (pno == last and bbox[1] < prev.meta["bbox"][1])
                    at_bottom = prev.meta["bbox"][3] > 0.8 * height
                    if nxt_col and (text[:1].islower() or at_bottom):
                        prev.text = f"{prev.text} {text}"
                        prev.meta.update(last_page=pno, bbox=bbox)
                        continue
                elements.append(Element(new_id("p"), "paragraph", text, page=pno, meta=meta))

    # Document title: a unique, strictly largest top-of-document heading sits
    # above the numbered sections (level 0) instead of competing with them.
    heads = [e for e in elements if e.kind == "heading"]
    if len(heads) > 1 and heads[0].page == 1 and not _SECTION_NUM_RE.match(heads[0].text):
        sizes = [h.meta.get("fontsize") or 0 for h in heads]
        rest_numbered = sum(bool(_SECTION_NUM_RE.match(h.text)) for h in heads[1:])
        if sizes[0] > max(sizes[1:]) or rest_numbered >= max(1, (len(heads) - 1) // 2):
            heads[0].level = 0

    # Tables that continue on the next page: same column count, directly follow.
    merged: list[Element] = []
    for e in elements:
        prev = merged[-1] if merged else None
        if (prev and e.kind == "table" and prev.kind == "table"
                and e.page == prev.meta.get("last_page", prev.page) + 1
                and len(e.table[0]) == len(prev.table[0]) and not e.caption
                and e.meta["bbox"][1] < 0.2 * e.meta["page_height"]
                and prev.meta["bbox"][3] > 0.75 * prev.meta["page_height"]):
            rows = e.table[_tablecheck.header_prefix_len(prev.table, e.table):]      # drop the repeated header block (1-3 rows, OCR noise tolerated)
            prev.table = prev.table + rows
            prev.meta.update(last_page=e.page, bbox=e.meta["bbox"])
            continue
        merged.append(e)
    return merged


def set_ocr(mode: Optional[str] = None, engine: Optional[str] = None, lang: Optional[str] = None,
            dpi: Optional[int] = None) -> None:
    """Configure OCR for PDFs: ``mode`` auto (default: only pages without usable text) | off | force;
    ``engine`` auto | tesseract | rapidocr; ``lang`` Tesseract language code(s), e.g. "eng+deu"; ``dpi``."""
    if mode is not None:
        if mode not in ("auto", "off", "force"):
            raise ValueError("ocr mode must be auto, off or force")
        os.environ["KG_OCR"] = mode
    for var, val in (("KG_OCR_ENGINE", engine), ("KG_OCR_LANG", lang), ("KG_OCR_DPI", dpi)):
        if val is not None:
            os.environ[var] = str(val)


def _renumber(elements: list[Element]) -> None:
    n = 0
    for e in elements:
        n += 1
        e.id = _eid({"heading": "h", "paragraph": "p", "table": "tbl", "figure": "fig"}[e.kind], n)


def _with_ocr(path: str, elements: list[Element]) -> list[Element]:
    """Second pass: OCR the pages that the text parser could not read (no usable text layer)."""
    mode = (os.environ.get("KG_OCR") or "auto").lower()
    if mode == "off":
        return elements
    import pymupdf
    import kg_ocr
    doc = pymupdf.open(stream=read_source(path), filetype="pdf")
    pages = kg_ocr.pages_needing_ocr(doc, mode)
    if not pages:
        return elements
    engine = kg_ocr.get_engine(os.environ.get("KG_OCR_ENGINE") or "auto")
    if engine is None:
        log.warning("page(s) %s of %s have no text layer and need OCR, but no OCR engine is installed "
                    "(pip install rapidocr-onnxruntime, or install Tesseract); they will be empty", pages, path)
        return elements
    dpi, lang = int(os.environ.get("KG_OCR_DPI") or 200), os.environ.get("KG_OCR_LANG") or "eng"
    by_page: dict[int, list[Element]] = {}
    for e in elements:
        by_page.setdefault(e.page or 0, []).append(e)
    done = []
    for pno in pages:
        page = doc[pno - 1]
        try:
            lines = engine.lines(page, dpi, lang)
        except Exception as exc:
            log.warning("OCR failed on page %d of %s: %s", pno, path, exc)
            continue
        by_page[pno] = kg_ocr.lines_to_elements(lines, page.rect.width, page.rect.height, pno,
                                                lambda prefix: "tmp", Element, engine.name)
        done.append(pno)
    log.info("OCR (%s, %d dpi) used on page(s) %s of %s", engine.name, dpi, done, path)
    merged = [e for pno in sorted(by_page) for e in by_page[pno]]
    _renumber(merged)
    return merged


def load_pdf(path: str) -> list[Element]:
    """Parse a PDF (local path or http(s) URL) into elements.

    1. **Text first**: the layout-aware parser (``pymupdf-layout``) when installed, else the basic
       PyMuPDF parser (also if the layout parser fails). Force one with ``set_pdf_engine``.
    2. **Then OCR**, only for pages without a usable text layer (see ``set_ocr`` / ``kg_ocr``)."""
    engine = (os.environ.get("KG_PDF_ENGINE") or "auto").lower()
    if engine not in PDF_ENGINES:
        raise ValueError(f"KG_PDF_ENGINE must be one of {PDF_ENGINES}, got {engine!r}")
    elements = None
    if engine != "basic":
        try:
            elements = load_pdf_layout(path)
        except ImportError as exc:
            if engine == "layout":
                raise
            log.warning("pymupdf-layout unavailable (%s); using the basic PDF parser. "
                        "pip install pymupdf-layout pymupdf4llm for better results", exc)
        except Exception as exc:
            if engine == "layout":
                raise
            log.warning("layout PDF parsing failed (%s); using the basic parser", exc)
    if elements is None:
        elements = _load_pdf_basic(path)
    return _with_ocr(path, elements)


def _resolve_src(base: str, src: str) -> str:
    """Absolute http(s) URL, ``data:`` URI or local path of an ``<img src>``."""
    src = (src or "").strip()
    if not src or src.startswith("data:") or is_url(src):
        return src
    if is_url(base):
        return urljoin(base, src)
    return str((Path(base).parent / unquote(src.split("#")[0].split("?")[0])).resolve())


def load_ecfr_xml(path: str) -> list[Element]:
    """Parse eCFR XML (``/api/versioner/v1/full/<date>/title-N.xml``, as served
    for a part, subpart, section or appendix) into elements, deterministically:
    each ``DIVn`` becomes a heading (level = nesting depth) and its ``P``/``FP``
    paragraphs and ``GPOTABLE`` tables become paragraphs/tables beneath it."""
    soup = BeautifulSoup(read_source(path), "xml")
    elements: list[Element] = []
    n = 0

    def table_of(tbl: Tag) -> list[list[str]]:
        rows = []
        head = [_clean(c.get_text(" ")) for c in tbl.select("BOXHD CHED, TTITLE")]
        if head:
            rows.append(head)
        for r in tbl.find_all("ROW"):
            cells = [_clean(c.get_text(" ")) for c in r.find_all(["ENT", "TH", "TD"])]
            if any(cells):
                rows.append(cells)
        return rows

    def walk(node: Tag, depth: int) -> None:
        nonlocal n
        for ch in node.children:
            if not isinstance(ch, Tag):
                continue
            name = ch.name
            if re.fullmatch(r"DIV\d", name):
                head = ch.find("HEAD", recursive=False)
                title = _clean(head.get_text(" ")) if head else ""
                if title:
                    n += 1
                    elements.append(Element(_eid("h", n), "heading", title, level=min(depth + 1, 6),
                                            meta={"ecfr_type": ch.get("TYPE", ""), "ecfr_n": ch.get("N", "")}))
                walk(ch, depth + 1)
            elif name in ("P", "FP", "NOTE", "EXTRACT", "EDNOTE", "AUTH", "SOURCE"):
                t = _clean(ch.get_text(" "))
                if t:
                    n += 1
                    elements.append(Element(_eid("p", n), "paragraph", t,
                                            meta={"role": "text" if name in ("P", "FP") else name.lower()}))
            elif name in ("GPOTABLE", "TABLE"):
                rows = table_of(ch)
                if len(rows) >= 2:
                    n += 1
                    t = ch.find("TTITLE")
                    cap = _clean(t.get_text(" ")) if t else ""
                    elements.append(Element(_eid("tbl", n), "table", text=cap or " | ".join(rows[0]),
                                            table=rows, caption=cap))
            elif name not in ("HEAD", "CITA", "SECAUTH"):
                walk(ch, depth)

    walk(soup, 0)
    return elements


def load_html(path: str) -> list[Element]:
    """Parse an HTML file (local path or http(s) URL) into elements using
    BeautifulSoup."""
    raw = read_source(path)
    soup = BeautifulSoup(raw, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    root = soup.body or soup
    elements: list[Element] = []
    n = 0

    title = soup.title.get_text(strip=True) if soup.title else ""
    if title:
        elements.append(Element("h0000", "heading", title, level=1))

    def table_rows(tbl: Tag) -> list[list[str]]:
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        return rows

    def walk(node: Tag) -> None:
        nonlocal n
        for ch in node.children:
            if not isinstance(ch, Tag):
                continue
            name = ch.name.lower()
            if re.fullmatch(r"h[1-6]", name):
                n += 1
                elements.append(Element(_eid("h", n), "heading",
                                        _clean(ch.get_text(" ")), level=int(name[1])))
            elif name == "table":
                rows = table_rows(ch)
                if len(rows) >= 2:
                    n += 1
                    cap = ch.find("caption")
                    cap_t = _clean(cap.get_text(" ")) if cap else ""
                    elements.append(Element(_eid("tbl", n), "table", text=cap_t or " | ".join(rows[0]),
                                            table=rows, caption=cap_t))
            elif name == "figure" or (name == "img" and not ch.find_parent("figure")):
                img = ch if name == "img" else ch.find("img")
                fc = ch.find("figcaption") if name == "figure" else None
                cap = _clean(fc.get_text(" ")) if fc else ""
                alt = _clean(img.get("alt", "")) if img else ""
                if cap or alt or img:
                    n += 1
                    elements.append(Element(_eid("fig", n), "figure",
                                            text=cap or alt, caption=cap or alt,
                                            meta={"src": img.get("src", "") if img else "",
                                                  "src_abs": _resolve_src(path, img.get("src", "")) if img else "",
                                                  "alt": alt}))
            elif name in ("p", "li", "blockquote", "pre", "dd", "dt"):
                t = _clean(ch.get_text(" "))
                if t:
                    n += 1
                    elements.append(Element(_eid("p", n), "paragraph", t))
            else:
                walk(ch)

    walk(root)
    return elements


# --------------------------------------------------------------------------
# Asset export: figures -> image files, tables -> CSV
# --------------------------------------------------------------------------

DEFAULT_ASSETS_DIR = "kg_assets"  # override via out_dir= or $KG_ASSETS_DIR
_IMG_MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8", ".jpg"), (b"GIF8", ".gif"),
              (b"BM", ".bmp"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"),
              (b"\x00\x00\x00\x0cjP  ", ".jp2"), (b"\xff\x4f\xff\x51", ".j2k"),   # JPEG 2000 (file / codestream)
              (b"\x00\x00\x01\x00", ".ico"), (b"\x97JB2\r\n\x1a\n", ".jb2"))


def _slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-")


def _image_ext(data: bytes) -> str:
    """File extension for image bytes by content ('' when the bytes are not an image we can identify:
    e.g. an HTML error page or compressed bytes returned instead of the picture)."""
    for magic, ext in _IMG_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"):
        return ".heic"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()          # tolerate a BOM and leading whitespace
    if head.startswith((b"<!doctype html", b"<html")):
        return ""
    if b"<svg" in head and (head.startswith((b"<svg", b"<?xml", b"<!--", b"<!doctype svg")) or b"<!doctype svg" in head):
        return ".svg"
    try:                                          # anything else Pillow can identify (e.g. PPM, PCX, ICNS ...)
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return "." + (im.format or "").lower().replace("jpeg", "jpg") if im.format else ""
    except Exception:
        return ""


def _figure_bytes(e: Element) -> Optional[bytes]:
    """Image bytes of a figure: rendered at parse time (PDF) or fetched from
    its ``src`` (HTML: data URI, local file or URL)."""
    if e.meta.get("bytes"):
        return e.meta["bytes"]
    src = e.meta.get("src_abs") or ""
    try:
        if src.startswith("data:"):
            head, _, payload = src.partition(",")
            return base64.b64decode(payload) if ";base64" in head else unquote(payload).encode()
        if is_url(src):
            return _fetch_http(src, 30.0)  # plain HTTP: avoids a browser launch per image
        if src:
            return Path(src).read_bytes()
    except Exception as exc:
        log.warning("could not read image %s (%s)", src[:100], exc)
    return None


def _to_png(data: bytes) -> Optional[bytes]:
    """Convert an image to PNG (None if no converter can read it): PyMuPDF first, then Pillow (ICO, WebP, AVIF
    with plugin, ...)."""
    try:
        import pymupdf
        pix = pymupdf.Pixmap(data)
        if pix.colorspace is not None and pix.colorspace.n > 3:  # CMYK -> RGB
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return pix.tobytes("png")
    except Exception:
        pass
    try:
        import io

        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            out = io.BytesIO()
            im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB").save(out, "PNG")
            return out.getvalue()
    except Exception:
        return None


def export_assets(elements: list[Element], source: str, out_dir: Optional[str] = None,
                  image_format: str = "png") -> Path:
    """Write every figure and table of a parsed document to standalone files.

    Layout: ``<out_dir>/<sha256(source)>/figures/<id>_p<page>_<caption>.png``,
    ``.../tables/<id>_p<page>_<caption>.csv`` and ``manifest.json`` (id, kind,
    page, caption, file, size). Raster figures are converted to PNG when
    ``image_format="png"`` (SVG is kept as is; ``"native"`` keeps the original
    bytes; a format that cannot be converted keeps its own extension, never ``.bin``). Content that is not an image
    (an HTML error page returned for the ``src``, ...) is not written and is listed under ``skipped`` in the manifest. ``out_dir`` defaults to ``$KG_ASSETS_DIR`` or ``./kg_assets``.
    Sets ``meta["file"]`` on each exported element. Returns the document folder."""
    root = Path(out_dir or os.environ.get("KG_ASSETS_DIR") or DEFAULT_ASSETS_DIR) / source_hash(source)
    manifest: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for e in elements:
        if e.kind not in ("table", "figure"):
            continue
        if e.kind == "table" and e.meta.get("front_matter"):        # table of contents / acronym list: in the graph, but not a data table
            skipped.append({"id": e.id, "page": e.page, "caption": e.caption, "reason": f"front matter ({e.meta['front_matter']})"})
            continue
        stem = "_".join(x for x in (e.id, f"p{e.page}" if e.page else "", _slug(e.caption or "")) if x)
        if e.kind == "table":
            sub, name = "tables", f"{stem}.csv"
            payload, info = None, {"rows": len(e.table), "cols": len(e.table[0]), **({"footnotes": e.meta["footnotes"]} if e.meta.get("footnotes") else {})}
        else:
            data = _figure_bytes(e)
            if not data:
                continue
            ext = _image_ext(data)
            if not ext:                              # not an image (error page, compressed bytes, ...): do not save it
                log.warning("figure %s: %s is not an image (starts %r); not exported", e.id, e.meta.get("src_abs", "")[:80], data[:16])
                skipped.append({"id": e.id, "page": e.page, "caption": e.caption, "reason": "content is not an image"})
                continue
            if image_format == "png" and ext not in (".png", ".svg"):
                png = _to_png(data)
                if png:
                    data, ext = png, ".png"
            sub, name, payload, info = "figures", f"{stem}{ext}", data, {"bytes": len(data)}
        (root / sub).mkdir(parents=True, exist_ok=True)
        path = root / sub / name
        if e.kind == "table":
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(e.table)
        else:
            path.write_bytes(payload)
        e.meta["file"] = f"{sub}/{name}"
        manifest.append({"id": e.id, "kind": e.kind, "page": e.page, "caption": e.caption,
                         "file": e.meta["file"], **info})
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(
        {"source": source, "assets": manifest, **({"skipped": skipped} if skipped else {})}, indent=2, ensure_ascii=False))
    return root


def export_tables_from_graph(g: nx.MultiDiGraph, out_dir: str) -> list[Path]:
    """Write the tables of an already-built (e.g. saved and reloaded) graph to
    CSV. Figures can't be recovered from a saved graph; use ``assets_dir`` when
    building."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, d in g.nodes(data=True):
        if d.get("type") == "Table" and d.get("rows"):
            rows = json.loads(d["rows"]) if isinstance(d["rows"], str) else d["rows"]
            page = d.get("page")
            path = out / "_".join(x for x in (str(n), f"p{page}" if page else "",
                                              _slug(str(d.get("label", "")))) if x)
            path = path.with_suffix(".csv")
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows)
            paths.append(path)
    return paths


# --------------------------------------------------------------------------
# Extractors (text -> triples)
# --------------------------------------------------------------------------


class Extractor(ABC):
    @abstractmethod
    def extract_text(self, text: str) -> list[Triple]:
        """Return (subject, predicate, object) triples found in ``text``."""

    def extract_table(self, rows: list[list[str]]) -> list[Triple]:
        """Row key (first column) --[column header]--> cell value."""
        header, *body = rows
        out = []
        for row in body:
            if not row or not row[0]:
                continue
            for h, v in zip(header[1:], row[1:]):
                if h and v:
                    out.append(Triple(row[0], _clean(h), v, "TableRow", "Value"))
        return out


_ENT_RE = re.compile(
    r"\b(?:[A-Z][\w&\-]*(?:\s+(?:of|and|for|the|de|von)\s+)?(?:\s*[A-Z][\w&\-]*)*(?:\s+\d+(?:\.\d+)?)?)\b")
_STOP = {"The", "This", "That", "These", "Those", "A", "An", "In", "On", "It", "We",
        "He", "She", "They", "However", "Figure", "Fig", "Table", "Section", "And",
        "But", "For", "As", "At", "By", "If", "Our", "Its", "Their", "There"}


class RuleBasedExtractor(Extractor):
    """Dependency-free baseline: capitalised phrases as entities; the short
    text between two adjacent entities in a sentence becomes the predicate."""

    def entities(self, sentence: str) -> list[tuple[str, int, int]]:
        found = []
        from kg_tools import identifier_spans
        idspans = identifier_spans(sentence)                # standards / guides / CFR references are Identifier nodes, not entities
        for m in _ENT_RE.finditer(sentence):
            if any(m.start() < b and m.end() > a for a, b in idspans):
                continue
            words = m.group().split()
            while words and words[0] in _STOP:
                words.pop(0)
            if not words:
                continue
            name = " ".join(words)
            if len(name) < 2:
                continue
            start = m.start() + m.group().index(words[0])
            found.append((name, start, start + len(name)))
        return found

    def extract_text(self, text: str) -> list[Triple]:
        triples: list[Triple] = []
        for sent in re.split(r"(?<=[.!?])\s+", text):
            ents = self.entities(sent)
            for (a, _, ae), (b, bs, _) in zip(ents, ents[1:]):
                if a == b:
                    continue
                mid = _clean(sent[ae:bs]).strip(" ,;:()")
                pred = mid if 0 < len(mid.split()) <= 5 else "related_to"
                triples.append(Triple(a, pred, b))
        return triples


EXTRACTION_PROMPT = (
    "Extract knowledge-graph triples from the text below. Use short, canonical "
    "entity names (people, organisations, products, places, concepts, "
    "quantities) and concise verb-phrase predicates (e.g. \"developed\", "
    "\"located in\"). Only state facts the text supports; resolve pronouns to "
    "the entity they refer to. Respond with ONLY JSON of the form "
    '{{"triples": [{{"subject": str, "predicate": str, "object": str}}]}}.'
    "\n\nText:\n{text}")

TRIPLES_SCHEMA = {  # JSON schema for Ollama structured outputs
    "type": "object",
    "properties": {"triples": {"type": "array", "items": {
        "type": "object",
        "properties": {"subject": {"type": "string"},
                       "predicate": {"type": "string"},
                       "object": {"type": "string"}},
        "required": ["subject", "predicate", "object"]}}},
    "required": ["triples"],
}


_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}")


def entity_grounded(entity: str, text: str) -> bool:
    """Is an entity named by an LLM backed by the source text? True when the name occurs in it, or when most of its significant words do (a model may
    shorten or expand a name, but it should not invent one). A heuristic for the hallucination rate, not a proof either way."""
    low = text.lower()
    name = " ".join(entity.lower().split())
    if not name or name in low:
        return True
    words = [w.lower() for w in _WORD.findall(entity)]
    return bool(words) and sum(w in low for w in words) / len(words) >= 0.6


def _check_grounding(triples: list["Triple"], text: str) -> None:
    """Count the subjects and objects of LLM-made triples that the text does not support (``cogent_llm_outputs_total{kind="triple"}``)."""
    try:
        for t in triples:
            if not t.strict:                                   # produced by a language model, not by the rule-based extractor
                for ent in (t.subject, t.object):
                    _telemetry.record_output("triple", not entity_grounded(ent, text))
    except Exception:                                          # noqa: BLE001 - telemetry never breaks an ingest
        pass


def parse_triples(reply: str) -> list[Triple]:
    """Parse an LLM reply: ``{"triples": [...]}`` or a bare JSON list
    (possibly wrapped in prose or code fences). Raises ValueError if unparseable."""
    m = re.search(r"[\[{].*[\]}]", reply, re.S)
    if not m:
        raise ValueError("no JSON found in LLM reply")
    data = json.loads(m.group())
    if isinstance(data, dict):
        data = data.get("triples", [])
    if not isinstance(data, list):
        raise ValueError("unexpected JSON shape")
    out = []
    for d in data:
        if isinstance(d, dict) and d.get("subject") and d.get("object"):
            out.append(Triple(_clean(str(d["subject"])),
                              _clean(str(d.get("predicate") or "related_to")),
                              _clean(str(d["object"])), strict=False))
    return out


class LLMExtractor(Extractor):
    """Wrap any ``prompt -> str`` callable (e.g. a LangChain chat model's
    ``lambda p: llm.invoke(p).content``). The model must answer with JSON
    triples (see ``EXTRACTION_PROMPT``). On any failure it falls back to
    ``fallback`` (rule-based by default) for that text."""

    PROMPT = EXTRACTION_PROMPT

    def __init__(self, llm: Callable[[str], str], fallback: Optional[Extractor] = None):
        self.llm, self.fallback = llm, fallback or RuleBasedExtractor()

    def extract_text(self, text: str) -> list[Triple]:
        try:
            return parse_triples(self.llm(self.PROMPT.format(text=text)))
        except Exception as exc:  # malformed output / backend error -> degrade
            log.warning("LLM extraction failed (%s); using fallback", exc)
            return self.fallback.extract_text(text)


def _chunks(text: str, max_chars: int) -> list[str]:
    """Split text on sentence boundaries into pieces of <= max_chars."""
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if cur and len(cur) + len(sent) + 1 > max_chars:
            out.append(cur)
            cur = ""
        cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


class OllamaExtractor(LLMExtractor):
    """Extract triples with a local `Ollama <https://ollama.com>`_ model.

    Needs a running server (``ollama serve``) and a pulled model
    (``ollama pull llama3.1``); no extra Python dependency. Uses Ollama's
    structured outputs (JSON schema) via ``/api/chat`` at temperature 0.

    Host defaults to ``$OLLAMA_HOST`` or ``http://localhost:11434``; model to
    ``$OLLAMA_MODEL`` or ``llama3.1``. Long texts are split into chunks of at
    most ``max_chars``. If the server is unreachable the extractor logs once and
    uses ``fallback`` (rule-based) for the rest of the run; a bad reply falls
    back for that chunk only.
    """

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None,
                 timeout: float = 120.0, max_chars: int = 3000,
                 temperature: float = 0.0, fallback: Optional[Extractor] = None):
        super().__init__(self._chat, fallback)
        self.model = model or os.environ.get("OLLAMA_MODEL") or "llama3.1"
        host = host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        if "://" not in host:
            host = "http://" + host
        self.host = host.rstrip("/")
        self.timeout, self.max_chars, self.temperature = timeout, max_chars, temperature
        self._down = False

    def _chat(self, prompt: str) -> str:
        body = json.dumps({
            "model": self.model, "stream": False, "format": TRIPLES_SCHEMA,
            "options": {"temperature": self.temperature},
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = Request(self.host + "/api/chat", data=body,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())["message"]["content"]

    def extract_text(self, text: str) -> list[Triple]:
        out: list[Triple] = []
        for chunk in _chunks(text, self.max_chars):
            if self._down:
                out += self.fallback.extract_text(chunk)
                continue
            try:
                out += parse_triples(self._chat(self.PROMPT.format(text=chunk)))
            except (URLError, ConnectionError, TimeoutError, OSError) as exc:
                log.warning("Ollama unreachable at %s (%s); using fallback "
                            "extractor for the rest of this run", self.host, exc)
                self._down = True
                out += self.fallback.extract_text(chunk)
            except Exception as exc:  # bad JSON / unexpected payload
                log.warning("Ollama reply unusable (%s); using fallback for this chunk", exc)
                out += self.fallback.extract_text(chunk)
        return out


class HybridExtractor(Extractor):
    """Run ``primary`` (e.g. an LLM) and ``secondary`` (e.g. rule-based) and
    merge: all primary triples are kept; a secondary triple is added only if
    primary found nothing about that pair of entities, so the rules fill gaps
    without duplicating or contradicting the LLM."""

    def __init__(self, primary: Extractor, secondary: Optional[Extractor] = None):
        self.primary = primary
        self.secondary = secondary or RuleBasedExtractor()

    def extract_text(self, text: str) -> list[Triple]:
        main = self.primary.extract_text(text)
        seen = {frozenset((canonical(t.subject), canonical(t.object))) for t in main}
        extra = [t for t in self.secondary.extract_text(text)
                 if frozenset((canonical(t.subject), canonical(t.object))) not in seen]
        return main + extra

    def extract_table(self, rows: list[list[str]]) -> list[Triple]:
        return self.primary.extract_table(rows)


def make_extractor(kind: str = "rules", model: Optional[str] = None,
                   host: Optional[str] = None, **kw: Any) -> Extractor:
    """Factory: ``"rules"`` (rule-based), ``"ollama"`` (LLM, rule-based
    fallback) or ``"hybrid"`` (LLM + rules filling the gaps)."""
    kind = kind.lower()
    if kind == "rules":
        return RuleBasedExtractor()
    if kind == "ollama":
        return OllamaExtractor(model=model, host=host, **kw)
    if kind == "hybrid":
        return HybridExtractor(OllamaExtractor(model=model, host=host, **kw),
                               RuleBasedExtractor())
    raise ValueError(f"unknown extractor {kind!r} (rules | ollama | hybrid)")


# --------------------------------------------------------------------------
# Entity resolution
# --------------------------------------------------------------------------


def canonical(name: str) -> str:
    return re.sub(r"[^\w\s]", "", name).lower().strip()


class EntityResolver:
    """Merges surface forms that normalise to the same key (lower-cased,
    punctuation stripped) and keeps the longest spelling for display."""

    def __init__(self) -> None:
        self.canon: dict[str, str] = {}  # key -> display name

    def resolve(self, name: str) -> str:
        key = canonical(name)
        self.canon.setdefault(key, name)
        if len(name) > len(self.canon[key]):  # prefer the fuller spelling
            self.canon[key] = name
        return key

    def display(self, key: str) -> str:
        return self.canon[key]


# --------------------------------------------------------------------------
# LangGraph nodes
# --------------------------------------------------------------------------


def make_pipeline(
    extractor: Optional[Extractor] = None,
    figure_captioner: Optional[Callable[[bytes], str]] = None,
    assets_dir: Optional[str] = None,
):
    """Build and compile the LangGraph pipeline. With ``assets_dir`` an extra
    step exports figures (PNG) and tables (CSV) there; see ``export_assets``."""
    extractor = extractor or RuleBasedExtractor()

    def load(state: KGState) -> KGState:
        p = state["path"]
        ext = Path(urlparse(p).path).suffix.lower()
        if is_url(p) and ext not in (".pdf", ".html", ".htm", ".xhtml", ".xml"):
            # Extensionless URL: sniff the content instead.
            raw = read_source(p)
            ext = ".pdf" if raw[:1024].lstrip().startswith(b"%PDF") else ".html"
        if ext == ".xml":
            els = load_ecfr_xml(p)
        elif ext == ".pdf":
            els = load_pdf(p)
        elif ext in (".html", ".htm", ".xhtml"):
            els = load_html(p)
        else:
            raise ValueError(f"Unsupported file type: {ext!r} (need .pdf or .html)")
        for e in els:                           # 'Table 2--1', 'Table 5<U+2011>1', 'andSDC-2'; '2.3.3Criteria' -> '2.3.3 Criteria'
            if e.kind == "heading" and e.text:
                e.text = _tablecheck.fix_heading(e.text)
            if e.caption:
                fixed = _tablecheck.fix_caption(e.caption)
                if e.text == e.caption:
                    e.text = fixed
                e.caption = fixed
        _tablecheck.apply(els, _renumber)       # false tables (two-column prose, sentence lists) go back to paragraphs; TOC / glossary are flagged
        if figure_captioner:  # optional vision model for uncaptioned figures
            for e in els:
                data = e.meta.get("bytes")
                if e.kind == "figure" and not e.caption and data:
                    e.text = e.caption = figure_captioner(data)
        return {"elements": els}

    def structure(state: KGState) -> KGState:
        stack: list[Element] = []
        for e in state["elements"]:
            if e.kind == "heading":
                while stack and stack[-1].level >= e.level:
                    stack.pop()
                e.section_id = stack[-1].id if stack else None
                stack.append(e)
            else:
                e.section_id = stack[-1].id if stack else None
        return {"elements": state["elements"]}

    def export(state: KGState) -> KGState:
        root = export_assets(state["elements"], state["path"], assets_dir)
        return {"elements": state["elements"], "assets_dir": str(root)}

    def extract(state: KGState) -> KGState:
        out = []
        for e in state["elements"]:
            if e.kind == "table":
                ts = extractor.extract_table(e.table)
                if e.caption:
                    ts += extractor.extract_text(e.caption)
            elif e.kind in ("paragraph", "figure") and e.text and not _pred.is_toc_like(e.text):
                ts = extractor.extract_text(e.text)
                _check_grounding(ts, e.text)
            else:
                ts = []
            out.append((e, ts))
        return {"triples": out}

    def build(state: KGState) -> KGState:
        g = nx.MultiDiGraph(source=state["path"])
        if state.get("assets_dir"):
            g.graph["assets_dir"] = state["assets_dir"]
        demoted = sum(1 for e in state["elements"] if e.meta.get("demoted_table"))
        if demoted:
            g.graph["tables_demoted"] = demoted
        ocr_pages = sorted({e.page for e in state["elements"] if e.meta.get("ocr") and e.page})
        if ocr_pages:                                     # pages that were read by OCR, not from a text layer
            g.graph["ocr_pages"] = ocr_pages
            g.graph["ocr_engine"] = next(e.meta["ocr_engine"] for e in state["elements"] if e.meta.get("ocr"))
        doc_id = "doc:" + (Path(urlparse(state["path"]).path).name or state["path"])
        g.add_node(doc_id, type="Document", label=state.get("label") or doc_id[4:])
        resolver = EntityResolver()
        by_id = {e.id: e for e in state["elements"]}

        # Structural layer
        for e in state["elements"]:
            attrs = {"type": e.kind.capitalize(), "label": (e.text or e.id)[:80],
                     "text": e.text, "page": e.page}
            if e.kind == "table":
                attrs["rows"] = json.dumps(e.table)
                attrs["file"] = e.meta.get("file")  # exported CSV, if any
                if e.meta.get("front_matter"):
                    attrs["front_matter"] = e.meta["front_matter"]
                if e.meta.get("footnotes"):
                    attrs["footnotes"] = json.dumps(e.meta["footnotes"])      # [{row, col, marker}]: markers removed from the cell values
                if e.meta.get("repeated_header_rows_removed"):
                    attrs["repeated_header_rows_removed"] = e.meta["repeated_header_rows_removed"]
            if e.kind == "figure":
                attrs.update({k: v for k, v in e.meta.items() if k not in ("bytes", "src_abs")})
            if e.kind == "heading":
                attrs["level"] = e.level
            if e.meta.get("ocr"):
                attrs["ocr"] = "true"
                if e.meta.get("ocr_conf") is not None:
                    attrs["ocr_conf"] = e.meta["ocr_conf"]
            if e.meta.get("role") not in (None, "text"):
                attrs["role"] = e.meta["role"]  # list-item | footnote | formula | code
            g.add_node(e.id, **{k: v for k, v in attrs.items() if v not in (None, "")})
            parent = e.section_id if e.section_id in by_id else doc_id
            g.add_edge(parent, e.id, key="CONTAINS", relation="CONTAINS")
        for prev, nxt in zip(state["elements"], state["elements"][1:]):
            g.add_edge(prev.id, nxt.id, key="NEXT", relation="NEXT")

        # Semantic layer
        for e, triples in state["triples"]:
            for t in triples:
                s, o = resolver.resolve(t.subject), resolver.resolve(t.object)
                for key, typ in ((s, t.subject_type), (o, t.object_type)):
                    nid = "ent:" + key
                    if nid not in g:
                        g.add_node(nid, type=typ, label=resolver.display(key))
                    else:
                        g.nodes[nid]["label"] = resolver.display(key)
                    if not g.has_edge(e.id, nid, "MENTIONS"):
                        g.add_edge(e.id, nid, key="MENTIONS", relation="MENTIONS")
                if s == o:
                    continue
                table = t.subject_type == "TableRow"
                rel = _pred.relation(t.predicate, t.strict, table)
                ekey = f"{rel}|{e.id}" + (f"|{t.predicate}" if table else "")
                if not g.has_edge("ent:" + s, "ent:" + o, ekey):
                    g.add_edge("ent:" + s, "ent:" + o, key=ekey, relation=rel,
                               predicate=t.predicate, source=e.id, page=e.page)
        return {"graph": g}

    wf = StateGraph(KGState)
    for name, fn in [("load", load), ("structure", structure), ("export", export),
                     ("extract", extract), ("build", build)]:
        wf.add_node(name, fn)
    wf.set_entry_point("load")
    wf.add_edge("load", "structure")
    if assets_dir is not None:
        wf.add_edge("structure", "export")
        wf.add_edge("export", "extract")
    else:
        wf.add_edge("structure", "extract")
    wf.add_edge("extract", "build")  # entity resolution happens inside build
    wf.add_edge("build", END)
    return wf.compile()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def file_to_knowledge_graph(
    path: str,
    extractor: Optional[Extractor] = None,
    figure_captioner: Optional[Callable[[bytes], str]] = None,
    assets_dir: Optional[str] = None,
    doc_label: Optional[str] = None,
) -> nx.MultiDiGraph:
    """Convert a PDF or HTML file into a ``networkx.MultiDiGraph``.

    If ``assets_dir`` is given, figures (PNG) and tables (CSV) are also written
    to ``<assets_dir>/<sha256(path)>/`` (see ``export_assets``); the folder is
    recorded in ``g.graph["assets_dir"]`` and each figure/table node gets a
    ``file`` attribute.

    Node ``type``s: Document, Heading, Paragraph, Table, Figure, plus extracted
    entities (and ``TableRow``/``Value`` for table-derived facts).
    Edge ``relation``s: CONTAINS (hierarchy), NEXT (reading order),
    MENTIONS (element -> entity), and extracted predicates between entities.
    """
    if not is_url(path) and not Path(path).is_file():
        raise FileNotFoundError(path)
    return make_pipeline(extractor, figure_captioner, assets_dir).invoke(
        {"path": path, **({"label": doc_label} if doc_label else {})})["graph"]


def export_graph(g: nx.MultiDiGraph, out_path: str) -> None:
    """Write the graph as GraphML (.graphml) or node-link JSON (.json)."""
    ext = Path(out_path).suffix.lower()
    if ext == ".json":
        Path(out_path).write_text(json.dumps(nx.node_link_data(g, edges="edges"),
                                             indent=2, default=str))
    elif ext == ".graphml":
        h = nx.MultiDiGraph()  # GraphML only supports scalar attributes
        h.add_nodes_from((n, {k: str(v) for k, v in d.items()}) for n, d in g.nodes(data=True))
        h.add_edges_from((u, v, k, {a: str(b) for a, b in d.items()})
                         for u, v, k, d in g.edges(keys=True, data=True))
        nx.write_graphml(h, out_path)
    else:
        raise ValueError("out_path must end in .json or .graphml")


DEFAULT_KG_DIR = "kg_store"  # TBD: override via out_dir= or $KG_OUTPUT_DIR


def source_hash(source: str) -> str:
    """Stable SHA-256 hex digest identifying a PDF/HTML input. URLs are hashed
    as given; local paths are resolved to absolute form first, so ``a.pdf`` and
    ``./a.pdf`` map to the same hash."""
    key = source if is_url(source) else str(Path(source).expanduser().resolve())
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def save_knowledge_graph(
    g: nx.MultiDiGraph,
    source: Optional[str] = None,
    out_dir: Optional[str] = None,
    fmt: str = "json",
) -> Path:
    """Save ``g`` to ``<out_dir>/<sha256(source)>.<fmt>`` and return the path.

    ``source`` is the input file path or URL (defaults to the one recorded on
    the graph). ``out_dir`` defaults to ``$KG_OUTPUT_DIR`` or ``./kg_store``
    and is created if missing. ``fmt`` is "json" or "graphml". Saving the same
    source again overwrites its previous file (atomic replace).
    """
    source = source or g.graph.get("source")
    if not source:
        raise ValueError("source not given and not recorded on the graph")
    fmt = fmt.lower().lstrip(".")
    if fmt not in ("json", "graphml"):
        raise ValueError("fmt must be 'json' or 'graphml'")
    directory = Path(out_dir or os.environ.get("KG_OUTPUT_DIR") or DEFAULT_KG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{source_hash(source)}.{fmt}"
    tmp = final.with_name(f".{final.stem}.tmp.{fmt}")
    try:
        export_graph(g, str(tmp))
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return final


def load_knowledge_graph(
    source: str, out_dir: Optional[str] = None, fmt: str = "json"
) -> Optional[nx.MultiDiGraph]:
    """Load a previously saved graph for ``source``, or None if there is none.

    GraphML (``fmt="graphml"``) is also readable, but all attributes come back
    as strings."""
    directory = Path(out_dir or os.environ.get("KG_OUTPUT_DIR") or DEFAULT_KG_DIR)
    path = directory / f"{source_hash(source)}.{fmt.lower().lstrip('.')}"
    if not path.is_file():
        return None
    if path.suffix == ".json":
        return nx.node_link_graph(json.loads(path.read_text()), edges="edges")
    return nx.read_graphml(path)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Convert a PDF/HTML file or URL into a knowledge graph.")
    ap.add_argument("input", help="PDF/HTML path or URL")
    ap.add_argument("output", nargs="?", help="optional .json/.graphml path "
                    "(default: save to the KG store under a hash of the input)")
    ap.add_argument("--extractor", choices=["rules", "ollama", "hybrid"], default="rules",
                    help="rules (default), ollama (LLM, rules as fallback) or hybrid (both)")
    ap.add_argument("--model", help="Ollama model (default $OLLAMA_MODEL or llama3.1)")
    ap.add_argument("--ollama-host", help="Ollama URL (default $OLLAMA_HOST or http://localhost:11434)")
    ap.add_argument("--fetch", choices=FETCH_MODES,
                    help="how to download URLs: headless browser (default), plain http, or auto")
    ap.add_argument("--pdf-engine", choices=PDF_ENGINES,
                    help="PDF parser: layout (pymupdf-layout), basic, or auto (default)")
    ap.add_argument("--ocr", choices=["auto", "off", "force"],
                    help="OCR for PDF pages: auto = only pages without a text layer (default), off, force = all pages")
    ap.add_argument("--ocr-engine", choices=["auto", "tesseract", "rapidocr"])
    ap.add_argument("--ocr-lang", help="OCR language(s), e.g. eng or eng+deu (Tesseract codes)")
    ap.add_argument("--assets-dir", nargs="?", metavar="DIR",
                    const=os.environ.get("KG_ASSETS_DIR") or DEFAULT_ASSETS_DIR,
                    help="also export figures (PNG) and tables (CSV) to DIR/<hash-of-input>/ "
                         f"(default DIR: $KG_ASSETS_DIR or ./{DEFAULT_ASSETS_DIR})")
    a = ap.parse_args()
    if a.pdf_engine:
        set_pdf_engine(a.pdf_engine)
    set_ocr(a.ocr, a.ocr_engine, a.ocr_lang)
    if a.fetch:
        set_fetch_mode(a.fetch)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    kg = file_to_knowledge_graph(
        a.input, make_extractor(a.extractor, a.model, a.ollama_host),
        assets_dir=a.assets_dir)
    print(f"{kg.number_of_nodes()} nodes, {kg.number_of_edges()} edges")
    if kg.graph.get("assets_dir"):
        print("assets:", kg.graph["assets_dir"])
    if a.output:
        export_graph(kg, a.output)
    else:
        print("saved:", save_knowledge_graph(kg, a.input))
