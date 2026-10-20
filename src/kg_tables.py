"""The table model: turn an extracted CSV (plus its place in the document KG) into typed columns and embedding text.

Pure functions, no database and no network, so everything here is unit-testable.

* ``read_csv`` / ``frame``      CSV -> header + data rows (header detection, de-duplicated names, ragged rows padded)
* ``parse_number``              '1,200', '<0.5', '5 %', '1.0E-4', '1.0 x 10^-4 g' -> (value, unit)
* ``profile``                   per-column Postgres type, role, unit, distinct counts, key candidates, samples
* ``kg_context``                what the document KG says about a table: section path, caption, intro / notes, entities,
                                cited identifiers, paragraphs that refer to it ("see Table 3-1")
* ``row_text`` / ``table_text`` the text that gets embedded. Default ``mode="full"``: *every* cell of the row (``header: value``),
                                the column names (with units), the table metadata and the KG context. ``mode="text"`` leaves the
                                numeric cell values out (only their column names remain).
"""
from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import networkx as nx

RECIPE = "v2"                       # bump when the embedding text recipe changes (forces re-embedding)
ROW_MODES = ("full", "text")        # full: every cell of the row is embedded (default); text: numeric cell values are left out


def recipe(mode: str = "full") -> str:
    """Recipe id stored with the database: the text recipe version plus the row mode (vectors of different recipes must not be mixed)."""
    if mode not in ROW_MODES:
        raise ValueError(f"row mode must be one of {ROW_MODES}")
    return f"{RECIPE}-{mode}"
MAX_SAMPLES = 5

# ---------------------------------------------------------------------------------------------------- numbers
_NUM = re.compile(
    r"""^\s*(?P<pre>[<>≤≥~±+\-−–]{0,2})\s*
        (?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)
        (?:\s*(?:[eE]\s*(?P<e1>[+\-−–]?\d+)|[x×]\s*10\s*\^?\s*(?P<e2>[+\-−–]?\d+)))?
        \s*(?P<unit>%|[A-Za-zµμ°/²³·\-]{1,10}\.?(?:\s?/\s?[A-Za-z²³]{1,6})?)?\s*$""", re.X)


def parse_number(cell: str) -> Optional[tuple[float, str]]:
    """Value and unit of a numeric cell, or None. Inequality signs are ignored (the raw text keeps them)."""
    m = _NUM.match(cell or "")
    if not m:
        return None
    try:
        v = float(m["num"].replace(",", ""))
        e = m["e1"] or m["e2"]
        if e:
            v *= 10 ** int(e.replace("−", "-").replace("–", "-"))
        if m["pre"] and m["pre"][0] in "-−–":
            v = -v
    except ValueError:
        return None
    return v, (m["unit"] or "")


def is_numeric_cell(cell: str) -> bool:
    return parse_number(cell) is not None


# ------------------------------------------------------------------------------------------------------ CSV
def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [[(c or "").replace("\r", " ").strip() for c in row] for row in csv.reader(fh)]


@dataclass
class Col:
    name: str                       # Postgres column name
    header: str                     # original header text
    pg_type: str = "text"           # text | double precision | bigint
    role: str = "text"              # text | numeric | key
    unit: str = ""
    n_nonnull: int = 0
    n_distinct: int = 0
    key_candidate: bool = False
    samples: list[str] = field(default_factory=list)
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    quantity: str = ""                                 # physical dimension the header / unit suggests (pressure, length, ...)
    unit_source: str = ""                              # evidence behind ``unit`` (header, cells, table_declaration, ...)
    unit_confidence: float = 0.0
    unit_candidates: list[dict] = field(default_factory=list)   # every unit hypothesis with its evidence (kg_units.candidates)


@dataclass
class Frame:
    header: list[str]
    rows: list[list[str]]
    header_detected: bool = True


