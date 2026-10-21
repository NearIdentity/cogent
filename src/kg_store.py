"""Read-only access to the knowledge graphs the update agent stages, with the meta-data an
agent needs to judge them: when each was captured, whether it is the latest edition
available or only what is on board, and which warnings / disclaimers apply.

Nothing here writes to the agent home, so the MCP server (kg_mcp.py) can run next to the
daemon: files are replaced atomically by the agent, and graphs are re-read when their
modification time changes.

Only networkx is needed (no LangGraph / Playwright / OCR imports).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from kg_models import PAYWALL_HOSTS

KINDS = {"standard": "standards", "report": "reports"}
TEXT_LIMIT = 1200           # characters of node text returned unless full text is requested

GENERAL_DISCLAIMERS = [
    "Knowledge graphs are machine-generated extractions of public documents (PDF/HTML/eCFR). Text, tables and "
    "cross-references can contain extraction errors; verify against the source document before relying on them.",
    "Entities and relations are produced by rule-based (or optionally LLM-assisted) extraction and are not "
    "exhaustive or authoritative. They are not legal, licensing or engineering advice.",
    "'Latest available' reflects what the update agent's trackers last observed, as of the capture times given; "
    "a newer edition may exist that has not been detected yet.",
]

ALERT_NOTES = {
    "no_local_copy": "This entry is paywalled and no purchased copy is on board; the graph holds at most the public store/landing page.",
    "new_edition_for_purchase": "A newer edition is on sale than the one on board; answers may rest on a superseded edition.",
    "local_copy_outdated": "The on-board copy is older than the latest known edition.",
    "local_copy_unidentified": "The edition of the on-board copy could not be read from its text.",
    "edition_not_endorsed": "The edition on board is not one that the endorsing regulatory guide accepts; check which edition applies before relying on it.",
    "store_page_changed": "The store/landing page changed since the last check; a new edition or errata may exist.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(s: Optional[str]) -> Optional[float]:
    t = _parse_ts(s)
    return None if t is None else round((_now() - t).total_seconds() / 86400, 2)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _clip(text: Any, full: bool) -> Any:
    if isinstance(text, str) and not full and len(text) > TEXT_LIMIT:
        return text[:TEXT_LIMIT] + f"… [{len(text) - TEXT_LIMIT} more characters; pass full_text=true]"
    return text


ROLE_RANK = {"regulatory_text": 0, "local_copy": 1, "document": 1, "rulemaking_notice": 2}


def doc_role(url: str) -> str:
    """What kind of source a document is: the codified regulation (eCFR), the Federal Register notice that announced it, a purchased local copy, or other."""
    u = (url or "").lower()
    if u.startswith("file://"):
        return "local_copy"
    host = u.split("/")[2] if "//" in u else ""
    if host == "ecfr.gov" or host.endswith(".ecfr.gov"):
        return "regulatory_text"
    if host == "federalregister.gov" or host.endswith(".federalregister.gov"):
        return "rulemaking_notice"
    return "document"


class NotFound(KeyError):
    pass


class Store:
    def __init__(self, home: Path, standards: Optional[Path] = None, reports: Optional[Path] = None,
                 stale_after_days: float = 3.0, paywalled_assets: str = "watermark"):
        if paywalled_assets not in ("watermark", "block", "allow"):
            raise ValueError("paywalled_assets must be watermark, block or allow")
        self.paywalled_assets = paywalled_assets
        self.home = Path(home)
        self.standards = Path(standards) if standards else None
        self.reports = Path(reports) if reports else None
        self.stale_after_days = stale_after_days
        self._cache: dict[str, tuple[float, nx.MultiDiGraph]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ config
    def _config(self) -> dict[str, dict[str, Any]]:
        """key ('standard:22') -> raw configuration item, with ``_kind``."""
        out: dict[str, dict[str, Any]] = {}
        for kind, path, field in (("standard", self.standards, "standards"), ("report", self.reports, "reports")):
            if path is None:
                continue
            for item in (_read_json(path, {}) or {}).get(field, []):
                out[f"{kind}:{item['id']}"] = {**item, "_kind": kind}
        return out

    def _entry_dir(self, key: str) -> Path:
        try:
            kind, ident = key.split(":", 1)
            return self.home / "staging" / KINDS[kind] / str(int(ident))
        except (ValueError, KeyError):
            raise NotFound(f"bad entry key {key!r}; use 'standard:<id>' or 'report:<id>'") from None

    # ------------------------------------------------------------------ graphs
    def graph(self, ref: str) -> nx.MultiDiGraph:
        """``ref`` is an entry key ('standard:2') or a document id (16 hex characters)."""
        path = self._graph_path(ref)
        if not path.exists():
            raise NotFound(f"no knowledge graph staged for {ref!r}")
        mtime = path.stat().st_mtime
        with self._lock:
            hit = self._cache.get(str(path))
            if hit and hit[0] == mtime:
                return hit[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        g = nx.node_link_graph(data, edges="edges")
        with self._lock:
            self._cache[str(path)] = (mtime, g)
        return g

    def _graph_path(self, ref: str) -> Path:
        if ref == "combined":
            return self.home / "staging" / "combined" / "kg.json"
        if ":" in ref:
            return self._entry_dir(ref) / "kg.json"
        if not ref.isalnum():
            raise NotFound(f"bad document id {ref!r}")
        return self.home / "staging" / "documents" / ref / "kg.json"

    # ----------------------------------------------------------------- alerts
    def alerts(self, entry: Optional[str] = None) -> list[dict[str, Any]]:
        al = _read_json(self.home / "state" / "alerts.json", {}) or {}
        out = [a for a in al.values() if entry is None or a.get("entry") == entry]
        return sorted(out, key=lambda a: (a.get("severity") != "warning", a.get("entry", ""), a.get("kind", "")))

    def last_run(self) -> Optional[dict[str, Any]]:
        try:
            lines = (self.home / "logs" / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
            return json.loads(lines[-1]) if lines else None
        except (OSError, ValueError):
            return None

    def run_errors(self, run: Optional[dict[str, Any]] = None) -> list[str]:
        """The error messages of the last agent run (itemised since the run record carries them)."""
        run = run if run is not None else self.last_run()
        return list(((run or {}).get("summary") or {}).get("error_messages") or [])

    def document_errors(self) -> list[dict[str, Any]]:
        """Documents whose last fetch failed, with the message and the number of consecutive failures (from their state files)."""
        out = []
        base = self.home / "staging" / "documents"
        for d in sorted(base.glob("*/state.json")) if base.is_dir() else []:
            st = _read_json(d, {}) or {}
            if st.get("last_error") and st.get("consecutive_errors", 0) > 0:
                out.append({"document_id": d.parent.name, "entries": self._doc_entries(d.parent.name), "url": st.get("url"),
                            "error": st["last_error"], "consecutive_errors": st["consecutive_errors"], "last_checked": st.get("last_checked")})
        return out

    # --------------------------------------------------------------- catalogue
    def catalog(self, kind: Optional[str] = None, query: Optional[str] = None) -> list[dict[str, Any]]:
        """One row per configured standard / report with staging and version status."""
        rows = []
        open_alerts: dict[str, int] = {}
        for a in self.alerts():
            open_alerts[a["entry"]] = open_alerts.get(a["entry"], 0) + (a.get("severity") == "warning")
        for key, item in self._config().items():
            if kind and item["_kind"] != kind:
                continue
            name = item.get("document") or item.get("report") or ""
            if query and query.lower() not in f"{key} {name} {item.get('description', '')}".lower():
                continue
            meta = _read_json(self._entry_dir(key) / "meta.json", {}) or {}
            vt = item.get("version_tracking") or {}
            rows.append({
                "entry": key, "kind": item["_kind"], "id": item["id"], "name": name,
                "group": item.get("group_title"), "has_graph": (self._entry_dir(key) / "kg.json").exists(),
                "graph_built": meta.get("built"), "nodes": meta.get("nodes"), "edges": meta.get("edges"),
                "paywalled": bool(item.get("paywalled")) or "paywalled" in item.get("format_of_data", "").lower(),
                "version_status": vt.get("status"), "open_warnings": open_alerts.get(key, 0)})
        return rows

    # ---------------------------------------------------------------- metadata
    def entry_metadata(self, key: str) -> dict[str, Any]:
        item = self._config().get(key)
        if item is None:
            raise NotFound(f"unknown entry {key!r}")
        edir = self._entry_dir(key)
        meta = _read_json(edir / "meta.json", {}) or {}
        vt = item.get("version_tracking") or {}
        docs = []
        for d in meta.get("documents", []):
            st = _read_json(self.home / "staging" / "documents" / d["docid"] / "state.json", {}) or {}
            docs.append({
                "document_id": d["docid"], "url": d["url"], "content_kind": st.get("content_kind"),
                "fetched_from": st.get("fetch_url") or d["url"],
                "first_seen": st.get("first_seen"), "last_checked": st.get("last_checked"),
                "last_changed": st.get("last_changed"), "kg_built": st.get("kg_built"),
                "kg_versions": st.get("versions"), "last_forced_reread": st.get("last_forced") or None,
                "forced_rereads": st.get("forced_runs", 0), "last_forced_result": st.get("last_forced_result") or None, "nodes": st.get("kg_nodes"), "edges": st.get("kg_edges"),
                "editions_stated_in_text": {k: v for k, v in (st.get("identities") or {}).items() if v},
                "ocr_pages": st.get("ocr_pages") or [], "consecutive_errors": st.get("consecutive_errors", 0),
                "last_error": st.get("last_error") or None, "text_sha": (st.get("text_sha") or "")[:12]})
        archive = sorted(p.name for p in (self.home / "archive" / "entries" / key.replace(":", "_")).glob("*")) \
            if (self.home / "archive" / "entries").exists() else []
        alerts = self.alerts(key)
        captured = max([d["last_checked"] for d in docs if d["last_checked"]], default=None)
        out = {
            "entry": key, "kind": item["_kind"], "name": item.get("document") or item.get("report"),
            "description": item.get("description"), "format_of_data": item.get("format_of_data"),
            "paywalled": bool(item.get("paywalled")) or "paywalled" in (item.get("format_of_data") or "").lower(),
            "source_links": item.get("links") or item.get("template_links") or [],
            "update_tracking_urls": item.get("update_tracking_urls", []),
            "graph": {"available": (edir / "kg.json").exists(), "built": meta.get("built"),
                      "nodes": meta.get("nodes"), "edges": meta.get("edges"), "missing_documents": meta.get("missing", []),
                      "superseded_versions_archived": archive},
            "capture": {"documents_last_checked": captured, "age_days": _age_days(captured),
                        "stale": (_age_days(captured) or 0) > self.stale_after_days if captured else None,
                        "agent_last_run": (self.last_run() or {}).get("finished")},
            "documents": docs,
            "version": {
                "status": vt.get("status"),
                "latest_available": vt.get("latest", []),
                "on_board": (vt.get("staged") or {}).get("editions", {}),
                "on_board_documents": (vt.get("staged") or {}).get("documents", []),
                "summary": self._version_summary(vt)},
            "warnings": alerts}
        out["disclaimers"] = self.disclaimers(out)
        return out

    @staticmethod
    def _version_summary(vt: dict[str, Any]) -> str:
        s = vt.get("status")
        latest = "; ".join(x.get("label") or f"{x.get('identity')} {x.get('edition')}" for x in vt.get("latest", [])) or "unknown"
        return {
            "current": f"On board matches the latest available ({latest}).",
            "update_available": f"A newer edition is available than the one on board (latest: {latest}).",
            "not_staged": f"Latest known: {latest}; its content is not staged yet.",
            "partially_staged": f"Only part of the latest content is on board (latest: {latest}).",
            "no_local_copy": f"Paywalled and no local copy on board (latest known: {latest}).",
            "no_documents": "No documents are configured for this entry.",
        }.get(s or "", "Version status not recorded (the agent has not run with version tracking yet).")

    def disclaimers(self, meta: Optional[dict[str, Any]] = None) -> list[str]:
        out = list(GENERAL_DISCLAIMERS)
        if not meta:
            return out
        if meta["paywalled"]:
            out.append("Paywalled source: the graph contains only the purchased copy placed on board (if any), never "
                       "content fetched behind a paywall. Redistribution may be restricted by the publisher's licence.")
        if meta["version"]["status"] in ("update_available", "not_staged", "partially_staged", "no_local_copy"):
            out.append("On-board content is NOT the latest available: " + meta["version"]["summary"])
        if meta["capture"]["stale"]:
            out.append(f"Stale capture: sources were last checked {meta['capture']['age_days']} days ago "
                       f"(threshold {self.stale_after_days:g}).")
        if any(d["ocr_pages"] for d in meta["documents"]):
            out.append("Some pages were read by OCR (scanned images); text there may contain recognition errors.")
        if any(d["last_error"] for d in meta["documents"]):
            out.append("The last fetch of at least one source failed; the graph shows the last good version.")
        if not meta["graph"]["available"]:
            out.append("No graph is staged for this entry yet.")
        for a in meta["warnings"]:
            if a["kind"] in ALERT_NOTES:
                note = ALERT_NOTES[a["kind"]]
                if note not in out:
                    out.append(note)
        return out

    def last_forced_run(self) -> Optional[dict[str, Any]]:
        """The most recent forced-update run record from logs/forced.jsonl (None if there was none)."""
        try:
            for line in reversed((self.home / "logs" / "forced.jsonl").read_text(encoding="utf-8").strip().splitlines()):
                rec = json.loads(line)
                if rec.get("kind") == "run":
                    return {"run_id": rec.get("run_id"), "ts": rec.get("ts"), "summary": rec.get("summary")}
        except (OSError, ValueError):
            pass
        return None

    def overview(self) -> dict[str, Any]:
        cat = self.catalog()
        by_status: dict[str, int] = {}
        for r in cat:
            by_status[r["version_status"] or "unknown"] = by_status.get(r["version_status"] or "unknown", 0) + 1
        run = self.last_run()
        return {"run_errors": self.run_errors(run), "document_errors": self.document_errors(), "entries": len(cat), "with_graph": sum(r["has_graph"] for r in cat),
                "version_status_counts": by_status, "open_warnings": sum(1 for a in self.alerts() if a.get("severity") == "warning"),
                "agent_last_run": (run or {}).get("finished"), "agent_last_forced_run": self.last_forced_run(), "agent_last_run_summary": (run or {}).get("summary"),
                "disclaimers": GENERAL_DISCLAIMERS}

    # -------------------------------------------------------------- graph views
    def graph_summary(self, ref: str) -> dict[str, Any]:
        g = self.graph(ref)
        types: dict[str, int] = {}
        rels: dict[str, int] = {}
        for _, a in g.nodes(data=True):
            types[a.get("type", "?")] = types.get(a.get("type", "?"), 0) + 1
        for _, _, a in g.edges(data=True):
            rels[a.get("relation", "?")] = rels.get(a.get("relation", "?"), 0) + 1
        return {"ref": ref, "nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
                "node_types": types, "relations": rels,
                "graph_attributes": {k: v for k, v in g.graph.items() if isinstance(v, (str, int, float, bool))}}

    @staticmethod
    def _nid(g: nx.MultiDiGraph, ref: str, node_id: str) -> str:
        """A node id as given, or in the other spelling the combined graph uses (``standard:3`` / ``entry:standard:3``); a miss suggests close ids."""
        if node_id in g:
            return node_id
        for alt in ("entry:" + node_id, node_id[len("entry:"):] if node_id.startswith("entry:") else None):
            if alt and alt in g:
                return alt
        import difflib
        near = difflib.get_close_matches(node_id, [str(n) for n in g.nodes], n=3, cutoff=0.6)
        raise NotFound(f"node {node_id!r} not in {ref}" + (f"; did you mean {', '.join(repr(x) for x in near)}?" if near else ""))

    def _node(self, g: nx.MultiDiGraph, nid: str, full: bool = False) -> dict[str, Any]:
        a = dict(g.nodes[nid])
        out = {"id": nid, "type": a.pop("type", None), "label": _clip(a.pop("label", None), full)}
        if "text" in a:
            t = a.pop("text")
            if t != out["label"] or full:
                out["text"] = _clip(t, full)
        if "rows" in a:
            a["rows"] = "(table; use get_table)" if not full else a["rows"]
        out.update({k: v for k, v in a.items() if isinstance(v, (str, int, float, bool, list))})
        return out

    def search(self, ref: str, query: str, node_types: Optional[list[str]] = None, limit: int = 20, match: str = "all",
               report: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Keyword search. ``match='all'``: every term must occur in the node (default); ``'any'``: at least one, ranked by how many.
        Hyphens, slashes and underscores count as spaces on both sides (``DOE-STD`` finds ``DOE STD``). ``report`` (a dict) receives the
        per-term node counts, so an empty result can say which term is missing."""
        if match not in ("all", "any"):
            raise NotFound("match must be 'all' or 'any'")
        g = self.graph(ref)
        norm = lambda t: " ".join(re.sub(r"[-_/]+", " ", t.lower()).split())
        terms = [norm(t) for t in query.split() if norm(t)]
        counts = {t: 0 for t in terms}
        scored = []
        for nid, a in g.nodes(data=True):
            if node_types and a.get("type") not in node_types:
                continue
            label = norm(str(a.get("label", "")))
            hay = f"{label} {norm(str(a.get('text', '')))}"
            present = [t for t in terms if t in hay]
            for t in present:
                counts[t] += 1
            ok = bool(terms) and (len(present) == len(terms) if match == "all" else bool(present))
            if ok:
                scored.append((sum(hay.count(t) for t in present) + (5 if all(t in label for t in terms) else 0) + 3 * len(present), nid))
        roles = self._node_roles(ref, g)
        if len({r for r in roles.values()}) > 1:                       # several kinds of source in one graph: regulation text before the notice that announced it
            scored = [(sc + (3 if roles.get(nid) == "regulatory_text" else 0), nid) for sc, nid in scored]
        scored.sort(key=lambda x: (-x[0], x[1]))
        out: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        for _, nid in scored:
            a = g.nodes[nid]
            body = norm(str(a.get("text") or a.get("label") or ""))
            if len(body) >= 40 and a.get("type") in ("Paragraph", "Heading") and body in seen:        # the same paragraph in another document of the entry
                seen[body].setdefault("also_in", []).append(nid)
                continue
            n = self._node(g, nid)
            if roles.get(nid):
                n["source_role"] = roles[nid]
            if len(body) >= 40 and a.get("type") in ("Paragraph", "Heading"):
                seen[body] = n
            out.append(n)
            if len(out) >= max(1, min(limit, 100)):
                break
        if report is not None:
            report["term_counts"] = counts
            report["duplicates_merged"] = sum(len(n.get("also_in", [])) for n in out)
        return out

    def _node_roles(self, ref: str, g: nx.MultiDiGraph) -> dict[str, str]:
        """node id -> role of its source document (only for entry graphs of several documents; node ids start with the first 8 characters of the document id)."""
        if ":" not in ref:
            return {}
        try:
            docs = self._entry_docs(ref)
        except NotFound:
            return {}
        if len(docs) < 2:
            return {}
        by_prefix = {d[:8]: doc_role((self._doc_state(d) or {}).get("url", "")) for d in docs}
        return {str(n): by_prefix[str(n).split(":", 1)[0]] for n in g.nodes if str(n).split(":", 1)[0] in by_prefix}

    def node(self, ref: str, node_id: str, full_text: bool = False) -> dict[str, Any]:
        g = self.graph(ref)
        node_id = self._nid(g, ref, node_id)
        n = self._node(g, node_id, full_text)
        n["degree"] = {"in": g.in_degree(node_id), "out": g.out_degree(node_id)}
        return n

    def neighbors(self, ref: str, node_id: str, relation: Optional[str] = None, direction: str = "both",
                  limit: int = 50) -> dict[str, Any]:
        g = self.graph(ref)
        node_id = self._nid(g, ref, node_id)
        edges = []
        if direction in ("out", "both"):
            edges += [(node_id, v, d, "out") for _, v, d in g.out_edges(node_id, data=True)]
        if direction in ("in", "both"):
            edges += [(u, node_id, d, "in") for u, _, d in g.in_edges(node_id, data=True)]
        rows = []
        for u, v, d, way in edges:
            if relation and d.get("relation") != relation:
                continue
            other = v if way == "out" else u
            rows.append({"direction": way, "relation": d.get("relation"), "node": self._node(g, other)})
        return {"node": node_id, "total": len(rows), "neighbors": rows[:max(1, min(limit, 200))]}

    def subgraph(self, ref: str, center: str, depth: int = 1, max_nodes: int = 100) -> dict[str, Any]:
        g = self.graph(ref)
        center = self._nid(g, ref, center)
        seen, frontier = {center}, [center]
        for _ in range(max(0, min(depth, 4))):
            nxt = []
            for n in frontier:
                for m in list(g.successors(n)) + list(g.predecessors(n)):
                    if m not in seen and len(seen) < max_nodes:
                        seen.add(m)
                        nxt.append(m)
            frontier = nxt
        h = g.subgraph(seen)
        return {"center": center, "truncated": len(seen) >= max_nodes,
                "nodes": [self._node(g, n) for n in h.nodes],
                "edges": [{"source": u, "target": v, "relation": d.get("relation")} for u, v, d in h.edges(data=True)]}

    def table(self, ref: str, node_id: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """Rows of a Table node, one page at a time (``offset``/``limit`` rows; ``next_offset`` is set while more remain)."""
        g = self.graph(ref)
        if node_id not in g:
            node_id = self._nid(g, ref, node_id)
        if g.nodes[node_id].get("type") != "Table":
            raise NotFound(f"{node_id!r} is not a Table node of {ref}")
        a = g.nodes[node_id]
        rows = a.get("rows")
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except ValueError:
                pass
        out = {"id": node_id, "label": a.get("label"), "page": a.get("page")}
        if isinstance(rows, list):
            offset, limit = max(0, int(offset)), max(1, min(int(limit), 2000))
            page = rows[offset:offset + limit]
            more = offset + len(page) < len(rows)
            out.update(rows=page, total_rows=len(rows), offset=offset, limit=limit, truncated=more, next_offset=offset + len(page) if more else None)
            if more:
                out["note"] = f"{len(rows) - offset - len(page)} more row(s); call again with offset={offset + len(page)} (or use get_table_csv for the exported file)"
        else:
            out["rows"] = rows
        return out

    def outline(self, ref: str, max_items: int = 200) -> list[dict[str, Any]]:
        """Headings in document order (node ids are ordered by position within each document)."""
        g = self.graph(ref)
        heads = [(n, a) for n, a in g.nodes(data=True) if a.get("type") == "Heading"]
        heads.sort(key=lambda x: (x[1].get("doc", ""), x[1].get("page") or 0, x[0]))
        out = []
        for n, a in heads[:max_items]:
            label, text = a.get("label") or "", a.get("text") or ""
            title = text if len(text) > len(label) else label                   # the node label is cut at 80 characters at ingest; the text is whole
            clipped = len(title) > 300
            out.append({"id": n, "level": a.get("level"), "page": a.get("page"), "title": title[:300], "doc": a.get("doc"),
                        **({"truncated": True} if clipped else {})})
        return out

    def outline_warnings(self, ref: str) -> list[str]:
        """Plausibility check of the heading numbers in reading order: a top-level number lower than one already seen ('3 RESPONSIBILITY' before
        '2 APPLICABILITY') suggests the page columns were read in the wrong order (or that numbering restarts, e.g. in an appendix)."""
        import kg_layout
        g = self.graph(ref)
        heads = sorted(((n, a) for n, a in g.nodes(data=True) if a.get("type") == "Heading"), key=lambda x: (x[1].get("doc", ""), x[1].get("page") or 0, x[0]))
        by_doc: dict[str, list[tuple[str, str]]] = {}
        for n, a in heads:
            by_doc.setdefault(a.get("doc", ""), []).append((n, a.get("text") or a.get("label") or ""))
        out: list[str] = []
        for doc, hs in by_doc.items():
            out += [f"{doc + ': ' if doc else ''}{p}" for p in kg_layout.numbering_problems(hs)]
        return out[:8]

    def export(self, ref: str, max_nodes: int = 2000) -> dict[str, Any]:
        g = self.graph(ref)
        keep = list(g.nodes)[:max_nodes]
        h = g.subgraph(keep)
        data = nx.node_link_data(h, edges="edges")
        data["truncated"] = g.number_of_nodes() > max_nodes
        data["total_nodes"] = g.number_of_nodes()
        return data

    # ------------------------------------------------- data dictionaries / relationship maps (files written by kg_tabledb)
    def _table_docs(self, ref: str) -> list[str]:
        if ":" in ref:
            meta = _read_json(self._entry_dir(ref) / "meta.json")
            if meta is None:
                raise NotFound(f"no staged entry {ref!r}")
            return [d["docid"] for d in meta.get("documents", [])]
        if not ref.isalnum():
            raise NotFound(f"bad document id {ref!r}")
        return [ref]

    def _published(self, ref: str, filename: str, as_text: bool) -> list[dict[str, Any]]:
        out, blocked = [], 0
        for docid in self._table_docs(ref):
            path = self.home / "tabledb" / docid / filename
            if not path.exists():
                continue
            pw = self.doc_paywall(docid)
            if pw["paywalled"] and self.paywalled_assets == "block":
                blocked += 1
                continue
            item: dict[str, Any] = {"document_id": docid, "paywalled": pw["paywalled"],
                                    "generated": (_read_json(self.home / "tabledb" / docid / "data_dictionary.json", {}) or {}).get("generated")}
            if pw["paywalled"]:
                from kg_assets import notice
                item["copyright_notice"] = notice(pw["holder"])
                item["warnings"] = self._asset_warnings(docid, pw)
            item["content"] = path.read_text(encoding="utf-8") if as_text else _read_json(path)
            out.append(item)
        if not out:
            why = (f"{blocked} document(s) are paywalled and the server is configured not to serve them" if blocked
                   else "none published yet: run the table database agent (`python src/kg_tabledb.py build`, or `kg_agent.py run --tables`)")
            raise NotFound(f"no data dictionary / relationship map for {ref!r}: {why}")
        return out

    def data_dictionary(self, ref: str, fmt: str = "json") -> dict[str, Any]:
        """The published data dictionary of the documents of ``ref`` (an entry key or a document id): tables, columns, types, units, samples, joins."""
        if fmt not in ("json", "markdown"):
            raise NotFound("format must be 'json' or 'markdown'")
        return {"ref": ref, "format": fmt, "documents": self._published(ref, "data_dictionary.md" if fmt == "markdown" else "data_dictionary.json", fmt == "markdown")}

    def relationship_map(self, ref: str, fmt: str = "json") -> dict[str, Any]:
        """The inferred joins between the tables of each document, as JSON (with scores and evidence) or a Mermaid ER diagram."""
        if fmt not in ("json", "mermaid"):
            raise NotFound("format must be 'json' or 'mermaid'")
        return {"ref": ref, "format": fmt, "documents": self._published(ref, "relationships.mmd" if fmt == "mermaid" else "relationships.json", fmt == "mermaid")}

    # -------------------------------------------------------- combined graph
    def combined_meta(self) -> dict[str, Any]:
        meta = _read_json(self.home / "staging" / "combined" / "meta.json")
        if meta is None:
            raise NotFound("no combined graph yet (it is built by the agent's run)")
        return {"built": meta.get("built"), "run_id": meta.get("run_id"), "age_days": _age_days(meta.get("built")),
                "nodes": meta.get("nodes"), "edges": meta.get("edges"), "entries_without_graph": meta.get("entries_without_graph", []),
                "derived": True,
                "notes": ["Derived from the per-entry graphs and the configuration; rebuilt by every agent run.",
                          "'declared' relations come from the configuration (curated). 'inferred' ones come from text matching "
                          "and may be wrong or incomplete.",
                          "Contains entry-level relations only, no document text."]}

    def dependents(self, entry: str) -> dict[str, Any]:
        dep = _read_json(self.home / "staging" / "combined" / "dependents.json", {}) or {}
        if entry not in dep:
            if not self.combined_meta_exists():
                raise NotFound("no combined graph yet (it is built by the agent's run)")
            raise NotFound(f"unknown entry {entry!r}")
        cfg = self._config()
        vt = lambda k: ((cfg.get(k) or {}).get("version_tracking") or {}).get("status")
        names = lambda k: (cfg.get(k) or {}).get("document") or (cfg.get(k) or {}).get("report")
        enrich = lambda keys: [{"entry": k, "name": names(k), "version_status": vt(k)} for k in keys]
        d = dep[entry]
        aff = d["affects_reports"]
        return {"entry": entry, "name": names(entry),
                "depends_on": enrich(d["depends_on"]),
                "declared_dependents": enrich(d["declared_dependents"]),
                "overlaps_with": enrich(d["overlaps"]),
                "part_of": enrich(d.get("part_of", [])),
                "inferred_cited_by": [{**c, "name": names(c["entry"])} for c in d["cited_by"]],
                "inferred_cites": [{**c, "name": names(c["entry"])} for c in d["cites"]],
                "if_this_changes_reports_affected": {"declared": enrich(aff["declared"]), "via_overlapping_standard": enrich(aff["via_overlap"]),
                                                     "inferred_from_text": enrich(aff["inferred"])},
                "combined": self.combined_meta()}

    def combined_meta_exists(self) -> bool:
        return (self.home / "staging" / "combined" / "meta.json").exists()

    # ------------------------------------------------------ figures / tables
    def _doc_state(self, docid: str) -> dict[str, Any]:
        if not docid.isalnum():
            raise NotFound(f"bad document id {docid!r}")
        return _read_json(self.home / "staging" / "documents" / docid / "state.json", {}) or {}

    def resolve_docid(self, token: str) -> str:
        """A full 16-hex document id, or a unique prefix of one (graph node ids show only 8 characters, like ``57988ced:tbl0224``)."""
        token = (token or "").strip().lower()
        base = self.home / "staging" / "documents"
        if (base / token).is_dir():
            return token
        if len(token) >= 6 and all(c in "0123456789abcdef" for c in token) and base.is_dir():
            hits = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith(token))
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise NotFound(f"document id prefix {token!r} is ambiguous: {', '.join(hits[:5])}")
        raise NotFound(f"unknown document id {token!r} (use the 16-character id of the document, e.g. from get_entry_metadata)")

    def _entry_docs(self, ref: str) -> list[str]:
        if ":" not in ref:
            return [self.resolve_docid(ref)]
        meta = _read_json(self._entry_dir(ref) / "meta.json")
        if meta is None:
            raise NotFound(f"no staged entry {ref!r}")
        return [d["docid"] for d in meta.get("documents", [])]

    def _doc_entries(self, docid: str) -> list[str]:
        out = []
        for key in self._config():
            try:
                if docid in self._entry_docs(key):
                    out.append(key)
            except NotFound:
                pass
        return out

    def doc_paywall(self, docid: str) -> dict[str, Any]:
        """Is this document's content copyrighted / paywalled material? (a purchased local copy, or a publisher's store host)"""
        st = self._doc_state(docid)
        url = st.get("url", "")
        host = (url.split("/")[2].lower() if "//" in url and not url.startswith("file://") else "")
        local = url.startswith("file://")
        store = bool(host) and any(host == x or host.endswith("." + x) for x in PAYWALL_HOSTS)
        entries = self._doc_entries(docid)
        cfg = self._config()
        names = [cfg[k].get("document") or cfg[k].get("report") for k in entries if k in cfg]
        return {"paywalled": local or store, "reason": "purchased local copy" if local else "publisher store host" if store else None,
                "entries": entries, "holder": names[0] if names else None, "url": url if not local else "local:" + url.rsplit("/", 1)[-1]}

    def _asset_warnings(self, docid: str, pw: dict[str, Any]) -> list[str]:
        out: list[str] = []
        if pw["paywalled"]:
            from kg_assets import notice
            out.append(notice(pw["holder"]))
            out.append(f"Paywalled source ({pw['reason']}). Server policy for such assets: {self.paywalled_assets}.")
        for k in pw["entries"]:
            out += [a["message"] for a in self.alerts(k)
                    if a.get("kind") in ("no_local_copy", "new_edition_for_purchase", "local_copy_outdated", "edition_not_endorsed")]
        return list(dict.fromkeys(out))

    def _manifest(self, docid: str) -> tuple[Path, dict[str, Any]]:
        base = self.home / "staging" / "documents" / docid / "assets"
        cands = sorted(base.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            raise NotFound(f"no exported figures/tables for document {docid}")
        return cands[0].parent, _read_json(cands[0], {}) or {}

    def list_assets(self, ref: str, kind: Optional[str] = None) -> dict[str, Any]:
        docs = []
        for docid in self._entry_docs(ref):
            pw = self.doc_paywall(docid)
            try:
                _, man = self._manifest(docid)
            except NotFound:
                continue
            items = [{"asset_id": f"{docid}/{a['id']}", "kind": a["kind"], "page": a.get("page"), "caption": a.get("caption"),
                      **{k: a[k] for k in ("rows", "cols", "bytes") if k in a},
                      "available": not (pw["paywalled"] and self.paywalled_assets == "block")}
                     for a in man.get("assets", []) if not kind or a["kind"] == kind]
            docs.append({"document_id": docid, "source": pw["url"], "paywalled": pw["paywalled"],
                         "warnings": self._asset_warnings(docid, pw), "assets": items})
        if not docs:
            raise NotFound(f"no exported figures or tables for {ref!r} (the agent exports them when run with assets enabled)")
        return {"ref": ref, "paywalled_assets_policy": self.paywalled_assets, "documents": docs}

    def _asset(self, ref: str, asset_id: str, kind: str) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
        docid, _, aid = asset_id.partition("/")
        if not aid:                                                    # a graph Table/Figure node id: '<docid prefix>:<id>'
            docid, _, aid = asset_id.partition(":")
        if not aid or not docid.isalnum() or not aid.replace("_", "").replace("-", "").isalnum():
            raise NotFound(f"bad asset id {asset_id!r}; use '<document_id>/<id>' from list_assets (or a graph node id such as '57988ced:tbl0224')")
        docid = self.resolve_docid(docid)
        if docid not in self._entry_docs(ref):
            owners = self._doc_entries(docid)
            raise NotFound(f"document {docid} is not part of {ref!r}" + (f"; it belongs to {', '.join(owners)}" if owners else ""))
        root, man = self._manifest(docid)
        item = next((a for a in man.get("assets", []) if a["id"] == aid and a["kind"] == kind), None)
        if item is None:
            raise NotFound(f"no {kind} {asset_id!r}")
        path = (root / item["file"]).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise NotFound(f"asset file for {asset_id!r} is missing")
        pw = self.doc_paywall(docid)
        if pw["paywalled"] and self.paywalled_assets == "block":
            raise NotFound(f"{kind} {asset_id!r} comes from paywalled content and the server is configured not to serve it")
        return path, item, pw, docid

    def figure(self, ref: str, asset_id: str, max_px: int = 1600) -> dict[str, Any]:
        """A figure as PNG bytes; watermarked and bannered when it comes from paywalled content."""
        import kg_assets as A
        path, item, pw, docid = self._asset(ref, asset_id, "figure")
        try:
            png, (w, h) = A.to_png(path.read_bytes(), max(200, min(max_px, 4000)))
        except Exception:
            raise NotFound(f"figure {asset_id!r} is stored as {path.suffix or 'an unknown format'} and cannot be converted to PNG") from None
        marked = pw["paywalled"] and self.paywalled_assets != "allow"
        if marked:
            png = A.watermark(png, A.notice(pw["holder"]))
        png = A.fit_bytes(png)
        return {"png": png, "info": {"asset_id": asset_id, "kind": "figure", "page": item.get("page"), "caption": item.get("caption"),
                                     "width": w, "height": h, "watermarked": marked, "paywalled": pw["paywalled"],
                                     "warnings": self._asset_warnings(docid, pw)}}

    def table_csv(self, ref: str, asset_id: str, max_rows: int = 500) -> dict[str, Any]:
        path, item, pw, docid = self._asset(ref, asset_id, "table")
        lines = path.read_text(encoding="utf-8").splitlines()
        total = max(0, len(lines))
        return {"asset_id": asset_id, "kind": "table", "page": item.get("page"), "caption": item.get("caption"),
                "rows_total": total, "truncated": total > max_rows, "csv": "\n".join(lines[:max_rows]),
                "paywalled": pw["paywalled"], "warnings": self._asset_warnings(docid, pw)}
