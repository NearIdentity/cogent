"""Markdown for the tabular data of the cogent MCP server.

The server returns tables as JSON (lookup_values rows, query_table rows, graph Table nodes, describe_table columns, ...). The client turns
them into Markdown *in code*: the cell text is copied verbatim from the server's answer and never retyped by the language model, so the
numbers on the screen are the numbers in the source. The interface renders this Markdown (see templates/index.html).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional, Sequence

from .models import Notice, SourceRef, TableView

MAX_CELL = 2000


def md_cell(value: Any) -> str:
    """One table cell as safe Markdown text: no newlines or pipes that would break the row, nothing else changed."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    else:
        text = str(value)
    text = re.sub(r"\s*[\r\n]+\s*", " ", text).strip()
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def md_table(columns: Sequence[Any], rows: Iterable[Sequence[Any]]) -> str:
    """A GitHub-style Markdown table. Short rows are padded and long rows cut to the number of columns."""
    cols = [md_cell(c) or " " for c in columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(" --- " for _ in cols) + "|"]
    for r in rows:
        cells = [md_cell(c) for c in list(r)[: len(cols)]]
        cells += [""] * (len(cols) - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def rows_to_markdown(rows: Sequence[Sequence[Any]]) -> str:
    """A table given as a list of rows whose first row is the header (graph Table nodes)."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    head = list(rows[0]) + [""] * (width - len(rows[0]))
    return md_table(head, rows[1:])


def md_inline(text: Any) -> str:
    """Text for a bold / italic line: the Markdown control characters are escaped."""
    return re.sub(r"([*_`\[\]<>\\])", r"\\\1", re.sub(r"\s+", " ", str(text or "")).strip())


def _source(table: dict[str, Any], table_id: str = "") -> SourceRef:
    src = table.get("source") or {}
    return SourceRef(kind="table", table_id=table_id or table.get("table_id", ""), document=src.get("document") or table.get("doc_label") or "",
                     url=src.get("url") or "", page=src.get("page") if isinstance(src.get("page"), int) else None,
                     section=src.get("section") or "", caption=src.get("caption") or table.get("caption") or "")


def _heading(title: str, src: Optional[SourceRef], extra: str = "") -> str:
    bits = []
    if src:
        if src.document:
            bits.append(src.document)
        if src.page:
            bits.append(f"p. {src.page}")
        if src.section:
            bits.append(src.section)
    if extra:
        bits.append(extra)
    line = f"**{md_inline(title)}**"
    return line + (f"\n*{md_inline(', '.join(bits))}*" if bits else "")


def _view(title: str, columns: list[str], rows: list[list[Any]], src: Optional[SourceRef], extra: str = "", notes: Sequence[str] = ()) -> TableView:
    parts = [_heading(title, src, extra), md_table(columns, rows)]
    parts += [f"> {md_inline(n)}" for n in notes if n]
    return TableView(title=title, markdown="\n\n".join(parts), columns=[str(c) for c in columns], rows=[[md_cell(c) for c in r] for r in rows], source=src)


def _keys(rows: Iterable[dict[str, Any]]) -> list[str]:
    out: dict[str, None] = {}
    for r in rows:
        for k in r:
            out.setdefault(k, None)
    return list(out)


def _unit_notes(units: Optional[dict[str, Any]], notes: Optional[Sequence[str]]) -> list[str]:
    u = {k: v for k, v in (units or {}).items() if v}
    return ([f"Units: " + "; ".join(f"{k} → {v}" for k, v in u.items())] if u else []) + list(notes or [])


# ------------------------------------------------------------------------------------------------------------- tool results

def lookup_views(result: dict[str, Any]) -> list[TableView]:
    """``lookup_values``: the best rows, one table per source table, cell text as in the document."""
    by_table: dict[str, list[dict[str, Any]]] = {}
    for h in result.get("rows") or []:
        by_table.setdefault(h.get("table_id", ""), []).append(h)
    views = []
    for tid, hits in by_table.items():
        t = hits[0].get("table") or {}
        src = _source(t, tid)
        cols = _keys(h.get("values") or {} for h in hits)
        has_sim = any(isinstance(h.get("similarity"), (int, float)) for h in hits)
        rows = []
        for h in hits:
            row = [(h.get("values") or {}).get(c, "") for c in cols]
            if has_sim:
                row.append(f"{h['similarity']:.2f}" if isinstance(h.get("similarity"), (int, float)) else "")
            rows.append(row)
        views.append(_view(src.caption or tid, cols + (["similarity"] if has_sim else []), rows, src, notes=_unit_notes(hits[0].get("units"), hits[0].get("unit_notes"))))
    return views


def query_view(result: dict[str, Any]) -> Optional[TableView]:
    """``query_table``: exact filter results of one table."""
    rows = result.get("rows") or []
    src = _source(result.get("table") or {}, result.get("table_id", ""))
    cols = _keys(r.get("values") or {} for r in rows) or list(result.get("columns") or [])
    if not cols:
        return None
    return _view(src.caption or src.table_id, cols, [[(r.get("values") or {}).get(c, "") for c in cols] for r in rows], src, f"{len(rows)} row(s)",
                 _unit_notes(result.get("units"), result.get("unit_notes")))


def join_view(result: dict[str, Any]) -> Optional[TableView]:
    rows = result.get("rows") or []
    if not rows:
        return None
    keys = _keys(rows)
    cols = [("left " if k.startswith("l_") else "right ") + k[2:] if k[:2] in ("l_", "r_") else k for k in keys]
    return _view(f"{result.get('left', '')} ⨝ {result.get('right', '')}", cols, [[r.get(k, "") for k in keys] for r in rows], None, f"on {result.get('on', '')}")


def describe_views(result: dict[str, Any]) -> list[TableView]:
    """``describe_table``: the columns (type, unit, range) and the sample rows."""
    t = result.get("table") or {}
    src = _source(t)
    name = src.caption or src.table_id or "the table"
    views = [_view(f"Columns of {name}", ["column", "header", "type", "unit", "distinct", "min", "max"],
                   [[c.get("name"), c.get("header"), c.get("pg_type"), c.get("unit") or "", c.get("n_distinct", ""), c.get("min", ""), c.get("max", "")]
                    for c in t.get("columns") or []], src, notes=[str(t["notes"])] if t.get("notes") else [])]
    samples = [s for s in t.get("sample_rows") or [] if isinstance(s, dict)]
    if samples:
        keys = _keys(samples)
        views.append(_view(f"Sample rows of {name}", keys, [[s.get(k, "") for k in keys] for s in samples], src))
    return views


def search_tables_view(result: dict[str, Any]) -> Optional[TableView]:
    tabs = result.get("tables") or []
    if not tabs:
        return None
    rows = [[t.get("table_id", ""), (t.get("source") or {}).get("caption") or t.get("caption") or "", (t.get("source") or {}).get("document") or "",
             (t.get("source") or {}).get("page") or "", f"{t['similarity']:.2f}" if isinstance(t.get("similarity"), (int, float)) else ""] for t in tabs]
    return _view("Tables that match", ["table id", "caption", "document", "page", "similarity"], rows, None, f"{result.get('mode', '')} search")


def entries_view(entries: Sequence[dict[str, Any]]) -> TableView:
    rows = [[e.get("entry"), e.get("name"), e.get("version_status") or "", "yes" if e.get("has_graph") else "no", "paywalled" if e.get("paywalled") else "",
             e.get("open_warnings", 0)] for e in entries]
    return _view("Entries on the server", ["entry", "name", "version status", "graph", "access", "open warnings"], rows, None, f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")


def graph_table(rows: Any) -> str:
    """The rows of a graph ``Table`` node (a JSON string or a list of lists) as Markdown, first row = header."""
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except ValueError:
            return ""
    if not isinstance(rows, list) or not rows or not all(isinstance(r, (list, tuple)) for r in rows):
        return ""
    return rows_to_markdown(rows)


def result_notices(result: dict[str, Any]) -> list[Notice]:
    """Caveats the table service attached to its answer: paywall warnings and the copyright notice."""
    out = [Notice(level="warning", text=str(w)) for w in result.get("warnings") or []]
    if result.get("copyright_notice"):
        out.append(Notice(level="warning", text=str(result["copyright_notice"])))
    return out