def frame(rows: list[list[str]]) -> Optional[Frame]:
    """Header + data rows from raw CSV rows; None if there is no usable table (needs >= 2 columns and >= 1 data row)."""
    rows = [r for r in rows if any(c for c in r)]
    if len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i] for r in rows)]            # drop columns that are empty everywhere
    rows = [[r[i] for i in keep] for r in rows]
    if len(keep) < 2:
        return None
    first = rows[0]
    numeric_first = sum(1 for c in first if is_numeric_cell(c)) / max(1, sum(1 for c in first if c))
    if numeric_first >= 0.6:                                              # first row is data: synthesise a header
        return Frame([f"column {i + 1}" for i in range(len(first))], rows, header_detected=False)
    header = [re.sub(r"\s+", " ", c).strip() or f"column {i + 1}" for i, c in enumerate(first)]
    return Frame(header, rows[1:], True)


_RESERVED = {"user", "table", "select", "order", "group", "from", "where", "end", "all", "check", "column", "default", "desc",
             "asc", "primary", "references", "unique", "limit", "offset", "to", "in", "is", "not", "null", "true", "false",
             "analyse", "analyze", "and", "or", "as", "case", "cast", "create", "current_user", "do", "else", "for",
             "foreign", "grant", "having", "into", "join", "like", "on", "only", "returning", "some", "then", "union", "with"}


def pg_identifier(header: str, used: set[str], maxlen: int = 40) -> str:
    """ASCII snake_case column name, unique within ``used`` (which it updates), never a reserved word or '_...'."""
    s = unicodedata.normalize("NFKD", header).encode("ascii", "ignore").decode()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)                           # units in brackets are kept in Col.unit instead
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()[:maxlen] or "col"
    if s[0].isdigit() or s in _RESERVED or s.startswith("kgt"):
        s = "c_" + s
    base, n = s, 2
    while s in used:
        s = f"{base}_{n}"
        n += 1
    used.add(s)
    return s


def header_unit(header: str) -> str:
    m = re.search(r"\(([^)]{1,16})\)|\[([^\]]{1,16})\]", header)
    return (m.group(1) or m.group(2)).strip() if m else ""


def profile(fr: Frame) -> list[Col]:
    """Per-column profile. A column is numeric when >= 90 % of its non-empty cells parse as numbers."""
    used: set[str] = set()
    cols = []
    for j, h in enumerate(fr.header):
        cells = [r[j] for r in fr.rows]
        nonempty = [c for c in cells if c]
        nums = [parse_number(c) for c in nonempty]
        parsed = [p for p in nums if p]
        c = Col(name=pg_identifier(h, used), header=h, n_nonnull=len(nonempty), unit=header_unit(h))
        c.n_distinct = len({x.lower() for x in nonempty})
        c.samples = list(dict.fromkeys(nonempty))[:MAX_SAMPLES]
        if nonempty and len(parsed) / len(nonempty) >= 0.9:
            c.role = "numeric"
            c.pg_type = "bigint" if all(v.is_integer() and abs(v) < 9e15 for v, _ in parsed) and not any("." in x for x in nonempty) else "double precision"
            vals = [v for v, _ in parsed]
            c.vmin, c.vmax = min(vals), max(vals)
            units = {u for _, u in parsed if u}
            if not c.unit and len(units) == 1:
                c.unit = units.pop()
        c.key_candidate = bool(nonempty) and c.n_distinct / len(nonempty) >= 0.98 and len(nonempty) >= 2
        if c.key_candidate and c.role == "text":
            c.role = "key"
        cols.append(c)
    return cols


def typed_value(cell: str, col: Col) -> Any:
    """The value stored in the typed column (None when the cell is empty or not numeric in a numeric column)."""
    if not cell:
        return None
    if col.pg_type == "text":
        return cell
    p = parse_number(cell)
    if p is None:
        return None
    return int(p[0]) if col.pg_type == "bigint" else p[0]


