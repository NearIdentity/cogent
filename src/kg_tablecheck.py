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


def demote_text(rows: list[list[str]]) -> str:
    """The text of a rejected table in reading order: the first column top to bottom, then the second, and so on."""
    width = max((len(r) for r in rows), default=0)
    cols = []
    for j in range(width):
        parts = [r[j].strip() for r in rows if j < len(r) and r[j].strip()]
        if parts:
            cols.append(" ".join(parts))
    return "\n\n".join(cols)


def apply(elements: list, renumber=None) -> dict[str, int]:
    """Demote the false tables among ``elements`` (kg_ingest.Element objects) in place; returns counts. ``renumber`` re-assigns ids."""
    stats = {"checked": 0, "demoted": 0, "rosters": 0}
    if not enabled():
        return stats
    for e in elements:
        if e.kind != "table" or not e.table:
            continue
        stats["checked"] += 1
        rows, removed = drop_repeated_headers(e.table)
        if removed:
            e.table = rows
            e.meta["repeated_header_rows_removed"] = removed
            stats["header_rows_removed"] = stats.get("header_rows_removed", 0) + removed
            if e.text and not e.caption:
                e.text = " | ".join(rows[0])
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
