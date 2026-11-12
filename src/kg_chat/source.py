"""The knowledge the chat agent is grounded in: everything comes through the MCP connection.

* ``graph_sections``   turns an entry's knowledge graph (``export_graph``) into Markdown sections with citation metadata (the counterpart of
                       0xA6E07's staged ``data/<host>/*.md`` files with a ``source:`` header);
* ``KnowledgeSource``  lists the entries, builds (and caches) the sections, runs the live table lookup (``lookup_values``) and reads the
                       provenance caveats of an entry (``get_entry_metadata``).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.documents import Document

from . import render
from .cache import SectionCache
from .mcp_conn import McpAuthError, McpConnection, McpError, McpToolError
from .models import ClientConfig, Notice, SourceRef, TableView

log = logging.getLogger("kg_chat.source")
SCHEMA_VERSION = 2
_LOCAL = re.compile(r"^(?:(?P<tag>[^:]+):)?(?P<kind>h|p|tbl|fig)(?P<n>\d+)$")
TEXT_TYPES = ("Heading", "Paragraph", "Table", "Figure")


def public_url(url: str) -> str:
    """A purchased local copy is served as ``file:///private/path``: show only its file name, never the path."""
    return "local:" + url.rsplit("/", 1)[-1] if url.startswith("file://") else url


