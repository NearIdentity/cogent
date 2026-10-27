"""Table validity check: is a detected "table" really a table?

Layout detectors (PDF table finders, OCR layout models) often report two-column running text, lists of sentences and rosters as
tables. Such false tables flood the table database with junk and produce junk joins. ``classify`` scores a candidate from cheap,
explainable features and returns a verdict with the reasons; ``demote`` turns a rejected table back into paragraph text (left column
first, then right, so two-column prose reads in the right order).

Conservative by design: a table with a "Table N" caption needs a much higher prose score before it is rejected, and numeric
tables are only rejected for overwhelming reasons. ``KG_TABLE_CHECK=off`` disables the check.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

_NUM = re.compile(r"^[<>≤≥~±+\-−]?\s*\d[\d,]*(\.\d+)?(\s*[×x]\s*10\^?[-−]?\d+)?\s*(%|[A-Za-zµ°/·²³]{0,6})?$")
_CAPTION = re.compile(r"^\s*(table|tab\.)\s+[A-Z]?\d", re.I)
_NAME = re.compile(r"^(?:[A-Z]\.\s*)+[A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+)?$|^[A-Z][a-z'’\-]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z'’\-]+(?:\s+(?:Jr|Sr|II|III)\.?)?$")


@dataclass
class Verdict:
    """``kind``: ``table`` (keep) or ``prose`` (demote). ``score`` is the prose score; ``reasons`` explain it."""
    kind: str
    score: int
    reasons: list[str] = field(default_factory=list)
    roster: bool = False


def enabled() -> bool:
    return (os.environ.get("KG_TABLE_CHECK") or "on").lower() not in ("off", "0", "false", "no")


def is_numeric(cell: str) -> bool:
    return bool(_NUM.match((cell or "").strip()))


def looks_like_name(cell: str) -> bool:
    """A person's name such as ``G. Bagchi`` or ``Jane Q. Public``."""
    return bool(_NAME.match((cell or "").strip()))


def name_share(values: list[str]) -> float:
    vals = [v for v in values if (v or "").strip()]
    return sum(looks_like_name(v) for v in vals) / len(vals) if vals else 0.0


def _words(c: str) -> int:
    return len(c.split())


def classify(rows: list[list[str]], caption: str = "") -> Verdict:
    """Prose score of a candidate table (rows[0] is the header row as detected)."""
    rows = [[(c or "").strip() for c in r] for r in rows if any((c or "").strip() for c in r)]
    if len(rows) < 2:
        return Verdict("prose", 9, ["fewer than two non-empty rows"])
    ncols = max(len(r) for r in rows)
    cells = [c for r in rows for c in r if c]
    if not cells:
        return Verdict("prose", 9, ["no text"])
    head, data = rows[0], rows[1:]
    lens = [len(c) for c in cells]
    mean_len = sum(lens) / len(lens)
    long_share = sum(n > 80 for n in lens) / len(lens)
    numeric_share = sum(is_numeric(c) for c in cells) / len(cells)
    words = [_words(c) for c in cells]
    sentence_share = sum(w >= 6 for w in words) / len(words)
    score, why = 0, []
    if mean_len > 60:
        score, why = score + 2, why + [f"mean cell length {mean_len:.0f} characters"]
    elif mean_len > 40:
        score, why = score + 1, why + [f"mean cell length {mean_len:.0f} characters"]
    if long_share > 0.3:
        score, why = score + 2, why + [f"{long_share:.0%} of the cells are longer than 80 characters"]
    if numeric_share < 0.1:
        score, why = score + 1, why + ["almost no numeric cells"]
    hdr = [h for h in head if h]
    frag = sum(1 for h in hdr if _words(h) >= 5 or h[:1].islower() or h.endswith((",", ";", "-")) or re.search(r"\w\.\s+\w", h)) if hdr else 0
    if hdr and frag / len(hdr) >= 0.5:
        score, why = score + 2, why + ["header cells read like sentence fragments"]
    if ncols == 2:
        both = sum(1 for r in data if len(r) >= 2 and _words(r[0]) >= 5 and _words(r[1]) >= 5)
        if data and both / len(data) >= 0.6:
            score, why = score + 2, why + ["two columns of running text"]
    if ncols == 1:
        score, why = score + (3 if mean_len > 40 else 1), why + ["a single column"]
    if sentence_share > 0.5 and numeric_share < 0.2:
        score, why = score + 1, why + [f"{sentence_share:.0%} of the cells are sentences"]
    captioned = bool(_CAPTION.match(caption or "")) or bool(_CAPTION.match(head[0] if head else ""))
    limit = 6 if captioned else 4
    if numeric_share >= 0.4:
        limit += 2                                   # numeric tables are rejected only for overwhelming reasons
    roster = ncols >= 2 and name_share([c for r in data for c in r[:1]]) >= 0.7 and numeric_share < 0.1
    if roster:
        why.append("a list of person names")
    return Verdict("prose" if score >= limit else "table", score, why, roster)