# --------------------------------------------------------------------------------------------------- KG context
_TABLE_LABEL = re.compile(r"^\s*(table\s+[A-Za-z]?[\dIVXivx]+(?:[.\-–][\dA-Za-z]+)*[A-Za-z]?)\b", re.I)
_NOTE = re.compile(r"^\s*(notes?\b|source\b|sources\b|\*|†|‡|\(?[a-z]\)\s|\d+\)\s)", re.I)


@dataclass
class Context:
    doc: str = ""
    section_path: list[str] = field(default_factory=list)
    caption: str = ""
    table_label: str = ""
    intro: str = ""
    notes: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    idents: list[str] = field(default_factory=list)
    referenced_by: list[str] = field(default_factory=list)      # ids of paragraphs / tables that mention this table's label
    section_texts: list[str] = field(default_factory=list)      # other paragraphs of the same section (used for unit deduction, not embedded)

    def sha(self) -> str:
        return hashlib.sha256("\x1f".join([RECIPE, self.doc, " > ".join(self.section_path), self.caption, self.intro,
                                           "|".join(self.notes), "|".join(self.entities), "|".join(self.idents),
                                           "|".join(self.section_texts)]).encode()).hexdigest()[:16]


def _txt(g: nx.MultiDiGraph, n: str, limit: int = 300) -> str:
    d = g.nodes[n]
    return re.sub(r"\s+", " ", str(d.get("text") or d.get("label") or "")).strip()[:limit]


def _rel_neighbors(g, n: str, rel: str, direction: str) -> list[str]:
    edges = g.in_edges(n, data=True) if direction == "in" else g.out_edges(n, data=True)
    return [(u if direction == "in" else v) for u, v, d in edges if d.get("relation") == rel]


def kg_context(g: Optional[nx.MultiDiGraph], node_id: str, caption: str = "", use_kg: bool = True) -> Context:
    """What the document KG knows about a table node (see module docstring). With no graph / unknown node only the caption."""
    ctx = Context(caption=re.sub(r"\s+", " ", caption or "").strip())
    m = _TABLE_LABEL.match(ctx.caption)
    ctx.table_label = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    if g is None or not use_kg or node_id not in g:
        return ctx
    doc = next((n for n, d in g.nodes(data=True) if d.get("type") == "Document"), None)
    ctx.doc = _txt(g, doc, 120) if doc else ""
    if not ctx.caption:
        ctx.caption = _txt(g, node_id, 160)
    seen, n, path = {node_id}, node_id, []
    while True:                                                         # section path: nearest heading first, then its parents
        parents = [p for p in _rel_neighbors(g, n, "CONTAINS", "in") if p not in seen]
        if not parents:
            break
        n = parents[0]
        seen.add(n)
        if g.nodes[n].get("type") == "Heading":
            path.append(_txt(g, n, 100))
    ctx.section_path = list(reversed(path))
    heading = next((p for p in _rel_neighbors(g, node_id, "CONTAINS", "in")), None)
    if heading is not None:                                             # sibling paragraphs of the table's section (KG neighbours)
        sibs = [c for c in _rel_neighbors(g, heading, "CONTAINS", "out") if c != node_id and g.nodes[c].get("type") == "Paragraph"]
        ctx.section_texts = [_txt(g, c, 300) for c in sibs[:8]]
    prev = _rel_neighbors(g, node_id, "NEXT", "in")
    if prev and g.nodes[prev[0]].get("type") == "Paragraph":
        ctx.intro = _txt(g, prev[0], 300)
    nxt, hops = _rel_neighbors(g, node_id, "NEXT", "out"), 0
    while nxt and hops < 3:                                             # footnote-like paragraphs right after the table
        d = g.nodes[nxt[0]]
        if d.get("type") == "Paragraph" and (d.get("role") == "footnote" or _NOTE.match(str(d.get("text", "")))):
            ctx.notes.append(_txt(g, nxt[0], 240))
            nxt, hops = _rel_neighbors(g, nxt[0], "NEXT", "out"), hops + 1
        else:
            break
    for src in [node_id] + ([prev[0]] if prev else []):
        for v in _rel_neighbors(g, src, "MENTIONS", "out"):
            lab = str(g.nodes[v].get("label", "")).strip()
            # the table's own cells come back as 'TableRow' / 'Value' entities (and numbers): they are not context
            if g.nodes[v].get("type") in ("TableRow", "Value") or is_numeric_cell(lab):
                continue
            if lab and lab not in ctx.entities:
                ctx.entities.append(lab)
        for v in _rel_neighbors(g, src, "CITES", "out"):
            lab = str(g.nodes[v].get("label", "")).strip()
            if lab and lab not in ctx.idents:
                ctx.idents.append(lab)
    ctx.entities, ctx.idents = ctx.entities[:8], ctx.idents[:6]
    if ctx.table_label:
        pat = re.compile(r"\b" + re.escape(ctx.table_label).replace(r"\ ", r"\s+") + r"(?![\w.\-–]*\d)", re.I)
        for n2, d in g.nodes(data=True):
            if n2 != node_id and d.get("type") in ("Paragraph", "Heading", "Table", "Figure") and pat.search(str(d.get("text") or "")):
                if not (d.get("type") == "Table" and _TABLE_LABEL.match(str(d.get("text") or "")) and pat.match(str(d.get("text") or ""))):
                    ctx.referenced_by.append(n2)
    return ctx