def _page(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def graph_sections(entry: dict[str, Any], graph: dict[str, Any]) -> list[Document]:
    """Markdown sections (one per heading) of an entry graph in reading order. The metadata of each carries what a citation needs."""
    nodes = graph.get("nodes") or []
    sources = {s.get("tag"): s.get("source", "") for s in (graph.get("graph") or {}).get("sources", []) if isinstance(s, dict)}
    doc_names = {n["id"].split(":", 1)[0]: n.get("label", "") for n in nodes if n.get("type") == "Document" and ":" in n["id"]}
    order: dict[str, int] = {}
    items = []
    for n in nodes:
        if n.get("type") not in TEXT_TYPES:
            continue
        m = _LOCAL.match(str(n.get("id", "")))
        if not m:
            continue
        tag = m.group("tag") or ""
        order.setdefault(tag, len(order))
        items.append((order[tag], int(m.group("n")), tag, n))
    items.sort(key=lambda x: (x[0], x[1]))

    out: list[Document] = []
    stack: list[tuple[int, str]] = []
    cur: dict[str, Any] = {}

    def flush() -> None:
        body = "\n\n".join(cur.get("lines", []))
        if cur and cur.get("body"):                       # a heading with nothing under it is not a section (its title is in the next section's path)
            out.append(Document(page_content=body, metadata={
                "kind": "text", "entry": entry.get("entry", ""), "entry_name": entry.get("name", ""), "document": doc_names.get(cur["tag"], ""),
                "url": public_url(sources.get(cur["tag"], "")), "section": " > ".join(t for _, t in cur["path"]), "page": cur.get("page"),
                "paywalled": bool(entry.get("paywalled")), "node": cur.get("first", "")}))

    def start(tag: str, path: list[tuple[int, str]], first: str) -> dict[str, Any]:
        return {"tag": tag, "path": list(path), "lines": [], "page": None, "first": first}

    last_tag: Optional[str] = None
    for _, _, tag, n in items:
        if tag != last_tag:
            flush()
            stack, cur, last_tag = [], start(tag, [], n["id"]), tag
        kind, text = n["type"], str(n.get("text") or n.get("label") or "").strip()
        if kind == "Heading":
            flush()
            level = _page(n.get("level")) or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            cur = start(tag, stack, n["id"])
            cur["lines"].append("#" * min(level, 6) + " " + text)
            cur["page"] = _page(n.get("page"))
            continue
        if cur.get("page") is None:
            cur["page"] = _page(n.get("page"))
        if kind == "Paragraph":
            if text:
                cur["lines"].append(text)
                cur["body"] = True
        elif kind == "Table":
            table = render.graph_table(n.get("rows"))
            cap = f"**{render.md_inline(n.get('label') or text)}**" if (n.get("label") or text) else ""
            cur["lines"].append("\n\n".join(x for x in (cap, table) if x))
            cur["body"] = True
        elif kind == "Figure" and text:
            cur["lines"].append(f"[Figure: {text}]")
            cur["body"] = True
    flush()
    return out


@dataclass
class LookupOutcome:
    tables: list[TableView] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    no_match: str = ""                      # the server's message when no table row mentions what was asked
    error: str = ""                         # the table service was not usable (database down, ...)
    rows: int = 0


class KnowledgeSource:
    def __init__(self, conn: McpConnection, cfg: ClientConfig, clock: Callable[[], float] = time.monotonic):
        self.conn, self.cfg, self.clock = conn, cfg, clock
        self.cache = SectionCache(cfg.cache_dir)
        self._tools: Optional[set[str]] = None
        self._general: Optional[list[str]] = None
        self._notices: dict[str, tuple[float, list[Notice]]] = {}
        self.problems: list[str] = []

    # ---- server facts
    @property
    def tools(self) -> set[str]:
        if self._tools is None:
            self._tools = self.conn.list_tools()
        return self._tools

    @property
    def has_tables(self) -> bool:
        return "lookup_values" in self.tools

    def overview(self) -> dict[str, Any]:
        return self.conn.call_tool("get_overview")

    def general_disclaimers(self) -> list[str]:
        if self._general is None:
            try:
                self._general = list(self.conn.call_tool("get_disclaimers").get("disclaimers") or [])
            except McpError:
                self._general = []
        return self._general

    def entries(self) -> list[dict[str, Any]]:
        rows = self.conn.call_tool("list_entries").get("entries") or []
        want = set(self.cfg.entries)
        return [r for r in rows if not want or r.get("entry") in want]

    # ---- documents
    def _signature(self, entry: dict[str, Any]) -> str:
        c = self.cfg
        return f"{SCHEMA_VERSION}|{entry.get('graph_built')}|{c.max_nodes}"

    def load_sections(self, progress: Callable[[str], None] = lambda s: None) -> list[Document]:
        """Sections of every entry that has a graph. An entry whose graph is unchanged since the cached copy is not downloaded again."""
        docs: list[Document] = []
        slow = self.conn.with_timeout(max(self.cfg.timeout, 300.0))
        for e in self.entries():
            key = e["entry"]
            if not e.get("has_graph"):
                self.problems.append(f"{key}: no graph on the server")
                continue
            sig = self._signature(e)
            cached = self.cache.load(key, sig)
            if cached is not None:
                progress(f"  {key}: {len(cached)} section(s) (cached)")
                docs += cached
                continue
            try:
                res = slow.call_tool("export_graph", {"ref": key, "max_nodes": self.cfg.max_nodes})
            except McpToolError as exc:
                self.problems.append(f"{key}: {exc}")
                progress(f"  {key}: skipped ({exc})")
                continue
            g = res.get("graph") or {}
            if g.get("truncated"):
                self.problems.append(f"{key}: graph truncated at {self.cfg.max_nodes} of {g.get('total_nodes')} nodes (raise max_nodes)")
            secs = graph_sections(e, g)
            self.cache.save(key, sig, secs)
            progress(f"  {key}: {len(secs)} section(s)")
            docs += secs
        return docs

    # ---- live table lookup
    def lookup(self, question: str) -> LookupOutcome:
        """``lookup_values`` for the question: the table rows as Markdown tables (verbatim cell text), or why there are none. Never raises."""
        if not self.has_tables:
            return LookupOutcome()
        try:
            res = self.conn.call_tool("lookup_values", {"question": question, "k": self.cfg.table_rows, "retrieval": self.cfg.table_retrieval})
        except McpAuthError:
            raise                                               # a rejected token is not a degraded answer: the caller must see it
        except McpToolError as exc:
            log.info("table lookup unavailable: %s", exc)
            return LookupOutcome(error=str(exc))
        except McpError as exc:
            log.warning("table lookup failed: %s", exc)
            return LookupOutcome(error=str(exc))
        out = LookupOutcome(notices=render.result_notices(res), rows=len(res.get("rows") or []))
        if res.get("no_confident_match"):
            out.no_match = str(res.get("message") or "no table row mentions what was asked")
            out.notices = []                                    # nothing from the tables is shown, so their caveats do not apply
            return out
        out.tables = render.lookup_views(res)
        return out

    # ---- provenance caveats
    def notices_for(self, entry: str) -> list[Notice]:
        """Caveats of an entry for display: not the latest edition, stale capture, open warnings, and the entry-specific disclaimers
        (paywall, OCR, failed fetch). Read from ``get_entry_metadata``; reused for ``notice_ttl`` seconds."""
        hit = self._notices.get(entry)
        if hit and self.clock() - hit[0] < self.cfg.notice_ttl:
            return hit[1]
        try:
            m = self.conn.call_tool("get_entry_metadata", {"entry": entry})
        except McpAuthError:
            raise
        except McpError as exc:
            log.warning("no provenance for %s: %s", entry, exc)
            return [Notice(level="warning", text=f"Could not read the provenance of {entry} from the server ({exc}); check the source document.", entry=entry)]
        name = m.get("name") or entry
        out: list[Notice] = []
        v = m.get("version") or {}
        if v.get("status") and v["status"] != "current":
            out.append(Notice(level="warning", text=f"{name}: {v.get('summary') or v['status']}", entry=entry))
        for w in m.get("warnings") or []:
            out.append(Notice(level="warning", text=str(w.get("message") if isinstance(w, dict) else w), entry=entry))
        general = set(self.general_disclaimers())
        seen = {n.text for n in out}
        for d in m.get("disclaimers") or []:
            if d not in general and not d.startswith("On-board content is NOT the latest") and d not in seen:
                out.append(Notice(level="info", text=str(d), entry=entry))
        self._notices[entry] = (self.clock(), out)
        return out

    def source_ref(self, doc: Document) -> SourceRef:
        md = doc.metadata
        return SourceRef(kind="text", entry=md.get("entry", ""), entry_name=md.get("entry_name", ""), document=md.get("document", ""),
                         section=md.get("section", ""), page=md.get("page"), url=md.get("url", ""))