def _flat(row: list[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", " ".join(c for c in row if c).lower()).strip()


def rows_similar(a: list[str], b: list[str], threshold: float = 0.72) -> bool:
    """Are two rows the same header repeated (allowing OCR noise)? Compared as text, so a changed column count does not matter."""
    fa, fb = _flat(a), _flat(b)
    if not fa or not fb:
        return False
    return fa == fb or SequenceMatcher(None, fa, fb).ratio() >= threshold


def _header_like(rows: list[list[str]]) -> bool:
    cells = [c for r in rows for c in r if c]
    return bool(cells) and sum(is_numeric(c) for c in cells) / len(cells) <= 0.5 and sum(len(_flat([c])) for c in cells) >= 6


def header_prefix_len(base: list[list[str]], new: list[list[str]]) -> int:
    """How many leading rows of a continuation ``new`` repeat the header block of ``base`` (0..3)."""
    for k in (3, 2, 1):
        if len(base) > k and len(new) > k and _header_like(base[:k]) and all(rows_similar(new[j], base[j]) for j in range(k)):
            return k
    return 0


def drop_repeated_headers(rows: list[list[str]]) -> tuple[list[list[str]], int]:
    """Remove the header block (1-3 rows) where it is repeated inside the table, as page breaks do. Returns (rows, number of rows removed)."""
    if len(rows) < 4:
        return rows, 0
    best_rows, best_removed = rows, 0
    for k in (1, 2, 3):
        head = rows[:k]
        if len(rows) <= k or not _header_like(head):
            continue
        out, i, removed = list(head), k, 0
        while i < len(rows):
            if i + k <= len(rows) and all(rows_similar(rows[i + j], head[j]) for j in range(k)):
                i, removed = i + k, removed + k
            else:
                out.append(rows[i])
                i += 1
        if removed > best_removed:
            best_rows, best_removed = out, removed
    return best_rows, best_removed


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-")


def fix_caption(text: str) -> str:
    """Normalise a table / figure caption: every dash variant between alphanumerics becomes '-', repeated hyphens collapse ('Table 2--1' ->
    'Table 2-1', 'Table 5\u20111' -> 'Table 5-1'), and a glued acronym gets its space back ('andSDC-2' -> 'and SDC-2')."""
    t = re.sub(r"\s+", " ", (text or "").translate(_DASHES)).strip()
    t = re.sub(r"(?<=[A-Za-z0-9])-{2,}(?=[A-Za-z0-9])", "-", t)
    return re.sub(r"(?<=[a-z]{2})(?=[A-Z]{2,}[-\d])", " ", t)


def fix_heading(text: str) -> str:
    """Give a section number its space back ('2.3.3Criteria and Guidance' -> '2.3.3 Criteria and Guidance'); multi-level numbers only."""
    return re.sub(r"^(\d+(?:\.\d+)+)(?=[A-Z][a-z]|[A-Z]{3,}\b)", r"\1 ", text or "")


_LEADER = re.compile(r"\.{4,}|(?:\.\s){4,}|…{2,}")
_SECTION_NO = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.?|appendix\s+\w+|annex\s+\w+)$", re.I)
_ACRONYM = re.compile(r"^[A-Z][A-Za-z0-9&/\-]{1,9}$")


_STORE_HEAD = {"price", "item number", "sku", "add to cart", "list price", "member price"}
_STORE_CELL = re.compile(r"add to (?:cart|basket)|\bcheckout\b|\bin stock\b|\bsign in to (?:buy|purchase)\b|\bbuy now\b", re.I)
_INDEX_NAME = re.compile(r"^\s*(?:(?:subject|numerical|alphabetical|keyword|author|general|interpretations?)\s+)?index(?:\s+of\s+interpretations)?(?:\s*\(continued\))?\s*$", re.I)
_SECTION_NUM = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[A-Z])[.)]?\s+")


