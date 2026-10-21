"""Reading order of text blocks on a page (R-01) and a plausibility check of heading numbers.

Sorting a page's blocks by their vertical position interleaves the columns of a two-column page (heading 3 of the right column lands between
headings 1 and 2 of the left one). ``order`` finds the gutters from the blocks themselves, so it needs no fixed page middle:

1. blocks as wide as most of the page (titles, wide tables, figures) split the page into horizontal bands and keep their place;
2. inside a band the gutters are the empty vertical strips between columns of text (found on a histogram of covered width, tolerant of a
   few stray boxes bridging them); the band is read column by column, left to right, each column top to bottom.

Anything that does not look like 2 or 3 clean columns is read top to bottom, as before.
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Callable, Optional, Sequence

SPAN_FRACTION = 0.62          # a block this wide (of the page width) crosses the columns
BINS = 240
MIN_GUTTER = 0.02             # of the page width
MAX_COLUMNS = 3


def _row_sorted(boxes: Sequence[Any], get: Callable[[Any], tuple[float, float, float, float]]) -> list[Any]:
    """Top to bottom; boxes on the same text row left to right."""
    if not boxes:
        return []
    hs = [max(1.0, get(b)[3] - get(b)[1]) for b in boxes]
    unit = 0.6 * (statistics.median(hs) or 10.0)
    return sorted(boxes, key=lambda b: (round(((get(b)[1] + get(b)[3]) / 2) / unit), get(b)[0]))


def _columns(band: Sequence[Any], get, page_w: float) -> Optional[list[list[Any]]]:
    """Split a band of narrow blocks into columns, or None when it is not clearly 2-3 columns."""
    if len(band) < 4:
        return None
    x_lo = min(get(b)[0] for b in band)
    x_hi = max(get(b)[2] for b in band)
    if x_hi - x_lo < 0.3 * page_w:
        return None
    n = BINS
    scale = n / max(x_hi - x_lo, 1.0)
    mass = [0.0] * n
    for b in band:
        x0, y0, x1, y1 = get(b)
        a, z = int((x0 - x_lo) * scale), min(n - 1, int((x1 - x_lo) * scale))
        for i in range(max(0, a), z + 1):
            mass[i] += max(1.0, y1 - y0)
    peak = max(mass) or 1.0
    empty = [m <= 0.04 * peak for m in mass]
    total = sum(mass) or 1.0
    gutters: list[float] = []
    i = 0
    while i < n:
        if empty[i]:
            j = i
            while j + 1 < n and empty[j + 1]:
                j += 1
            width = (j - i + 1) / scale
            left, right = sum(mass[:i]), sum(mass[j + 1:])
            if width >= MIN_GUTTER * page_w and left >= 0.15 * total and right >= 0.15 * total:
                gutters.append(x_lo + (i + j + 1) / 2 / scale)
            i = j + 1
        else:
            i += 1
    if not gutters or len(gutters) + 1 > MAX_COLUMNS:
        return None
    cols: list[list[Any]] = [[] for _ in range(len(gutters) + 1)]
    for b in band:
        x0, _, x1, _ = get(b)
        cols[sum(1 for g in gutters if (x0 + x1) / 2 > g)].append(b)
    return cols if all(len(c) >= 2 for c in cols) else None


def order(boxes: Sequence[Any], page_w: float, get: Optional[Callable[[Any], tuple[float, float, float, float]]] = None) -> list[Any]:
    """``boxes`` in reading order. ``get(box)`` returns (x0, y0, x1, y1); by default the attributes x0, y0, x1, y1 are used."""
    get = get or (lambda b: (b.x0, b.y0, b.x1, b.y1))
    boxes = list(boxes)
    if len(boxes) < 4 or page_w <= 0:
        return _row_sorted(boxes, get)
    spanning = [b for b in boxes if get(b)[2] - get(b)[0] >= SPAN_FRACTION * page_w]
    span_ids = {id(b) for b in spanning}
    others = [b for b in boxes if id(b) not in span_ids]
    out: list[Any] = []
    # bands: runs of non-spanning boxes between spanning ones (by vertical position)
    cuts = sorted(get(b)[1] for b in spanning)
    bands: list[list[Any]] = [[] for _ in range(len(cuts) + 1)]
    for b in others:
        cy = (get(b)[1] + get(b)[3]) / 2
        bands[sum(1 for c in cuts if cy > c)].append(b)
    spanning = sorted(spanning, key=lambda b: get(b)[1])
    for k, band in enumerate(bands):
        cols = _columns(band, get, page_w)
        if cols:
            for col in cols:
                out += _row_sorted(col, get)
        else:
            out += _row_sorted(band, get)
        if k < len(spanning):
            out.append(spanning[k])
    return out


def top_numbers(headings: Sequence[tuple[str, str]]) -> list[tuple[int, str]]:
    """(number, title) of the headings that start with a single section number ('3 RESPONSIBILITY', '10. INSPECTION'); subsections are skipped."""
    out = []
    for _, title in headings:
        m = re.match(r"^\s*(\d{1,2})[\s.:)-]+\S", title or "")
        if m and not re.match(r"^\s*\d+\.\d", title or ""):
            out.append((int(m.group(1)), title.strip()))
    return out


def numbering_problems(headings: Sequence[tuple[str, str]], limit: int = 5) -> list[str]:
    """Out-of-sequence section numbers in a heading list (document order): a top-level number lower than one already seen
    ('3 RESPONSIBILITY' before '2 APPLICABILITY') means the blocks were read in the wrong order or the numbering restarts."""
    nums = top_numbers(headings)
    problems, high = [], 0
    for n, title in nums:
        if n < high:
            problems.append(f"heading {title[:50]!r} (section {n}) comes after section {high}")
        high = max(high, n)
    return problems[:limit]