# ------------------------------------------------------------------------------------------ embedding text
def _head(ctx: Context) -> str:
    return " | ".join(x for x in (ctx.doc, " > ".join(ctx.section_path), ctx.caption) if x)


def _columns_line(cols: list[Col]) -> str:
    return "Columns: " + ", ".join(c.header + (f" ({c.unit})" if c.unit and f"({c.unit})" not in c.header else "") for c in cols)


def _context_lines(ctx: Context) -> list[str]:
    out = []
    if ctx.intro:
        out.append(f"Context: {ctx.intro}")
    if ctx.notes:
        out.append("Notes: " + " ".join(ctx.notes)[:400])
    if ctx.entities or ctx.idents:
        out.append("Related: " + "; ".join(ctx.entities + ctx.idents))
    return out


def row_text(ctx: Context, cols: list[Col], row: list[str], mode: str = "full") -> str:
    """Text embedded for one row. ``full``: every non-empty cell as ``header: value``. ``text``: numeric cells are left out on purpose
    (their *column names* are not)."""
    if mode == "full":
        pairs = [f"{c.header}: {cell}" for c, cell in zip(cols, row) if cell]
        lines = [_head(ctx), _columns_line(cols), "; ".join(pairs)]
    else:
        pairs = [f"{c.header}: {cell}" for c, cell in zip(cols, row) if cell and not is_numeric_cell(cell)]
        numeric = [c.header for c, cell in zip(cols, row) if cell and is_numeric_cell(cell)]
        lines = [_head(ctx), _columns_line(cols), "; ".join(pairs)]
        if numeric:
            lines.append("Values given for: " + ", ".join(numeric))
    return "\n".join(x for x in lines + _context_lines(ctx) if x)


def table_text(ctx: Context, cols: list[Col], rows: list[list[str]]) -> str:
    """Text embedded for the table as a whole: metadata, column names, and the row labels (text cells of the first text column)."""
    j = next((i for i, c in enumerate(cols) if c.role in ("text", "key")), None)
    labels = list(dict.fromkeys(r[j] for r in rows if j is not None and r[j] and not is_numeric_cell(r[j])))[:30]
    lines = [_head(ctx), _columns_line(cols), ("Rows: " + "; ".join(labels)) if labels else ""]
    return "\n".join(x for x in lines + _context_lines(ctx) if x)