def front_matter(rows: list[list[str]], caption: str = "", section: str = "") -> Optional[str]:
    """``'toc'`` for a table of contents (dot leaders, or section numbers with a rising page-number column), ``'glossary'`` for a list of
    acronyms and their expansions, ``'index'`` for an index (named so in its caption or section), ``'store'`` for a product / order box of a web
    shop ('Quantity | Item', 'Add to cart'); None for anything else. Such tables describe the document or the page, not its design data."""
    rows = [[(c or "").strip() for c in r] for r in rows if any((c or "").strip() for c in r)]
    if rows:
        head = {c.lower() for c in rows[0] if c}
        # a shop's order box: shop words in a cell, two shop column names, or the bare 'Quantity | Item' pair of a table without a 'Table N' caption
        if (len(head & _STORE_HEAD) >= 2 or any(_STORE_CELL.search(c) for r in rows for c in r)
                or (head == {"quantity", "item"} and len(rows) <= 8 and not _CAPTION.match(caption or ""))):
            return "store"
        last = _SECTION_NUM.sub("", section.split(" > ")[-1]) if section else ""
        if not _CAPTION.match(caption or "") and (_INDEX_NAME.match(caption or "") or _INDEX_NAME.match(last)):      # the title IS 'Subject Index'; 'Index Properties of Soils' is data
            return "index"
    if len(rows) < 4:
        return None
    ncols = max(len(r) for r in rows)
    if re.search(r"table of contents|^\s*contents\b|list of (figures|tables)", " ".join(r[0] for r in rows[:1] if r) + " " + (caption or ""), re.I):
        return "toc"
    leaders = sum(1 for r in rows if any(_LEADER.search(c) for c in r))
    if leaders / len(rows) >= 0.5:
        return "toc"
    pages = []
    for r in rows:
        last = next((c for c in reversed(r) if c), "")
        pages.append(int(last) if re.fullmatch(r"\d{1,4}", last) else None)
    numbered = sum(1 for r in rows if r and _SECTION_NO.match(r[0]))
    have = [p for p in pages if p is not None]
    if ncols >= 3 and len(rows) >= 5 and len(have) >= 0.7 * len(rows) and numbered >= 0.6 * len(rows):
        rising = sum(1 for a, b in zip(have, have[1:]) if b >= a)
        if rising >= 0.8 * max(1, len(have) - 1):
            return "toc"
    if ncols == 2:
        head = " ".join(rows[0]).lower()
        data = rows[1:] if re.search(r"acronym|abbreviation|term|symbol", head) else rows
        short = sum(1 for r in data if len(r) == 2 and _ACRONYM.match(r[0]) and sum(ch.isupper() for ch in r[0]) >= 2 and len(r[1].split()) >= 2 and not any(ch.isdigit() for ch in r[1]))
        need = 0.5 if re.search(r"acronym|abbreviation", head) else 0.7
        if len(data) >= 5 and short / len(data) >= need:
            return "glossary"
    return None


def demote_text(rows: list[list[str]]) -> str:
    """The text of a rejected table in reading order: the first column top to bottom, then the second, and so on."""
    width = max((len(r) for r in rows), default=0)
    cols = []
    for j in range(width):
        parts = [r[j].strip() for r in rows if j < len(r) and r[j].strip()]
        if parts:
            cols.append(" ".join(parts))
    return "\n\n".join(cols)


def _flat_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def caption_fragment(caption: str, first_row: Optional[list[str]] = None) -> bool:
    """A 'caption' that is really a piece of the header row ('| Millions', 'Rdilid | TABLE 1 Hazard Ca | -1: THRESHOL ...'): it starts or ends with a
    column separator, has two or more inner separators (cells joined together), or is the start of the header row's own text. A short real caption
    ('Damping Values') and anything labelled 'Table N' are not fragments."""
    c = (caption or "").strip()
    if not c or _CAPTION.match(c):
        return False
    if c.startswith("|") or c.endswith("|") or c.count("|") >= 2:
        return True
    if first_row:
        row, cap = _flat_norm(" ".join(x or "" for x in first_row)), _flat_norm(c)
        if len(cap) >= 4 and row.startswith(cap):
            return True
    return False


_LABEL_PAIR = re.compile(r"\bTABLE\s*([A-Z]?\d+)(?:[^|\n]{0,30}\|\s*|\s*)-\s*(\d+)\b", re.I)
_LABEL_WHOLE = re.compile(r"\bTABLE\s*([A-Z]?\d+(?:[-.]\d+)+)\b", re.I)


def recover_label(fragment: str) -> str:
    """The 'Table N-M' label hidden in a garbled header-row fragment ('... | TABLE 1 Hazard Ca | -1: ...' -> 'Table 1-1'), or ''."""
    m = _LABEL_WHOLE.search(fragment or "")
    if m:
        return f"Table {m.group(1)}"
    m = _LABEL_PAIR.search(fragment or "")
    return f"Table {m.group(1)}-{m.group(2)}" if m else ""


def apply(elements: list, renumber=None) -> dict[str, int]:
    """Demote the false tables among ``elements`` (kg_ingest.Element objects) in place; returns counts. ``renumber`` re-assigns ids."""
    stats = {"checked": 0, "demoted": 0, "rosters": 0, "front_matter": 0}
    if not enabled():
        return stats
    stack: list = []                                      # headings above the current element (level, text)
    for e in elements:
        if e.kind == "heading":
            while stack and stack[-1][0] >= e.level:
                stack.pop()
            stack.append((e.level, (e.text or "").strip()))
        if e.kind != "table" or not e.table:
            continue
        section = " > ".join(t for _, t in stack if t)
        if caption_fragment(e.caption, e.table[0] if e.table else None) and stack:        # use the section heading instead of a header-row fragment
            frag, label = e.caption, stack[-1][1]
            tag = recover_label(frag)                      # the 'Table 1-1' that the garbled caption still carries
            if tag:
                label = f"{tag} ({label})"
                e.meta["recovered_label"] = tag
            if e.text == e.caption:
                e.text = label
            e.caption = label
            e.meta["caption_fragment"] = frag
            stats["captions_replaced"] = stats.get("captions_replaced", 0) + 1
        stats["checked"] += 1
        rows, removed = drop_repeated_headers(e.table)
        if removed:
            e.table = rows
            e.meta["repeated_header_rows_removed"] = removed
            stats["header_rows_removed"] = stats.get("header_rows_removed", 0) + removed
            if e.text and not e.caption:
                e.text = " | ".join(rows[0])
        fm = front_matter(e.table, e.caption, section)
        if fm:                                           # stays in the graph, but is not exported as a data table
            e.meta["front_matter"] = fm
            stats["front_matter"] += 1
            continue
        v = classify(e.table, e.caption)
        if v.kind == "prose":
            stats["demoted"] += 1
            e.meta["demoted_table"] = {"score": v.score, "reasons": v.reasons, "rows": len(e.table), "cols": max(len(r) for r in e.table)}
            e.text = demote_text(e.table)
            e.kind, e.table = "paragraph", None
        elif v.roster:
            stats["rosters"] += 1
            e.meta["roster"] = True
    if stats["demoted"] and renumber is not None:
        renumber(elements)
    return stats
