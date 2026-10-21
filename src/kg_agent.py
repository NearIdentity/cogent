"""KG update agent: keeps one knowledge graph per standard / report format up to date.

Daily run (``python kg_agent.py run`` or ``daemon --at 06:00``):

    init -> track -> apply -> sync_docs -> assemble -> finalize        (LangGraph)

* init       lock, load + validate (Pydantic) the two JSON configurations, log manual edits
* track      check every ``update_tracking_urls`` page with a hard-coded tracker
             (eCFR version history, Federal Register API, identifier watcher for index pages)
* apply      turn high-confidence findings into configuration edits (new edition ->
             new link / identifier text), audited in ``config/changes.jsonl``
* sync_docs  conditional-GET every document link (ETag / Last-Modified / content
             fingerprint); changed documents are re-ingested into a document KG, the
             previous version is archived, a structured diff is computed
* assemble   rebuild the KG of each standard/report whose documents changed
* finalize   persist state and APPEND a section to ``logs/report.md``

Free text is only touched by the KG builder (and only an optional local Ollama model
is used there, or to narrate the already-structured run results). Layout of ``--home``::

    config/history/*.json  config/changes.jsonl      configuration snapshots + audit log
    staging/documents/<id>/{state.json,kg.json,assets/}   one per unique document URL
    staging/{standards,reports}/<n>/{kg.json,meta.json}   the KG of each entry
    archive/documents/<id>/<ts>_<sha>/  archive/entries/<key>/<ts>/   superseded KGs
    state/*.json   logs/report.md   logs/runs.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import logging
import operator
import os
import shutil
import tarfile
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict
from urllib.parse import parse_qs, unquote, urlparse

import networkx as nx
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

import kg_combine
import kg_ingest
import kg_tools as T
from kg_models import (Alert, ConfigChange, VersionTracking, DocAction, DocEntry, DocState, EntryAction, Finding,
                       RunNarrative, TrackerState, utcnow)

log = logging.getLogger("kg_agent")


# --------------------------------------------------------------------------
# Settings / paths / small helpers
# --------------------------------------------------------------------------


class Settings(BaseModel):
    home: Path = Path("kg_agent_home")
    standards: Path = Path("config/standards.json")
    reports: Path = Path("config/reports.json")
    extractor: str = "rules"            # rules | ollama | hybrid  (KG triple extraction)
    model: Optional[str] = None         # Ollama model for extraction
    ollama_host: Optional[str] = None
    llm: Optional[str] = None           # Ollama model used only to narrate the run report
    min_interval: float = 1.0           # seconds between requests to the same host
    auto_apply: bool = True             # apply high-confidence config updates
    min_confidence: float = 0.8
    dry_run: bool = False
    assets: bool = True                 # export figures (PNG) / tables (CSV) of each document
    only: list[str] = Field(default_factory=list)   # restrict to entries, e.g. ["standard:22"]
    keep_archive: int = 0               # versions kept per document (0 = keep all)
    stale_after: int = 3                # consecutive failures before a URL is flagged stale
    local_dir: Optional[Path] = None    # purchased/local copies of paywalled documents (default <home>/local)
    tables: bool = False                # after staging: build / refresh the table database (PostgreSQL + pgvector), see kg_tabledb.py
    tabledb_dsn: Optional[str] = None   # default $COGENT_TABLEDB_DSN
    tabledb_schema: str = "kgtables"
    embedder: str = "ollama"            # ollama | hash  (embeddings of table rows)
    embed_model: Optional[str] = None   # default nomic-embed-text
    kg_context: bool = True             # use the KG neighbourhood of a table in the embedded text
    row_mode: str = "full"              # full: embed every cell of a row | text: leave numeric values out
    llm_joins: bool = False             # also infer joins with an Ollama LLM and judge all candidates with an LLM (needs --tables)
    join_model: Optional[str] = None    # Ollama chat model of the join suggester (default llama3.1:8b)
    join_judge_model: Optional[str] = None
    join_samples: int = 3               # independent suggestion runs per table pair
    join_judge_runs: int = 3            # judge votes per candidate
    join_max_pairs: int = 40
    join_max_seconds: Optional[float] = None
    prune: bool = True                  # with force: also purge stale asset files and archive (compressed) orphaned staging data
    force: bool = False                 # forced update: re-read every source directly (no conditional requests,
                                        # no "unchanged" short-cut), rebuild the KGs, and log it all


class Paths:
    def __init__(self, home: Path, local: Optional[Path] = None):
        self.home = Path(home)
        self.local = Path(local) if local else self.home / "local"
        self.config = self.home / "config"
        self.history = self.config / "history"
        self.changes = self.config / "changes.jsonl"
        self.docs = self.home / "staging" / "documents"
        self.state = self.home / "state"
        self.archive = self.home / "archive"
        self.logs = self.home / "logs"
        self.report = self.logs / "report.md"
        self.runs = self.logs / "runs.jsonl"
        self.forced = self.logs / "forced.jsonl"       # one line per source re-read by a forced update
        self.orphans = self.state / "orphans.json"     # tracking of staged data no longer referenced by the configuration
        self.orphan_archive = self.archive / "orphans"  # compressed (tar.gz) copies of orphaned staging data
        self.lock = self.home / ".lock"

    def doc(self, docid: str) -> Path:
        return self.docs / docid

    def entry(self, e: DocEntry) -> Path:
        return self.home / "staging" / f"{e.kind}s" / str(e.id)


def docid_of(url: str) -> str:
    return kg_ingest.source_hash(url)[:16]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Runtime:
    """Everything the graph nodes share (not part of the LangGraph state)."""

    def __init__(self, s: Settings, llm: Any = None):
        self.s, self.p, self.llm = s, Paths(s.home, s.local_dir), llm
        self.dry = s.dry_run
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.started = utcnow()
        self.raw: dict[str, dict] = {}            # "standards"/"reports" -> raw JSON
        self.entries: list[DocEntry] = []
        self.tracker_state: dict[str, TrackerState] = {}
        self.ident_state: dict[str, dict[str, dict]] = {}
        self.pending: list[dict] = []
        self.alerts: dict[str, dict] = {}         # open alerts from earlier runs
        self.ctx = T.TrackerContext(min_interval=s.min_interval, ident_state=self.ident_state, force=s.force)
        self.ecfr_dates: dict[str, Optional[str]] = {}
        self.config_errors: list[str] = []
        self.dependents: dict[str, Any] = {}      # from the combined graph (kg_combine)

    # -- persistence helpers (no-ops in dry-run) --------------------------------
    def write(self, path: Path, text: str) -> None:
        if self.dry:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def write_json(self, path: Path, obj: Any) -> None:
        self.write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")

    def append(self, path: Path, text: str) -> None:
        if self.dry:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def save_graph(self, g: nx.MultiDiGraph, path: Path) -> None:
        if self.dry:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.stem}.tmp.json")
        kg_ingest.export_graph(g, str(tmp))
        os.replace(tmp, path)

    def selected(self, e: DocEntry) -> bool:
        return not self.s.only or e.key in self.s.only


def load_graph_file(path: Path) -> Optional[nx.MultiDiGraph]:
    data = read_json(path)
    return nx.node_link_graph(data, edges="edges") if data else None


LOCAL_SUFFIXES = (".pdf", ".html", ".htm", ".xml")


def local_files(rt: Runtime, e: DocEntry) -> list[Path]:
    """Local copies of a (paywalled) document: ``<local>/<kind>s/<id>/`` or the
    file / directory / glob named by the entry's ``local_copy`` field."""
    if e.local_copy:
        pat = Path(e.local_copy).expanduser()
        cands = (sorted(pat.parent.glob(pat.name)) if any(c in pat.name for c in "*?[") else
                 sorted(pat.iterdir()) if pat.is_dir() else [pat])
    else:
        d = rt.p.local / f"{e.kind}s" / str(e.id)
        cands = sorted(d.iterdir()) if d.is_dir() else []
    return [f for f in cands if f.is_file() and f.suffix.lower() in LOCAL_SUFFIXES and not f.name.startswith(".")]


def entry_sources(rt: Runtime, e: DocEntry) -> list[tuple[str, str]]:
    """(url, label) of everything that feeds an entry's KG: its links plus local copies."""
    out = [(l.url, l.text or e.name) for l in e.links]
    out += [(f"file://{f.resolve()}", f"{e.name} (local copy: {f.name})") for f in local_files(rt, e)]
    return out


def doc_identities(g: nx.MultiDiGraph, label: str) -> dict[str, Optional[str]]:
    """Identifiers (with edition) that a document states about itself: its
    name plus the first headings/paragraphs (title page)."""
    def num(n: str) -> int:
        digits = "".join(c for c in n.split(":")[-1] if c.isdigit())
        return int(digits) if digits else 0
    first = sorted((n for n, d in g.nodes(data=True) if d.get("type") in ("Heading", "Paragraph")), key=num)[:15]
    texts = [label] + [g.nodes[n].get("text") or g.nodes[n].get("label", "") for n in first]
    return {i.label: i.edition for i in T.parse_identities(*texts)}


# --------------------------------------------------------------------------
# Configuration store (raw JSON is the source of truth; Pydantic validates)
# --------------------------------------------------------------------------


def parse_entries(raw: dict[str, dict]) -> tuple[list[DocEntry], list[str]]:
    entries, errors = [], []
    for fname, kind, field in (("standards", "standard", "standards"), ("reports", "report", "reports")):
        for item in raw.get(fname, {}).get(field, []):
            try:
                entries.append(DocEntry.from_raw(kind, item))  # type: ignore[arg-type]
            except Exception as exc:
                errors.append(f"{fname} id={item.get('id')}: {exc}")
    return entries, errors


def raw_item(rt: Runtime, key: str) -> tuple[str, dict]:
    kind, ident = key.split(":")
    fname = "standards" if kind == "standard" else "reports"
    for item in rt.raw[fname][fname]:
        if str(item["id"]) == ident:
            return fname, item
    raise KeyError(key)


def _links_field(key: str) -> str:
    return "links" if key.startswith("standard") else "template_links"


def _name_field(key: str) -> str:
    return "document" if key.startswith("standard") else "report"


# --------------------------------------------------------------------------
# LangGraph state
# --------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    notes: Annotated[list[str], operator.add]
    config_changes: Annotated[list[dict], operator.add]
    findings: Annotated[list[dict], operator.add]
    tracker_log: Annotated[list[dict], operator.add]
    doc_actions: Annotated[list[dict], operator.add]
    entry_actions: Annotated[list[dict], operator.add]
    errors: Annotated[list[str], operator.add]
    alerts: list[dict]
    versions: list[dict]
    forced: bool
    forced_warnings: Annotated[list[str], operator.add]
    orphans: list[dict]
    report: str
    summary: dict


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def make_nodes(rt: Runtime):
    s, p = rt.s, rt.p

    # 1 ---------------------------------------------------------------- init
    def init(state: AgentState) -> AgentState:
        out: AgentState = {"notes": [], "config_changes": [], "errors": [], "forced": s.force}
        if s.force:
            out["notes"].append("FORCED UPDATE: every tracking page and source document is re-read directly (no conditional requests); document and entry KGs are rebuilt even if nothing changed")
        if not rt.dry:
            p.home.mkdir(parents=True, exist_ok=True)
            if p.lock.exists():
                try:
                    pid = int(p.lock.read_text().strip())
                    os.kill(pid, 0)
                    raise RuntimeError(f"another run is active (pid {pid}); remove {p.lock} if stale")
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
            p.lock.write_text(str(os.getpid()))
        for name, path in (("standards", s.standards), ("reports", s.reports)):
            rt.raw[name] = json.loads(Path(path).read_text(encoding="utf-8"))
        rt.entries, errs = parse_entries(rt.raw)
        rt.config_errors = errs
        out["errors"] += [f"config validation: {e}" for e in errs]
        if s.force and s.only:
            out_of_scope = [e.key for e in rt.entries if not rt.selected(e)]
            out["forced_warnings"] = [f"scope limited by --only ({', '.join(s.only)}): {len(out_of_scope)} other entr(ies) and their "
                                      "documents were NOT refreshed; orphan archiving was skipped"]

        # detect manual edits since the previous run
        hashes = read_json(p.state / "config_hashes.json", {})
        for name, path in (("standards", s.standards), ("reports", s.reports)):
            cur_text = Path(path).read_text(encoding="utf-8")
            sha = T._sha(cur_text)
            last = p.history / f"{name}.last.json"
            if name in hashes and hashes[name] != sha and last.exists():
                for d in T.json_diff(json.loads(last.read_text()), json.loads(cur_text)):
                    entry = _entry_of_path(d["path"], name)
                    ch = ConfigChange(run_id=rt.run_id, file=name, entry=entry, path=d["path"],
                                      old=d["old"], new=d["new"], reason="edited outside the agent",
                                      source="external", auto=False)
                    out["config_changes"].append(ch.model_dump())
                    rt.append(p.changes, ch.model_dump_json() + "\n")
                rt.write(p.history / f"{rt.run_id}_{name}.json", cur_text)
            elif name not in hashes:
                out["notes"].append(f"{name}: first run, configuration snapshot recorded")
                rt.write(p.history / f"{rt.run_id}_{name}.json", cur_text)
            hashes[name] = sha
            rt.write(last, cur_text)
        rt.write_json(p.state / "config_hashes.json", hashes)

        rt.tracker_state = {u: TrackerState(**d) for u, d in read_json(p.state / "trackers.json", {}).items()}
        rt.ident_state.update(read_json(p.state / "identities.json", {}))
        rt.pending = read_json(p.state / "pending.json", [])
        rt.alerts = read_json(p.state / "alerts.json", {})
        if not rt.dry:                       # show the user where purchased copies go
            for e in rt.entries:
                if e.paywalled and not e.local_copy:
                    (p.local / f"{e.kind}s" / str(e.id)).mkdir(parents=True, exist_ok=True)
        return out

    # 2 --------------------------------------------------------------- track
    def track(state: AgentState) -> AgentState:
        watchers: dict[str, list[DocEntry]] = {}
        for e in rt.entries:
            if rt.selected(e):
                for u in e.tracking_urls:
                    watchers.setdefault(u, []).append(e)
        out: AgentState = {"findings": [], "tracker_log": [], "errors": []}
        for url, ents in watchers.items():
            tr = T.tracker_for(url)
            try:
                res = tr.check(url, rt.tracker_state.get(url), ents, rt.ctx)
            except Exception as exc:                      # a broken tracker must not stop the run
                log.exception("tracker %s failed on %s", tr.name, url)
                res = tr._error(url, rt.tracker_state.get(url), f"{type(exc).__name__}: {exc}")
            if s.force:
                res.state.last_forced, res.state.forced_runs = utcnow(), res.state.forced_runs + 1
            rt.tracker_state[url] = res.state
            out["tracker_log"].append({"url": url, "tracker": tr.name, "changed": res.changed,
                                       "summary": res.summary, "error": res.error,
                                       "watchers": [e.key for e in ents], "forced": s.force,
                                       "findings": [f"{f.type}:{f.identity}" + (f" {f.old}->{f.new}" if f.new else "") for f in res.findings if f.type != "baseline"],
                                       "stale": res.state.consecutive_errors >= s.stale_after})
            if s.force:
                _log_forced(rt, {"kind": "tracker", "url": url, "tracker": tr.name, "entries": [e.key for e in ents],
                                 "result": "error" if res.error else "changed" if res.changed else "unchanged",
                                 "detail": res.error or res.summary,
                                 "findings": out["tracker_log"][-1]["findings"]})
            if res.error:
                out["errors"].append(f"tracker {url}: {res.error}")
            out["findings"] += [f.model_dump() for f in res.findings]
        return out

    # 3 --------------------------------------------------------------- apply
    def apply(state: AgentState) -> AgentState:
        out: AgentState = {"config_changes": []}
        touched: set[str] = set()
        # this run's findings plus earlier ones still waiting for review (the tracked page may not
        # have changed since, e.g. 304, so they would never come back on their own)
        cands = [Finding(**fd) for fd in state.get("findings", [])]
        seen = {(f.entry, f.identity, f.new) for f in cands if f.type == "new_edition"}
        for x in rt.pending:
            if (x["entry"], x["identity"], x["new"]) not in seen:
                cands.append(Finding(**{k: v for k, v in x.items() if k != "first_seen"}))
        cands.sort(key=lambda f: -(T.FAMILIES[f.identity.split(" ", 1)[0]].rank(f.new)
                                   if f.type == "new_edition" and f.identity.split(" ", 1)[0] in T.FAMILIES else 0))
        for f in cands:
            if f.type == "new_edition" and _pending_resolved(rt, {"entry": f.entry, "identity": f.identity, "new": f.new}):
                continue                                     # a higher edition was already applied
            if f.type == "baseline":                       # remember the edition shown on the page
                rt.ident_state.setdefault(f.entry, {}).setdefault(f.identity, {})["edition"] = f.new
                continue
            if f.type != "new_edition":
                continue
            _note_available(rt, f)                         # latest edition known to exist, applied or not
            ok = s.auto_apply and f.confidence >= s.min_confidence and f.new_url and not rt.dry
            if ok:
                changes = _apply_new_edition(rt, f, touched)
                if changes:
                    f.applied = True
                    rt.ident_state.setdefault(f.entry, {}).setdefault(f.identity, {})["edition"] = f.new
                    out["config_changes"] += [c.model_dump() for c in changes]
                    for c in changes:
                        rt.append(p.changes, c.model_dump_json() + "\n")
            if not f.applied:
                _remember_pending(rt, f)
        persist_config(rt, touched)
        if touched:
            rt.entries, _ = parse_entries(rt.raw)
        # resolved pending items drop out
        rt.pending = [x for x in rt.pending if not _pending_resolved(rt, x)]
        return out

    # 4 ------------------------------------------------------------ sync_docs
    def sync_docs(state: AgentState) -> AgentState:
        urls: dict[str, list[str]] = {}
        labels: dict[str, str] = {}
        for e in rt.entries:
            if not rt.selected(e):
                continue
            for url, label in entry_sources(rt, e):
                urls.setdefault(url, []).append(e.key)
                labels.setdefault(url, label)
        out: AgentState = {"doc_actions": [], "errors": []}
        for url, keys in urls.items():
            try:
                act = _sync_one(rt, url, keys, labels[url])
            except Exception as exc:
                log.exception("sync failed for %s", url)
                act = DocAction(url=url, entries=keys, action="error", detail=f"{type(exc).__name__}: {exc}")
            if s.force:
                act.forced = True
                _stamp_forced(rt, act)
                if act.action == "error":
                    prev = read_json(rt.p.doc(docid_of(url)) / "state.json", {}).get("kg_built") if (rt.p.doc(docid_of(url)) / "kg.json").exists() else None
                    msg = (f"forced re-read FAILED for {url}: {act.detail}; " +
                           (f"the previous KG (built {prev}) was kept and may be out of date" if prev else "no KG exists for it yet"))
                    out.setdefault("forced_warnings", []).append(msg)
                    _log_forced(rt, {"kind": "warning", "url": url, "entries": keys, "detail": msg})
            if act.action == "error":
                out["errors"].append(f"document {url}: {act.detail}")
            out["doc_actions"].append(act.model_dump())
        return out

    # 5 -------------------------------------------------------------- assemble
    def assemble(state: AgentState) -> AgentState:
        changed_urls = {a["url"] for a in state.get("doc_actions", []) if a["action"] in ("created", "updated")}
        names = {e.id: e.name for e in rt.entries if e.kind == "standard"}
        out: AgentState = {"entry_actions": [], "errors": []}
        for e in rt.entries:
            if not rt.selected(e):
                continue
            try:
                out["entry_actions"].append(_assemble_one(rt, e, names, changed_urls).model_dump())
            except Exception as exc:
                log.exception("assemble failed for %s", e.key)
                out["entry_actions"].append(EntryAction(entry=e.key, action="error", detail=str(exc)).model_dump())
                out["errors"].append(f"entry {e.key}: {exc}")
        return out

    # 5b ------------------------------------------------------- version tracking
    def versions(state: AgentState) -> AgentState:
        out: AgentState = {"config_changes": [], "versions": []}
        touched: set[str] = set()
        for e in rt.entries:
            if not rt.selected(e):
                continue
            block = version_block(rt, e)
            fname, item = raw_item(rt, e.key)
            old = item.get("version_tracking")
            out["versions"].append({"entry": e.key, "name": e.name, **block})
            if old != block:
                ch = ConfigChange(run_id=rt.run_id, file=fname, entry=e.key, path="version_tracking",
                                  old=_vt_summary(old) if old else None, new=_vt_summary(block),
                                  reason="version tracking refreshed", source="agent")
                item["version_tracking"] = block
                touched.add(fname)
                out["config_changes"].append(ch.model_dump())
                rt.append(p.changes, ch.model_dump_json() + "\n")
        persist_config(rt, touched)
        return out

    # 5c ---------------------------------------------- combined graph (secondary staging)
    def combine(state: AgentState) -> AgentState:
        out: AgentState = {"notes": [], "errors": []}
        try:
            info = []
            for e in rt.entries:
                vt = raw_item(rt, e.key)[1].get("version_tracking") or {}
                info.append({"key": e.key, "kind": e.kind, "name": e.name, "paywalled": e.paywalled,
                             "complies_with": e.complies_with, "overlaps": e.overlaps,
                             "identities": [i.label for i in T.entry_identities(e)], "version_status": vt.get("status"),
                             "latest": [x.get("label") for x in vt.get("latest", [])],
                             "on_board": [fmt_edition(k, v.get("edition")) for k, v in (vt.get("staged") or {}).get("editions", {}).items()]})
            g, dep, meta = kg_combine.build(p.home, info, rt.run_id)
            rt.dependents = dep
            if not rt.dry and kg_combine.write(p.home, g, dep, meta, utcnow()):
                out["notes"].append(f"combined graph rebuilt: {meta['nodes']} entries, {meta['edges']} relations")
        except Exception as exc:                       # derived data must never fail the run
            log.exception("combined graph failed")
            out["errors"].append(f"combined graph: {exc}")
        return out

    # 6 ---------------------------------------------------------------- alerts
    def alerts(state: AgentState) -> AgentState:
        found = compute_alerts(rt, state)
        for a in found:                                # say what a change affects
            aff = (rt.dependents.get(a.entry) or {}).get("affects_reports") or {}
            if aff.get("declared") or aff.get("via_overlap") or aff.get("inferred"):
                a.details["affects_reports"] = aff
                bits = [f"{', '.join(aff[k])} ({lab})" for k, lab in (("declared", "declared"), ("via_overlap", "via overlapping standard"),
                                                                       ("inferred", "cites it in text")) if aff.get(k)]
                a.message += " Reports affected: " + "; ".join(bits) + "."
        now = utcnow()
        merged: dict[str, dict] = {}
        for a in found:
            old = rt.alerts.get(a.key)
            a.first_seen = old["first_seen"] if old else now
            a.runs = (old["runs"] + 1) if old else 1
            a.last_seen, a.is_new = now, old is None
            merged[a.key] = a.model_dump()
            log.warning("[%s] %s", a.kind, a.message)
        rt.alerts = merged
        rt.write_json(p.state / "alerts.json", merged)
        return {"alerts": list(merged.values())}

    # 7 --------------------------------------------------------------- finalize
    def finalize(state: AgentState) -> AgentState:
        rt.write_json(p.state / "trackers.json", {u: t.model_dump() for u, t in rt.tracker_state.items()})
        rt.write_json(p.state / "identities.json", rt.ident_state)
        rt.write_json(p.state / "pending.json", rt.pending)
        state["orphans"] = _track_orphans(rt, state)
        if s.tables:
            state["tabledb"] = _run_tabledb(rt, state)
        summary = summarize(state)
        narrative = narrate(rt, state, summary)
        text = render_report(rt, state, summary, narrative)
        rt.append(p.report, text)
        if s.force:
            _log_forced(rt, {"kind": "run", "summary": summary})
        rt.append(p.runs, json.dumps({"run_id": rt.run_id, "started": rt.started, "finished": utcnow(),
                                      "summary": summary}) + "\n")
        if not rt.dry and p.lock.exists():
            p.lock.unlink()
        return {"report": text, "summary": summary}

    return init, track, apply, sync_docs, assemble, versions, combine, alerts, finalize


def build_workflow(rt: Runtime):
    nodes = make_nodes(rt)
    wf = StateGraph(AgentState)
    names = ("init", "track", "apply", "sync_docs", "assemble", "versions", "combine", "alerts", "finalize")
    for n, fn in zip(names, nodes):
        wf.add_node(n, fn)
    wf.set_entry_point("init")
    for a, b in zip(names, names[1:]):
        wf.add_edge(a, b)
    wf.add_edge("finalize", END)
    return wf.compile()


# --------------------------------------------------------------------------
# Config edits (apply node)
# --------------------------------------------------------------------------


def persist_config(rt: Runtime, touched: set[str]) -> None:
    """Write edited configuration files and keep the snapshot / hash bookkeeping in sync
    (so the agent's own edits are never mistaken for manual ones)."""
    for name in touched:
        text = json.dumps(rt.raw[name], indent=2, ensure_ascii=False) + "\n"
        path = rt.s.standards if name == "standards" else rt.s.reports
        rt.write(Path(path), text)
        rt.write(rt.p.history / f"{rt.run_id}_{name}.json", text)
        rt.write(rt.p.history / f"{name}.last.json", text)
        hashes = read_json(rt.p.state / "config_hashes.json", {})
        hashes[name] = T._sha(text)
        rt.write_json(rt.p.state / "config_hashes.json", hashes)


def _note_available(rt: Runtime, f: Finding) -> None:
    """Record the newest edition a tracker has seen for an identifier (whether or not
    the configuration was updated to it)."""
    slot = rt.ident_state.setdefault(f.entry, {}).setdefault(f.identity, {})
    old = slot.get("available") or {}
    if old.get("edition") != f.new:
        slot["available"] = {"edition": f.new, "url": f.new_url, "text": f.new_text,
                             "detected": utcnow(), "source": f.tracker_url}


def _entry_of_path(path: str, name: str) -> str:
    import re
    m = re.search(r"\[id=(\d+)\]", path)
    return f"{'standard' if name == 'standards' else 'report'}:{m.group(1)}" if m else ""


def _apply_new_edition(rt: Runtime, f: Finding, touched: set[str]) -> list[ConfigChange]:
    """Rewrite the entry's link + name (and every other entry sharing the old link)
    for a new edition found by a tracker. Returns the audited changes."""
    fname, item = raw_item(rt, f.entry)
    fam, key = f.identity.split(" ", 1)
    ident = next((i for i in T.entry_identities(DocEntry.from_raw(
        "standard" if fname == "standards" else "report", item)) if i.family == fam and i.key == key), None)
    if ident is None or not f.new_url or not f.new_text:
        return []
    changes: list[ConfigChange] = []
    why = f"{f.identity}: {f.old} -> {f.new} (found on {f.tracker_url})"

    def edit(file: str, entry: str, obj: dict, field: str, new: Any, path: str) -> None:
        if obj.get(field) != new:
            changes.append(ConfigChange(run_id=rt.run_id, file=file, entry=entry, path=path,
                                        old=obj.get(field), new=new, reason=why, source=f.tracker_url))
            obj[field] = new
            touched.add(file)

    lf = _links_field(f.entry)
    links = item.setdefault(lf, [])
    # which link carries this identifier?
    def link_ident(l):
        return [i for i in T.parse_identities(l.get("text", "")) if (i.family, i.key) == (fam, key)]
    cands = [(i, l) for i, l in enumerate(links) if link_ident(l)]
    if cands:
        idx = max(cands, key=lambda c: T.FAMILIES[fam].rank(link_ident(c[1])[0].edition))[0]
    else:
        unlabeled = [i for i, l in enumerate(links) if not T.parse_identities(l.get("text", ""))]
        idx = unlabeled[0] if len(unlabeled) == 1 else None
    old_url = links[idx]["url"] if idx is not None else None
    if idx is None:
        links.append({"text": f.new_text, "url": f.new_url})
        changes.append(ConfigChange(run_id=rt.run_id, file=fname, entry=f.entry, path=f"{lf}[{len(links)-1}]",
                                    old=None, new=links[-1], reason=why, source=f.tracker_url))
        touched.add(fname)
    else:
        edit(fname, f.entry, links[idx], "text", T.retarget_text(links[idx].get("text", ""), ident, f.new_text),
             f"{lf}[{idx}].text")
        edit(fname, f.entry, links[idx], "url", f.new_url, f"{lf}[{idx}].url")
    nf = _name_field(f.entry)
    edit(fname, f.entry, item, nf, T.retarget_text(item[nf], ident, f.new_text), nf)
    # propagate to every other entry that used the same old URL
    if old_url and old_url != f.new_url:
        for other in rt.entries:
            if other.key == f.entry:
                continue
            ofile, oitem = raw_item(rt, other.key)
            for j, l in enumerate(oitem.get(_links_field(other.key), [])):
                if l.get("url") == old_url:
                    edit(ofile, other.key, l, "text", T.retarget_text(l.get("text", ""), ident, f.new_text),
                         f"{_links_field(other.key)}[{j}].text")
                    edit(ofile, other.key, l, "url", f.new_url, f"{_links_field(other.key)}[{j}].url")
    return changes


def _remember_pending(rt: Runtime, f: Finding) -> None:
    sig = (f.entry, f.identity, f.new)
    if not any((x["entry"], x["identity"], x["new"]) == sig for x in rt.pending):
        rt.pending.append({**f.model_dump(), "first_seen": utcnow()})


def _pending_resolved(rt: Runtime, item: dict) -> bool:
    cur = rt.ident_state.get(item["entry"], {}).get(item["identity"], {}).get("edition")
    fam = item["identity"].split(" ", 1)[0]
    return cur is not None and T.FAMILIES[fam].rank(cur) >= T.FAMILIES[fam].rank(item["new"])


# --------------------------------------------------------------------------
# Document sync (one per unique URL)
# --------------------------------------------------------------------------


def _extractor(s: Settings):
    return kg_ingest.make_extractor(s.extractor, s.model, s.ollama_host)


def _ecfr_date(rt: Runtime, url: str) -> Optional[str]:
    m = T._ECFR_PAGE.match(urlparse(url).path)
    key = m["title"] if m else ""
    if key not in rt.ecfr_dates:
        rt.ecfr_dates[key] = T.ecfr_latest_date(url, rt.ctx)
    return rt.ecfr_dates[key]


def _acquire_remote(rt: Runtime, url: str, keys: list[str], st: DocState, ddir: Path, kg_path: Path, now: str):
    """Conditional download of one remote document. Returns (finished_action | None, fetch_url, result)."""
    s = rt.s
    # eCFR pages are a JavaScript app: read the structured XML from the versioner API instead.
    # (Never fall back to the empty HTML shell: that would hide a failure as "unchanged".)
    fetch_url = url
    if T.ecfr_api_url(url, "2000-01-01"):
        date = _ecfr_date(rt, url)
        if not date:
            st.last_checked, st.consecutive_errors, st.last_error = now, st.consecutive_errors + 1, "eCFR date lookup failed"
            rt.write_json(ddir / "state.json", st.model_dump())
            return DocAction(url=url, entries=keys, action="error", detail="eCFR titles API unavailable"), fetch_url, None
        fetch_url = T.ecfr_api_url(url, date) or url
    same = st.fetch_url == fetch_url and not s.force      # forced update: never send If-None-Match / If-Modified-Since
    r = T.fetch(fetch_url, st.etag if same else "", st.last_modified if same else "",
                min_interval=s.min_interval, accept="application/pdf,text/html,application/xml,*/*;q=0.8")

    if r.not_modified and kg_path.exists():
        st.last_checked, st.consecutive_errors, st.last_error = now, 0, ""
        rt.write_json(ddir / "state.json", st.model_dump())
        return DocAction(url=url, entries=keys, action="unchanged", detail="304 not modified"), fetch_url, None
    if not r.ok:
        st.last_checked, st.consecutive_errors, st.last_error = now, st.consecutive_errors + 1, r.error
        rt.write_json(ddir / "state.json", st.model_dump())
        stale = " (STALE: repeatedly unreachable)" if st.consecutive_errors >= s.stale_after else ""
        return DocAction(url=url, entries=keys, action="error", detail=f"{r.error}{stale}"), fetch_url, None
    return None, fetch_url, r



def _kg_changed(diff: dict[str, Any]) -> bool:
    """Does a ``kg_diff`` show any difference between two KG versions?"""
    return bool(diff.get("nodes", [0, 0])[0] != diff.get("nodes", [0, 0])[1] or diff.get("edges", [0, 0])[0] != diff.get("edges", [0, 0])[1]
                or any(diff.get(k) for k in ("sections_added", "sections_removed", "sections_modified"))
                or diff.get("entities_added") or diff.get("entities_removed"))


def _log_forced(rt: Runtime, rec: dict[str, Any]) -> None:
    """Append one record to ``logs/forced.jsonl`` (audit trail of forced re-reads; nothing is written in a dry run)."""
    rt.append(rt.p.forced, json.dumps({"run_id": rt.run_id, "ts": utcnow(), "dry_run": rt.dry, **rec}, ensure_ascii=False) + "\n")


def _stamp_forced(rt: Runtime, act: DocAction) -> None:
    """Record a forced re-read in the document's state file and in the forced log."""
    ddir = rt.p.doc(docid_of(act.url))
    if (ddir / "state.json").exists():
        st = DocState(**read_json(ddir / "state.json", {"url": act.url}))
        st.last_forced, st.forced_runs, st.last_forced_result = utcnow(), st.forced_runs + 1, act.action
        rt.write_json(ddir / "state.json", st.model_dump())
    _log_forced(rt, {"kind": "document", "url": act.url, "docid": docid_of(act.url), "entries": act.entries, "result": act.action,
                     "detail": act.detail, "text_sha_before_after": act.shas, "archived": act.archived,
                     **({"assets_purged": act.purged} if act.purged and act.purged.get("files") else {})})


def _tree_stats(path: Path) -> tuple[int, int]:
    files = [f for f in path.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def _prune_assets(assets_root: Path, keep: Path) -> dict[str, Any]:
    """Purge stale exported assets of one document after its assets were re-exported into ``keep``:
    files in ``keep/figures|tables`` that the new manifest does not list, and whole sibling folders
    (assets of an earlier fetch URL, e.g. an older eCFR date). Returns what was removed."""
    out: dict[str, Any] = {"files": 0, "bytes": 0, "dirs": 0, "names": []}
    man = read_json(keep / "manifest.json")
    if man is None or not assets_root.is_dir():
        return out                                            # nothing trustworthy to compare with: purge nothing
    wanted = {a["file"] for a in man.get("assets", [])}
    for sub in ("figures", "tables"):
        for f in sorted((keep / sub).glob("*")) if (keep / sub).is_dir() else []:
            if f.is_file() and f"{sub}/{f.name}" not in wanted:
                out["files"] += 1
                out["bytes"] += f.stat().st_size
                out["names"].append(f"{keep.name[:8]}/{sub}/{f.name}")
                f.unlink()
    for d in sorted(assets_root.iterdir()):
        if d.is_dir() and d.resolve() != keep.resolve():
            n, b = _tree_stats(d)
            out["files"], out["bytes"], out["dirs"] = out["files"] + n, out["bytes"] + b, out["dirs"] + 1
            out["names"].append(f"{d.name[:8]}/ ({n} files)")
            shutil.rmtree(d)
    out["names"] = out["names"][:20]
    return out


def _find_orphans(rt: Runtime) -> list[dict[str, Any]]:
    """Staged data the configuration no longer references: document folders (by URL) and entry folders (by id)."""
    p = rt.p
    live_docs = {docid_of(u) for e in rt.entries for u, _ in entry_sources(rt, e)}
    live_entries = {e.key for e in rt.entries}
    out = []
    for d in sorted(p.docs.glob("*/state.json")):
        if d.parent.name not in live_docs:
            st = read_json(d, {})
            out.append({"kind": "document", "id": d.parent.name, "label": st.get("url", ""), "path": d.parent,
                        "restore_into": "staging/documents"})
    for kind in ("standard", "report"):
        base = p.home / "staging" / f"{kind}s"
        for d in sorted(base.glob("*")) if base.is_dir() else []:
            if d.is_dir() and d.name.isdigit() and f"{kind}:{d.name}" not in live_entries:
                out.append({"kind": "entry", "id": f"{kind}:{d.name}", "label": read_json(d / "meta.json", {}).get("name", ""),
                            "path": d, "restore_into": f"staging/{kind}s"})
    return out


def _archive_orphan(rt: Runtime, o: dict[str, Any]) -> dict[str, Any]:
    """tar.gz one orphan under archive/orphans/, verify the archive, only then remove it from staging."""
    src: Path = o["path"]
    n_files, n_bytes = _tree_stats(src)
    name = f"{o['kind']}_{o['id'].replace(':', '_')}_{rt.run_id}.tar.gz"
    dest = rt.p.orphan_archive / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():                                        # never overwrite an earlier archive
        n += 1
        name = f"{o['kind']}_{o['id'].replace(':', '_')}_{rt.run_id}_{n}.tar.gz"
        dest = rt.p.orphan_archive / name
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(src, arcname=src.name)
    with tarfile.open(dest, "r:gz") as tf:                       # verify before deleting anything
        members = [m for m in tf.getmembers() if m.isfile()]
    if len(members) != n_files:
        dest.unlink()
        raise RuntimeError(f"archive verification failed ({len(members)} of {n_files} files)")
    sha = hashlib.sha256(dest.read_bytes()).hexdigest()
    shutil.rmtree(src)
    return {"archive": f"archive/orphans/{name}", "archive_bytes": dest.stat().st_size, "archive_sha256": sha,
            "files": n_files, "original_bytes": n_bytes,
            "restore": f"tar -xzf {rt.p.home}/archive/orphans/{name} -C {rt.p.home}/{o['restore_into']}/"}


def _track_orphans(rt: Runtime, state: AgentState) -> list[dict[str, Any]]:
    """Detect orphans, record them in state/orphans.json (first seen, status, archive) and, in a forced run, archive
    them (compressed) and remove them from staging. Returns this run's orphan records for the report."""
    s = rt.s
    track = read_json(rt.p.orphans, {})
    found = _find_orphans(rt)
    now = utcnow()
    live_keys = {f"{o['kind']}:{o['id']}" for o in found}
    for k in [k for k, v in track.items() if v["status"] == "orphaned" and k not in live_keys]:
        del track[k]                                              # referenced again (or removed by hand)
    archive_ok = s.force and s.prune and not rt.dry
    why = ""
    if s.force and s.prune and s.only:
        why = "orphan archiving skipped: the run is limited by --only"
    elif s.force and s.prune and (rt.config_errors or not rt.entries):
        why = "orphan archiving skipped: the configuration has validation errors or no entries"
    elif s.force and not s.prune:
        why = "orphan archiving skipped: --no-prune"
    if why:
        archive_ok = False
    out = []
    for o in found:
        key = f"{o['kind']}:{o['id']}"
        rec = track.get(key) or {"kind": o["kind"], "id": o["id"], "label": o["label"], "first_orphaned": now, "runs": 0}
        rec.update(label=o["label"] or rec.get("label", ""), last_seen=now, runs=rec["runs"] + 1, status="orphaned")
        if archive_ok:
            try:
                rec.update(_archive_orphan(rt, o), status="archived", archived_at=now, archived_run=rt.run_id)
            except Exception as exc:                              # never lose data: staging is untouched on failure
                log.exception("orphan archive failed for %s", key)
                rec["last_error"] = f"{type(exc).__name__}: {exc}"
                state.setdefault("errors", []).append(f"orphan {key}: {rec['last_error']}")
        track[key] = rec
        out.append({**rec, "skipped_reason": why, "archived_now": rec["status"] == "archived" and rec.get("archived_run") == rt.run_id})
        if s.force:
            _log_forced(rt, {"kind": "orphan", "orphan_kind": o["kind"], "id": o["id"], "label": rec["label"],
                             "result": rec["status"], "detail": why or rec.get("last_error", ""),
                             **{k: rec[k] for k in ("archive", "archive_bytes", "archive_sha256", "files") if k in rec}})
    if why and found:
        state.setdefault("forced_warnings", []).append(f"{len(found)} orphaned staging item(s) found; {why}")
    rt.write_json(rt.p.orphans, track)
    return out


def _run_tabledb(rt: Runtime, state: AgentState) -> dict[str, Any]:
    """Second stage, after staging: tables -> PostgreSQL + pgvector (kg_tabledb). Never fails the run."""
    s = rt.s
    if rt.dry:
        return {"not_run": "dry run"}
    dsn = s.tabledb_dsn or os.environ.get("COGENT_TABLEDB_DSN")
    if not dsn:
        msg = "table database not built: no DSN (--tabledb-dsn or COGENT_TABLEDB_DSN)"
        state.setdefault("notes", []).append(msg)
        return {"error": msg}
    try:
        import kg_tabledb
        from kg_embed import get_embedder
        emb = get_embedder(s.embedder, s.embed_model, s.ollama_host)
        out = kg_tabledb.run(rt.p.home, dsn, emb, only=s.only, force=s.force, kg_context=s.kg_context, schema=s.tabledb_schema, row_mode=s.row_mode,
                             **kg_tabledb.llm_kwargs(s.llm_joins, s.join_model, s.join_judge_model, s.ollama_host, s.join_samples, s.join_judge_runs,
                                                     s.join_max_pairs, s.join_max_seconds))
    except Exception as exc:
        log.exception("table database stage failed")
        msg = f"table database stage failed: {type(exc).__name__}: {exc}"
        state.setdefault("errors", []).append(msg)
        return {"error": msg}
    for e in out.get("errors", []):
        state.setdefault("errors", []).append(f"table database: {e}")
    return {k: out[k] for k in ("embedder", "sources", "loaded", "unchanged", "skipped", "dropped", "joins", "documents_with_joins", "llm_joins", "errors", "warnings")}


def _sync_one(rt: Runtime, url: str, keys: list[str], label: str) -> DocAction:
    s, p = rt.s, rt.p
    docid = docid_of(url)
    ddir = p.doc(docid)
    st = DocState(**read_json(ddir / "state.json", {"url": url})) if (ddir / "state.json").exists() else DocState(url=url)
    kg_path = ddir / "kg.json"
    now = utcnow()
    forced = s.force

    if url.startswith("file://"):                      # purchased / local copy of a paywalled document
        path = Path(unquote(urlparse(url).path))
        if not path.is_file():
            return DocAction(url=url, entries=keys, action="error", detail=f"local copy missing: {path}")
        fetch_url, r = str(path), T.FetchResult(url, 200, path.read_bytes(), final_url=str(path), via="local")
    else:
        done, fetch_url, r = _acquire_remote(rt, url, keys, st, ddir, kg_path, now)
        if done is not None:
            return done

    kind = T.sniff_kind(r.data, r.content_type, fetch_url)
    raw_sha, text_sha = T.fingerprint(r.data, kind)
    st.etag, st.last_modified, st.fetch_url, st.content_kind = r.etag, r.last_modified, fetch_url, kind
    st.last_checked, st.consecutive_errors, st.last_error = now, 0, ""
    same_text = kg_path.exists() and st.text_sha == text_sha
    if same_text and not forced:
        reissued = raw_sha != st.raw_sha
        st.raw_sha = raw_sha
        rt.write_json(ddir / "state.json", st.model_dump())
        return DocAction(url=url, entries=keys, action="unchanged",
                         detail="content identical" + (" (file re-issued, same text)" if reissued else ""))

    # ---- (re)build the document KG
    old = load_graph_file(kg_path) if kg_path.exists() else None
    if rt.dry:
        return DocAction(url=url, entries=keys, forced=forced, shas=[st.text_sha[:12], text_sha[:12]],
                         action="created" if old is None else "refreshed" if (forced and same_text) else "updated",
                         detail="dry run: would rebuild the KG" + (" (forced re-read)" if forced else ""))
    kg_ingest.clear_prefetched()
    kg_ingest.prefetch(fetch_url, r.data)
    try:
        g = kg_ingest.file_to_knowledge_graph(
            fetch_url, _extractor(s), assets_dir=str(ddir / "assets") if s.assets else None,
            doc_label=label)
    finally:
        kg_ingest.clear_prefetched()
    g.graph.update(source=url, fetch_url=fetch_url, text_sha=text_sha, raw_sha=raw_sha, fetched=now,
                   content_kind=kind)
    T.add_identifier_citations(g)
    T.add_cfr_crossrefs(g)
    # editions the document states about itself: its text, plus the file name for a local copy
    # (never the configured link text, which would just echo the configuration)
    st.ocr_pages = list(g.graph.get("ocr_pages", []))
    st.identities = doc_identities(g, Path(unquote(urlparse(url).path)).name if url.startswith("file://") else "")
    diff = T.kg_diff(old, g)
    # a forced re-read of identical text only counts as a new version when the rebuilt KG differs (e.g. parser changes)
    changed_kg = old is None or not same_text or _kg_changed(diff)
    old_sha = st.text_sha

    archived = None
    if old is not None and changed_kg:
        archived = _archive(rt, "documents", docid, [kg_path, ddir / "state.json"],
                            f"{now.replace(':', '').replace('-', '')}_{st.text_sha[:8]}")
    rt.save_graph(g, kg_path)
    st.raw_sha, st.text_sha = raw_sha, text_sha
    st.first_seen = st.first_seen or now
    st.kg_built = now
    if changed_kg:
        st.last_changed, st.versions = now, st.versions + 1
    st.kg_nodes, st.kg_edges = g.number_of_nodes(), g.number_of_edges()
    rt.write_json(ddir / "state.json", st.model_dump())
    _prune_archive(rt, "documents", docid)
    ocr = f"; OCR on page(s) {st.ocr_pages} ({g.graph.get('ocr_engine')})" if st.ocr_pages else ""
    purged = {}
    if forced and s.assets and s.prune and g.graph.get("assets_dir"):     # purge assets the new export no longer produced
        purged = _prune_assets(ddir / "assets", Path(g.graph["assets_dir"]))
    if forced and old is not None and not changed_kg:
        return DocAction(url=url, entries=keys, action="refreshed", forced=True, shas=[old_sha[:12], text_sha[:12]], diff=diff, purged=purged,
                         detail=f"forced re-read: content identical; KG rebuilt ({g.number_of_nodes()} nodes / {g.number_of_edges()} edges){ocr}")
    why = ""
    if forced and old is not None:
        why = "forced re-read: " + ("source text changed; " if old_sha != text_sha else "text identical but the rebuilt KG differs; ")
    return DocAction(url=url, entries=keys, action="created" if old is None else "updated", forced=forced,
                     shas=[old_sha[:12], text_sha[:12]] if forced else [], purged=purged,
                     detail=f"{why}{kind}, {g.number_of_nodes()} nodes / {g.number_of_edges()} edges{ocr}",
                     diff=diff, archived=archived)


def _archive(rt: Runtime, group: str, name: str, files: list[Path], label: str) -> Optional[str]:
    if rt.dry:
        return None
    dest = rt.p.archive / group / name / label
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f.exists():
            shutil.copy2(f, dest / f.name)
    return str(dest.relative_to(rt.p.home))


def _prune_archive(rt: Runtime, group: str, name: str) -> None:
    keep = rt.s.keep_archive
    base = rt.p.archive / group / name
    if keep and base.exists():
        for old in sorted(base.iterdir())[:-keep]:
            shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------
# Entry assembly
# --------------------------------------------------------------------------


def _assemble_one(rt: Runtime, e: DocEntry, names: dict[int, str], changed_urls: set[str]) -> EntryAction:
    p = rt.p
    edir = p.entry(e)
    parts, missing, sig_parts = [], [], []
    sources = entry_sources(rt, e)
    for url, _label in sources:
        docid = docid_of(url)
        g = load_graph_file(p.doc(docid) / "kg.json")
        if g is None:
            missing.append(url)
            sig_parts.append([url, "missing"])
        else:
            parts.append((docid[:8], g))
            sig_parts.append([url, g.graph.get("text_sha", "")])
    sig = T._sha(json.dumps([e.name, e.complies_with, e.overlaps, sig_parts], sort_keys=True))
    meta = read_json(edir / "meta.json", {})
    same_sig = meta.get("signature") == sig and (edir / "kg.json").exists()
    if same_sig and not rt.s.force:
        return EntryAction(entry=e.key, action="unchanged")
    if rt.dry:
        return EntryAction(entry=e.key, action="refreshed" if same_sig else "rebuilt", detail="dry run" + (" (forced)" if same_sig else ""))
    g = T.merge_graphs(parts, e, names)
    old_exists = (edir / "kg.json").exists()
    if same_sig:                       # forced rebuild of an entry whose inputs did not change
        diff = T.kg_diff(load_graph_file(edir / "kg.json"), g)
        if not _kg_changed(diff):      # identical result: refresh the files, keep no archive copy
            rt.save_graph(g, edir / "kg.json")
            rt.write_json(edir / "meta.json", {**meta, "built": utcnow(), "nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
                                               "missing": missing})
            return EntryAction(entry=e.key, action="refreshed", detail=f"forced rebuild: identical ({g.number_of_nodes()} nodes)")
    archived = None
    if old_exists:
        archived = _archive(rt, "entries", e.key.replace(":", "_"), [edir / "kg.json", edir / "meta.json"],
                            utcnow().replace(":", "").replace("-", ""))
    rt.save_graph(g, edir / "kg.json")
    rt.write_json(edir / "meta.json", {"signature": sig, "built": utcnow(), "name": e.name,
                                       "documents": [{"url": u, "docid": docid_of(u)} for u, _ in sources],
                                       "missing": missing, "nodes": g.number_of_nodes(),
                                       "edges": g.number_of_edges()})
    detail = f"{len(parts)}/{len(sources)} document KG(s) merged, {g.number_of_nodes()} nodes"
    if not sources:
        detail = "no source documents configured (entry node and relations only)"
    if missing:
        detail += f"; missing: {len(missing)}"
    return EntryAction(entry=e.key, action="rebuilt" if old_exists else "created", detail=detail,
                       archived=archived)


# --------------------------------------------------------------------------
# Version tracking: what is staged vs. what is the latest edition available
# --------------------------------------------------------------------------


def version_block(rt: Runtime, e: DocEntry) -> dict[str, Any]:
    """The ``version_tracking`` block of one entry (validated by ``VersionTracking``)."""
    idents = [i for i in T.entry_identities(e) if T.FAMILIES[i.family].tracks_editions]
    fam = lambda i: T.FAMILIES[i.family]

    # ---- staged: documents whose KG is part of the entry KG
    docs, stated, content_docs, missing = [], {}, 0, 0
    for url, _ in entry_sources(rt, e):
        ddir = rt.p.doc(docid_of(url))
        if not (ddir / "kg.json").exists():
            missing += 1
            continue
        st = DocState(**read_json(ddir / "state.json", {"url": url}))
        local = url.startswith("file://")
        d: dict[str, Any] = {"url": f"local:{Path(unquote(urlparse(url).path)).name}" if local else url,
                             "kind": st.content_kind, "local": local, "fetched": st.last_changed or None,
                             "text_sha": st.text_sha[:12]}
        if e.paywalled and not local:
            d["role"] = "store page"                 # a landing page is not the standard itself
        m = re.search(r"/full/(\d{4}-\d{2}-\d{2})/", st.fetch_url)
        if m:
            d["as_of"] = m.group(1)
        if st.ocr_pages:
            d["ocr_pages"] = st.ocr_pages
        if any(v is not None for v in st.identities.values()):
            d["editions"] = {k: v for k, v in st.identities.items() if v is not None}
        docs.append(d)
        if not (e.paywalled and not local):
            content_docs += 1
            for lab, ed in st.identities.items():
                f0 = T.FAMILIES.get(lab.split(" ", 1)[0])
                if f0 and ed is not None and (lab not in stated or f0.rank(ed) > f0.rank(stated[lab])):
                    stated[lab] = ed
    meta = read_json(rt.p.entry(e) / "meta.json", {})
    editions: dict[str, Any] = {}
    for i in idents:
        if i.label in stated:
            editions[i.label] = {"edition": stated[i.label], "source": "document"}
        elif content_docs and i.edition is not None:
            editions[i.label] = {"edition": i.edition, "source": "configuration"}
    staged = {"kg": str(rt.p.entry(e).relative_to(rt.p.home) / "kg.json") if meta else None,
              "kg_built": meta.get("built"), "documents": docs, "editions": editions}

    # ---- latest: newest edition any tracker has seen (or the configured one)
    latest = []
    for i in idents:
        slot = rt.ident_state.get(e.key, {}).get(i.label, {})
        known = slot.get("edition") or i.edition
        av = slot.get("available")
        link_url = next((l.url for l in e.links
                         if any((j.family, j.key) == (i.family, i.key) for j in T.parse_identities(l.text))), None)
        if av and fam(i).rank(av["edition"]) > fam(i).rank(known):
            latest.append({"identity": i.label, "edition": av["edition"], "label": fam(i).fmt(i.key, av["edition"]),
                           "url": av.get("url"), "detected": av.get("detected"), "source": av.get("source")})
        else:
            latest.append({"identity": i.label, "edition": known, "label": fam(i).fmt(i.key, known),
                           "url": av.get("url") if av and av.get("edition") == known else link_url,
                           "detected": (av or {}).get("detected") if av and av.get("edition") == known else None,
                           "source": (av or {}).get("source") if av and av.get("edition") == known else None})
    for url in e.tracking_urls:                       # eCFR: the issue date of the newest version
        ts = rt.tracker_state.get(url)
        if ts and ts.tracker == "ecfr-versions" and ts.data.get("latest_issue_date"):
            title = re.search(r"title-(\d+)", url)
            part = parse_qs(urlparse(url).query).get("part", [""])[0]
            latest.append({"identity": f"eCFR title {title.group(1) if title else '?'} part {part}".strip(),
                           "edition": ts.data["latest_issue_date"], "label": f"eCFR as of {ts.data['latest_issue_date']}",
                           "url": url, "detected": ts.last_changed or None, "source": url})

    # ---- status
    if not entry_sources(rt, e):
        status = "no_documents"
    elif e.paywalled and not local_files(rt, e):
        status = "no_local_copy"
    elif not docs:
        status = "not_staged"
    elif missing:
        status = "partially_staged"
    else:
        status = "current"
        ecfr_as_of = [d["as_of"] for d in docs if d.get("as_of")]
        for lt in latest:
            if lt["identity"].startswith("eCFR"):
                if ecfr_as_of and min(ecfr_as_of) < lt["edition"]:
                    status = "update_available"
            else:
                f0 = T.FAMILIES[lt["identity"].split(" ", 1)[0]]
                have = editions.get(lt["identity"], {}).get("edition")
                if lt["edition"] is not None and have is not None and f0.rank(lt["edition"]) > f0.rank(have):
                    status = "update_available"
    return VersionTracking.model_validate({"status": status, "latest": latest, "staged": staged}).model_dump(
        mode="json", exclude_none=True, exclude_defaults=False)


def fmt_edition(identity: str, edition: Optional[str]) -> str:
    """'RG 1.233', '1' -> 'RG 1.233 Rev. 1'."""
    fam, _, key = identity.partition(" ")
    return T.FAMILIES[fam].fmt(key, edition) if fam in T.FAMILIES else f"{identity} {edition or ''}".strip()


def _vt_summary(b: dict[str, Any]) -> str:
    lat = ", ".join(l.get("label") or l["identity"] for l in b.get("latest", [])) or "-"
    st = b.get("staged", {})
    eds = ", ".join(fmt_edition(k, v["edition"]) for k, v in st.get("editions", {}).items() if v.get("edition")) or "-"
    return f"{b.get('status')}; latest: {lat}; staged: {len(st.get('documents', []))} doc(s), {eds}; kg {st.get('kg_built') or '-'}"


# --------------------------------------------------------------------------
# Alerts for paywalled documents
# --------------------------------------------------------------------------


def compute_alerts(rt: Runtime, state: AgentState) -> list[Alert]:
    """Warnings a human must act on for paywalled documents:

    * ``no_local_copy``            there is nothing local to process (the agent cannot download it)
    * ``new_edition_for_purchase`` a tracker saw a newer edition on sale (kept until the local copy is that new)
    * ``local_copy_outdated``      the local copy states an older edition than the latest known one
    * ``local_copy_unidentified``  (info) the edition of the local copy could not be read from its text
    * ``store_page_changed``       (info) the landing/store page changed (possible new edition/errata)
    """
    out: list[Alert] = []
    findings = state.get("findings", [])
    for e in rt.entries:
        if not (e.paywalled and rt.selected(e)):
            continue
        files = local_files(rt, e)
        idents = [i for i in T.entry_identities(e) if T.FAMILIES[i.family].tracks_editions]
        latest = {i.label: rt.ident_state.get(e.key, {}).get(i.label, {}).get("edition") or i.edition for i in idents}
        known = ", ".join(T.FAMILIES[i.family].fmt(i.key, latest[i.label]) for i in idents)
        expected = (rt.p.local / f"{e.kind}s" / str(e.id)) if not e.local_copy else Path(e.local_copy)
        if not files:
            out.append(Alert(kind="no_local_copy", entry=e.key, message=(
                f"{e.key} {e.name}: paywalled and there is no local copy to process. Put the purchased PDF/HTML in "
                f"{expected}/ (or set 'local_copy' in the configuration). Latest known edition: {known or 'unknown'}.")))
        # editions stated by the local copies
        local_eds: dict[str, Optional[str]] = {}
        for url, _ in entry_sources(rt, e):
            if url.startswith("file://"):
                ds = read_json(rt.p.doc(docid_of(url)) / "state.json", {})
                for lab, ed in (ds.get("identities") or {}).items():
                    fam = lab.split(" ", 1)[0]
                    if fam in T.FAMILIES and (lab not in local_eds or T.FAMILIES[fam].rank(ed) > T.FAMILIES[fam].rank(local_eds[lab])):
                        local_eds[lab] = ed
        # new editions on sale: this run's findings + earlier ones not yet satisfied by a local copy
        new_keys: set[str] = set()
        candidates = [f for f in findings if f["entry"] == e.key and f["type"] == "new_edition"]
        for old in rt.alerts.values():
            if old["kind"] == "new_edition_for_purchase" and old["entry"] == e.key:
                candidates.append({"identity": old["identity"], "new": old["details"].get("new"), "old": old["details"].get("old"),
                                   "new_url": old["details"].get("new_url"), "new_text": old["details"].get("new_text"),
                                   "tracker_url": old["details"].get("tracker_url", ""), "carried": True})
        for f in candidates:
            lab = f["identity"]
            fam = T.FAMILIES[lab.split(" ", 1)[0]]
            have = local_eds.get(lab)
            if have is not None and fam.rank(have) >= fam.rank(f["new"]):
                continue                                           # the local copy is already that new
            key = f"new_edition_for_purchase|{e.key}|{lab}"
            if key in new_keys:
                continue
            new_keys.add(key)
            out.append(Alert(kind="new_edition_for_purchase", entry=e.key, identity=lab, details={
                "old": f["old"], "new": f["new"], "new_url": f.get("new_url"), "new_text": f.get("new_text"),
                "tracker_url": f.get("tracker_url", "")},
                message=(f"{e.key} {e.name}: a new edition is published for purchase: {f.get('new_text') or lab + ' ' + str(f['new'])} "
                         f"(was {f['old']}; {'local copy: ' + str(have) if have else 'no local copy of it'}). "
                         f"Buy it and place it in {expected}/. {f.get('new_url') or ''}".strip())))
        for lab, want in latest.items():
            if files and want:
                fam = T.FAMILIES[lab.split(" ", 1)[0]]
                have = local_eds.get(lab)
                if have is None:
                    out.append(Alert(kind="local_copy_unidentified", entry=e.key, identity=lab, severity="info", message=(
                        f"{e.key}: cannot tell which edition of {lab} the local copy ({', '.join(f.name for f in files)}) is; "
                        f"expected {want}.")))
                elif fam.rank(have) < fam.rank(want) and f"new_edition_for_purchase|{e.key}|{lab}" not in new_keys:
                    out.append(Alert(kind="local_copy_outdated", entry=e.key, identity=lab, details={"have": have, "latest": want},
                                     message=(f"{e.key} {e.name}: the local copy is {lab} {have} but the latest known edition is {want}; "
                                              f"buy the new edition and replace the local file.")))
        for a in state.get("doc_actions", []):
            if a["action"] == "updated" and e.key in a["entries"] and not a["url"].startswith("file://"):
                out.append(Alert(kind="store_page_changed", entry=e.key, identity=a["url"], severity="info", message=(
                    f"{e.key}: the store/landing page changed ({a['url']}); check it for a new edition, errata or a price/availability change.")))
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def summarize(state: AgentState) -> dict[str, Any]:
    da = state.get("doc_actions", [])
    count = lambda xs, k, v: sum(1 for x in xs if x[k] == v)
    fs = state.get("findings", [])
    return {
        "documents": len(da),
        "docs_created": count(da, "action", "created"), "docs_updated": count(da, "action", "updated"),
        "docs_unchanged": count(da, "action", "unchanged"), "docs_error": count(da, "action", "error"),
        "forced": bool(state.get("forced")), "docs_refreshed": count(da, "action", "refreshed"),
        "entries_refreshed": sum(1 for x in state.get("entry_actions", []) if x["action"] == "refreshed"),
        "assets_purged_files": sum((x.get("purged") or {}).get("files", 0) for x in da),
        "assets_purged_bytes": sum((x.get("purged") or {}).get("bytes", 0) for x in da),
        "orphans": len(state.get("orphans", [])), "orphans_archived": sum(1 for o in state.get("orphans", []) if o.get("archived_now")),
        "forced_warnings": len(state.get("forced_warnings", [])),
        "tabledb": state.get("tabledb"),
        "entries_rebuilt": sum(1 for x in state.get("entry_actions", []) if x["action"] in ("rebuilt", "created")),
        "trackers": len(state.get("tracker_log", [])),
        "trackers_changed": sum(1 for t in state.get("tracker_log", []) if t["changed"]),
        "findings": sum(1 for f in fs if f["type"] != "baseline"),
        "config_changes": len(state.get("config_changes", [])),
        "errors": len(state.get("errors", [])),
        "warnings": sum(1 for a in state.get("alerts", []) if a["severity"] == "warning"),
        "not_current": sum(1 for v in state.get("versions", []) if v["status"] != "current"),
    }


def narrate(rt: Runtime, state: AgentState, summary: dict) -> Optional[RunNarrative]:
    """Optional: a local LLM (Ollama through LangChain) narrates the structured results."""
    llm = rt.llm
    if llm is None and rt.s.llm:
        try:
            from langchain_ollama import ChatOllama
            llm = ChatOllama(model=rt.s.llm, temperature=0, base_url=rt.s.ollama_host)
        except Exception as exc:
            log.warning("LLM narration unavailable: %s", exc)
            return None
    if llm is None:
        return None
    facts = {"summary": summary,
             "config_changes": [{k: c[k] for k in ("entry", "path", "old", "new")} for c in state.get("config_changes", [])][:20],
             "document_updates": [{"url": a["url"], "action": a["action"], "diff": a["diff"]}
                                  for a in state.get("doc_actions", []) if a["action"] in ("created", "updated")][:20],
             "findings": [{k: f.get(k) for k in ("type", "entry", "identity", "old", "new")}
                          for f in state.get("findings", []) if f["type"] not in ("baseline",)][:30],
             "errors": state.get("errors", [])[:10]}
    try:
        chain = llm.with_structured_output(RunNarrative)
        return chain.invoke("Summarise this documentation-update run for a nuclear licensing team in at most "
                            "4 sentences, then list the most important changes. Use only the facts given.\n"
                            + json.dumps(facts, default=str)[:6000])
    except Exception as exc:
        log.warning("LLM narration failed: %s", exc)
        return None


def _md(x: Any, n: int = 90) -> str:
    t = str(x).replace("|", "\\|").replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def render_report(rt: Runtime, state: AgentState, summary: dict, narrative: Optional[RunNarrative]) -> str:
    L: list[str] = []
    mode = (" (DRY RUN)" if rt.dry else "") + (" (FORCED UPDATE)" if summary.get("forced") else "")
    L.append(f"## Run {rt.run_id}{mode}\n")
    L.append(f"Started {rt.started}, finished {utcnow()}.\n")
    L.append(f"**Summary:** {summary['documents']} document(s) checked: {summary['docs_created']} new, "
             f"{summary['docs_updated']} updated, " + (f"{summary['docs_refreshed']} re-read unchanged (forced), " if summary.get("forced") else "") +
             f"{summary['docs_unchanged']} unchanged, {summary['docs_error']} failed. "
             f"{summary['entries_rebuilt']} entry KG(s) rebuilt, {summary['not_current']} entr(ies) not current. {summary['trackers']} tracking page(s) checked "
             f"({summary['trackers_changed']} with changes). {summary['config_changes']} configuration edit(s). "
             f"{summary['errors']} error(s). **{summary['warnings']} warning(s).**\n")
    if narrative:
        L.append(f"**Narrative (local LLM):** {narrative.summary}\n")
        L += [f"- {h}" for h in narrative.highlights]
        L.append("")
    for n in state.get("notes", []):
        L.append(f"_Note: {n}_\n")

    if summary.get("forced"):
        fd = [a for a in state.get("doc_actions", []) if a.get("forced")]
        L.append(f"### Forced update: {len(fd)} source document(s) and {len(state.get('tracker_log', []))} tracking page(s) re-read directly\n")
        L.append("| result | document | entries | text sha before → after | detail |\n|---|---|---|---|---|")
        for a in fd:
            sh = " → ".join(x or "-" for x in (a.get("shas") or [])) or "-"
            L.append(f"| {a['action']} | {_md(a['url'], 70)} | {_md(', '.join(a['entries']), 40)} | `{sh}` | {_md(a['detail'], 90)} |")
        ef = [a for a in state.get("entry_actions", []) if a["action"] in ("refreshed", "rebuilt", "created")]
        if ef:
            L.append(f"\nEntry KGs rebuilt by the forced update: {', '.join(a['entry'] + ' (' + a['action'] + ')' for a in ef)}.")
        L.append("\n| tracking page | tracker | result | findings |\n|---|---|---|---|")
        for t in state.get("tracker_log", []):
            res = "error" if t["error"] else "changed" if t["changed"] else "unchanged"
            L.append(f"| {_md(t['url'], 70)} | {t['tracker']} | {res} | {_md('; '.join(t.get('findings') or []) or '-', 90)} |")
        pg = [a for a in fd if (a.get("purged") or {}).get("files")]
        if pg:
            L.append(f"\nStale asset files purged: {summary['assets_purged_files']} file(s), {summary['assets_purged_bytes']:,} bytes "
                     f"(" + "; ".join(f"{_md(a['url'], 40)}: {a['purged']['files']} file(s)" for a in pg) + ").")
        fw = state.get("forced_warnings", [])
        if fw:
            L.append("\n**Forced-update warnings**\n")
            L += [f"- **WARNING** {_md(w, 260)}" for w in fw]
        L.append(f"\n_Every re-read is logged in `{rt.p.forced}`; new editions found are handled as in a normal run (see tracker findings below). "
                 "Failed re-reads keep the previous KG; entries outside `--only`, `archive/` (apart from new versions and `archive/orphans/`) "
                 "and `local/` are not modified._\n")
    orph = state.get("orphans", [])
    if orph:
        arch = [o for o in orph if o["status"] == "archived"]
        L.append(f"### Orphaned staging data ({len(orph)}: no longer referenced by the configuration)\n")
        L.append("| kind | id | label | first orphaned | status | archive |\n|---|---|---|---|---|---|")
        for o in orph:
            ar = f"`{o['archive']}` ({o['archive_bytes']:,} B, sha256 `{o['archive_sha256'][:12]}`)" if o.get("archive") else (o["skipped_reason"] or "-")
            L.append(f"| {o['kind']} | {_md(o['id'], 30)} | {_md(o['label'], 50)} | {o['first_orphaned'][:10]} | {o['status']} | {ar} |")
        if not rt.s.force:
            L.append("\n_Still in staging. `run --force` archives them (compressed, under `archive/orphans/`) and removes them from staging; "
                     "tracked in `state/orphans.json`._")
        elif arch:
            L.append("\n_Restore with the `restore` command recorded in `state/orphans.json` (tar -xzf … -C staging/…)._")
        L.append("")

    td = state.get("tabledb")
    if td:
        L.append("### Table database\n")
        if td.get("error") or td.get("not_run"):
            L.append(f"- {td.get('error') or 'not run: ' + td['not_run']}")
        else:
            L.append(f"- {td['sources']} staged table(s): **{td['loaded']} loaded**, {td['unchanged']} unchanged, {td['skipped']} skipped, {td['dropped']} dropped; "
                     f"{td['joins']} join(s) inferred in {td['documents_with_joins']} document(s); embedder `{td['embedder']}`. "
                     f"Details in `logs/tabledb_report.md`; data dictionaries and relationship maps in `tabledb/<docid>/`.")
            lj = td.get("llm_joins")
            if lj:
                L.append(f"- LLM join inference (suggester `{lj['suggester']}`, judge `{lj['judge']}`; {lj['documents']} document(s) evaluated, status {', '.join(lj['llm_status']) or '-'}): "
                         f"{lj['candidates']} candidate(s): {lj['confirmed']} confirmed (deterministic + LLM + judge), {lj['deterministic_only']} deterministic only, "
                         f"{lj['llm_only']} LLM only, {lj['disputed']} disputed, {lj['rejected']} rejected, {lj['uncertain']} uncertain. Statistics in `tabledb/<docid>/join_evaluation.md`.")
            L += [f"- **WARNING** {_md(w, 200)}" for w in td.get("warnings", [])] + [f"- **ERROR** {_md(e, 200)}" for e in td.get("errors", [])]
        L.append("")

    al = state.get("alerts", [])
    if al:
        L.append("### Warnings: paywalled documents\n")
        titles = {"no_local_copy": "NO LOCAL COPY", "new_edition_for_purchase": "NEW EDITION FOR PURCHASE",
                  "local_copy_outdated": "LOCAL COPY OUTDATED", "local_copy_unidentified": "local copy not identified",
                  "store_page_changed": "store page changed"}
        for a in sorted(al, key=lambda a: (a["severity"] != "warning", a["kind"], a["entry"])):
            tag = "**WARNING**" if a["severity"] == "warning" else "note"
            age = "NEW" if a["is_new"] else f"open since {a['first_seen'][:10]}, {a['runs']} runs"
            L.append(f"- {tag} [{titles[a['kind']]}] ({age}) {a['message']}")
        L.append("")

    vs = state.get("versions", [])
    if vs:
        behind = [v for v in vs if v["status"] != "current"]
        L.append(f"### Version status ({len(vs) - len(behind)} of {len(vs)} entries staged and current)\n")
        if behind:
            L.append("| entry | status | latest available | staged |\n|---|---|---|---|")
            for v in behind:
                eds = ", ".join(fmt_edition(k, x["edition"]) for k, x in v["staged"].get("editions", {}).items() if x.get("edition")) or "-"
                L.append(f"| {v['entry']} {_md(v['name'], 40)} | {v['status']} | "
                         f"{_md(', '.join(l['label'] for l in v['latest']) or '-', 60)} | {_md(eds, 50)} |")
        L.append("")

    cc = [c for c in state.get("config_changes", []) if c["path"] != "version_tracking"]
    vt = [c for c in state.get("config_changes", []) if c["path"] == "version_tracking"]
    if vt:
        L.append(f"_Version-tracking blocks refreshed in the configuration: {', '.join(c['entry'] for c in vt)}._\n")
    if cc:
        L.append("### Configuration changes\n")
        L.append("| entry | field | old | new | reason |\n|---|---|---|---|---|")
        for c in cc:
            L.append(f"| {c['entry']} | `{c['file']}:{_md(c['path'], 40)}` | {_md(c['old'])} | {_md(c['new'])} | "
                     f"{_md(c['reason'], 70)}{'' if c['auto'] else ' (manual edit)'} |")
        L.append("")

    docs = [a for a in state.get("doc_actions", []) if a["action"] in ("created", "updated")]
    if docs:
        L.append("### Document KGs created / updated\n")
        for a in docs:
            d = a["diff"]
            L.append(f"- **{a['action']}** {a['url']} (entries: {', '.join(a['entries'])}) — {a['detail']}")
            for k, label in (("sections_modified", "modified sections"), ("sections_added", "new sections"),
                             ("sections_removed", "removed sections")):
                if d.get(k):
                    L.append(f"    - {label}: {_md('; '.join(d[k]), 200)}")
            if "entities_added" in d:
                L.append(f"    - entities +{d['entities_added']} / -{d['entities_removed']}; "
                         f"nodes {d['nodes'][0]}→{d['nodes'][1]}, edges {d['edges'][0]}→{d['edges'][1]}")
            if a.get("archived"):
                L.append(f"    - previous version archived: `{a['archived']}`")
        L.append("")

    ent = [a for a in state.get("entry_actions", []) if a["action"] in ("created", "rebuilt")]
    if ent:
        L.append("### Entry KGs rebuilt\n")
        L += [f"- {a['entry']}: {a['action']} — {a['detail']}" + (f" (archived `{a['archived']}`)" if a.get("archived") else "")
              for a in ent]
        L.append("")

    fnd = [f for f in state.get("findings", []) if f["type"] not in ("baseline",)]
    if fnd:
        L.append("### Tracker findings\n")
        for f in fnd:
            mark = "applied" if f["applied"] or any(c["entry"] == f["entry"] and c["source"] == f["tracker_url"]
                                                    for c in cc) else "informational"
            if f["type"] == "new_edition" and mark != "applied":
                mark = "needs review"
            extra = ""
            if f["type"] == "version_bump":
                dd = f["details"]
                extra = f" sections: {_md(', '.join(dd.get('relevant_sections') or dd.get('changed_sections', [])[:8]), 100)}"
                if not dd.get("affects_entry", True):
                    mark = "not relevant to this entry"
            L.append(f"- [{mark}] {f['entry']} · {f['type']} · {f['identity']}: {f['old'] or ''} → {f['new'] or ''}"
                     f" — {_md(f['evidence'], 130)}{extra}")
        L.append("")

    if rt.pending:
        L.append("### Pending (needs human review)\n")
        L += [f"- {x['entry']} {x['identity']}: {x['old']} → {x['new']} (confidence {x['confidence']}, "
              f"first seen {x['first_seen']}) {x.get('new_url') or 'no link found'}" for x in rt.pending]
        L.append("")

    errs = state.get("errors", [])
    if errs:
        L.append("### Errors\n")
        L += [f"- {_md(e, 200)}" for e in errs]
        L.append("")
    stale = [t for t in state.get("tracker_log", []) if t.get("stale")]
    if stale:
        L.append("### Stale trackers (repeatedly failing)\n")
        L += [f"- {t['url']}" for t in stale]
        L.append("")
    L.append("---\n")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# Public API / CLI
# --------------------------------------------------------------------------


def run_once(settings: Settings, llm: Any = None) -> AgentState:
    """Run the whole agent once and return the final LangGraph state."""
    rt = Runtime(settings, llm)
    try:
        final = build_workflow(rt).invoke({})
    except BaseException:
        if not rt.dry and rt.p.lock.exists():
            rt.p.lock.unlink()
        raise
    return final


def _next_run(hhmm: str, now: Optional[datetime] = None) -> datetime:
    h, m = map(int, hhmm.split(":"))
    now = now or datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def daemon(settings: Settings, at: str) -> None:
    while True:
        nxt = _next_run(at)
        log.info("next run at %s", nxt.isoformat(timespec="minutes"))
        while datetime.now() < nxt:
            time.sleep(min(60, max(1, (nxt - datetime.now()).total_seconds())))
        try:
            st = run_once(settings)
            log.info("run complete: %s", st.get("summary"))
        except Exception:
            log.exception("run failed")


def probe(settings: Settings, url: str) -> None:
    """Run one tracker against ``url`` and print what it parsed (for verifying
    the trackers against the live sites)."""
    raw = {n: json.loads(Path(p).read_text()) for n, p in (("standards", settings.standards), ("reports", settings.reports))}
    entries, _ = parse_entries(raw)
    watchers = [e for e in entries if url in e.tracking_urls] or entries[:0]
    tr = T.tracker_for(url)
    print(f"tracker: {tr.name}; watchers: {[e.key for e in watchers]}")
    res = tr.check(url, None, watchers, T.TrackerContext(min_interval=0))
    print("error:", res.error or "-")
    print("state:", json.dumps(res.state.data, default=str)[:600])
    for e in watchers:
        print(e.key, [i.label + (f" ({i.edition})" if i.edition else "") for i in T.entry_identities(e)])
    for f in res.findings:
        print("finding:", f.model_dump_json())


def _local_target(rt: Runtime, e: DocEntry) -> Path:
    """The directory that receives local copies of ``e`` (the one ``local_files`` reads)."""
    if e.local_copy:
        pat = Path(e.local_copy).expanduser()
        if pat.is_dir() or not pat.suffix and not any(c in pat.name for c in "*?["):
            return pat
        raise ValueError(f"{e.key}: 'local_copy' is a file/glob ({e.local_copy}); replace the file there or point it at a directory")
    return rt.p.local / f"{e.kind}s" / str(e.id)


def add_local(settings: Settings, key: str, files: list[Path], move: bool = False, replace: bool = False,
              stage: bool = False) -> dict[str, Any]:
    """Import purchased copies of a paywalled entry into its local folder (and optionally stage them now).

    ``replace`` moves the files already there to ``<dir>/.superseded/<timestamp>/`` (never deleted, and ignored by
    the agent) so only the new edition is staged. Identical files are skipped."""
    rt = Runtime(settings.model_copy(update={"dry_run": False}))
    raw = {n: read_json(Path(getattr(settings, n)), {}) for n in ("standards", "reports")}
    entries, _ = parse_entries(raw)
    e = next((x for x in entries if x.key == key), None)
    if e is None:
        raise ValueError(f"unknown entry {key!r}")
    if not e.paywalled:
        log.warning("%s is not marked paywalled; the copy is staged in addition to its links", key)
    rt.entries = [e]
    dest = _local_target(rt, e)
    for f in files:
        f = Path(f).expanduser()
        if not f.is_file():
            raise ValueError(f"not a file: {f}")
        if f.suffix.lower() not in LOCAL_SUFFIXES:
            raise ValueError(f"{f.name}: unsupported type (use {', '.join(LOCAL_SUFFIXES)})")
        if f.stat().st_size == 0:
            raise ValueError(f"{f.name} is empty")
    dest.mkdir(parents=True, exist_ok=True)
    superseded, added, skipped = [], [], []
    if replace:
        old = local_files(rt, e)
        if old:
            stash = dest / ".superseded" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stash.mkdir(parents=True)
            for o in old:
                shutil.move(str(o), stash / o.name)
                superseded.append(o.name)
    for f in files:
        f = Path(f).expanduser()
        target = dest / f.name
        if target.exists() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(f.read_bytes()).digest():
            skipped.append(f.name)
            continue
        if target.exists():
            raise ValueError(f"{target} already exists with different content; use --replace or rename the file")
        (shutil.move if move else shutil.copy2)(str(f), str(target))
        added.append(f.name)
    result: dict[str, Any] = {"entry": key, "folder": str(dest), "added": added, "skipped_identical": skipped,
                              "superseded": superseded}
    if stage:
        st = run_once(settings.model_copy(update={"only": [key]}))
        result["alerts"] = [a for a in st.get("alerts", []) if a["entry"] == key]
        result["summary"] = st["summary"]
    return result


def list_local(settings: Settings) -> None:
    """Which paywalled entries have local copies, and which edition each states."""
    rt = Runtime(settings.model_copy(update={"dry_run": True}))
    raw = {n: read_json(Path(getattr(settings, n)), {}) for n in ("standards", "reports")}
    entries, _ = parse_entries(raw)
    for e in entries:
        if not e.paywalled:
            continue
        files = local_files(rt, e)
        print(f"{e.key:12} {e.name[:60]}")
        if not files:
            print(f"    (no local copy; put it in {_local_target(rt, e)}/)")
        for f in files:
            st = read_json(rt.p.doc(docid_of(f"file://{f.resolve()}")) / "state.json", {})
            eds = ", ".join(f"{k} {v}" for k, v in (st.get("identities") or {}).items() if v) or "not staged / edition unknown"
            print(f"    {f.name}  [{eds}]")


def status(settings: Settings) -> None:
    p = Paths(settings.home)
    print(f"{'document':18} {'kind':5} {'versions':>8} {'last changed':21} {'forced':>10} {'err':>3}  url")
    for d in sorted(p.docs.glob("*/state.json")):
        st = DocState(**read_json(d, {}))
        print(f"{d.parent.name:18} {st.content_kind:5} {st.versions:8} {st.last_changed:21} {st.last_forced[:10] or '-':>10} {st.consecutive_errors:3}  {st.url}")
    raw = {n: read_json(Path(getattr(settings, n)), {}) for n in ("standards", "reports")}
    print(f"\n{'entry':14} {'status':17} {'latest available':34} staged")
    for n in ("standards", "reports"):
        for item in raw[n].get(n, []):
            vt = item.get("version_tracking")
            key = f"{'standard' if n == 'standards' else 'report'}:{item['id']}"
            if not vt:
                print(f"{key:14} {'(not yet run)':17}")
                continue
            lat = ", ".join(l["label"] for l in vt["latest"]) or "-"
            eds = ", ".join(fmt_edition(k, v["edition"]) for k, v in vt["staged"].get("editions", {}).items() if v.get("edition")) or "-"
            print(f"{key:14} {vt['status']:17} {lat[:34]:34} {eds} ({len(vt['staged']['documents'])} doc)")
    orph = read_json(p.orphans, {})
    if orph:
        print("\nOrphaned staging data (tracked in state/orphans.json):")
        for k, o in orph.items():
            print(f"  {o['status']:9} {k}  first orphaned {o['first_orphaned'][:10]}" + (f"  -> {o['archive']}" if o.get("archive") else ""))
    alerts = read_json(p.state / "alerts.json", {})
    if alerts:
        print("\nOpen alerts:")
        for a in alerts.values():
            print(f"  [{a['severity']}] {a['kind']}: {a['message']}")
    n = len(list((p.home / 'staging').glob('*/*/kg.json'))) - len(list(p.docs.glob('*/kg.json')))
    print(f"\nentry KGs: {n}; config changes logged: "
          f"{sum(1 for _ in p.changes.open()) if p.changes.exists() else 0}; report: {p.report}")


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Keep per-standard / per-report knowledge graphs up to date.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--standards", default="config/standards.json")
    common.add_argument("--reports", default="config/reports.json")
    common.add_argument("--home", default="kg_agent_home", help="staging / archive / logs root")
    common.add_argument("--extractor", choices=["rules", "ollama", "hybrid"], default="rules")
    common.add_argument("--model", help="Ollama model for triple extraction")
    common.add_argument("--ollama-host")
    common.add_argument("--llm", help="Ollama model used only to narrate the report")
    common.add_argument("--min-interval", type=float, default=1.0)
    common.add_argument("--no-auto-apply", action="store_true", help="never edit the configuration; list as pending")
    common.add_argument("--no-assets", action="store_true", help="skip figure/table export")
    common.add_argument("--only", help="comma-separated entries, e.g. standard:22,report:1")
    common.add_argument("--keep-archive", type=int, default=0)
    common.add_argument("--local-dir", help="where purchased copies of paywalled documents go "
                        "(<dir>/<standards|reports>/<id>/; default <home>/local)")
    common.add_argument("--fetch", choices=kg_ingest.FETCH_MODES)
    common.add_argument("--pdf-engine", choices=kg_ingest.PDF_ENGINES)
    common.add_argument("--ocr", choices=["auto", "off", "force"], help="PDF OCR: auto = only pages without a text layer (default)")
    common.add_argument("--ocr-engine", choices=["auto", "tesseract", "rapidocr"])
    common.add_argument("--ocr-lang", help="OCR language(s), e.g. eng or eng+deu")
    common.add_argument("-v", "--verbose", action="store_true")
    r = sub.add_parser("run", parents=[common], help="run once")
    r.add_argument("--dry-run", action="store_true", help="check only; write nothing, print the report")
    r.add_argument("--tables", action="store_true", help="after staging, build / refresh the table database (PostgreSQL + pgvector; kg_tabledb.py)")
    r.add_argument("--tabledb-dsn", help="postgresql://user:pass@host/db (default $COGENT_TABLEDB_DSN)")
    r.add_argument("--tabledb-schema", default="kgtables")
    r.add_argument("--embedder", choices=["ollama", "hash"], default="ollama", help="embedder for table rows (Ollama model, or 'hash' for offline tests)")
    r.add_argument("--embed-model", help="Ollama embedding model (default nomic-embed-text)")
    r.add_argument("--no-kg-context", action="store_true", help="do not use the KG neighbourhood of a table in the embedded text")
    r.add_argument("--row-mode", choices=["full", "text"], default="full", help="full: embed every cell of a row (default); text: leave numeric values out")
    r.add_argument("--llm-joins", action="store_true", help="with --tables: also infer joins with an Ollama LLM and evaluate all candidates with an LLM judge")
    r.add_argument("--join-model", help="Ollama chat model of the join suggester (default llama3.1:8b)")
    r.add_argument("--join-judge-model", help="Ollama chat model of the judge (default: the suggester)")
    r.add_argument("--join-samples", type=int, default=3, help="independent suggestion runs per table pair")
    r.add_argument("--join-judge-runs", type=int, default=3, help="judge votes per candidate")
    r.add_argument("--join-max-pairs", type=int, default=40)
    r.add_argument("--join-max-seconds", type=float, help="time budget per document for the LLM stage")
    r.add_argument("--no-prune", action="store_true", help="with --force: do not purge stale asset files or archive orphaned staging data")
    r.add_argument("--force", action="store_true", help="forced update: re-read every tracking page and source document directly "
                   "(no conditional requests, no \"unchanged\" short-cut), rebuild the KGs even if nothing changed, pick up "
                   "the latest edition if one is available, and log/report it all (logs/forced.jsonl). Combine with --only to limit it")
    d = sub.add_parser("daemon", parents=[common], help="simple foreground loop: run every day (the full daemon with "
                       "catch-up, retries, status and systemd support is src/kg_daemon.py)")
    d.add_argument("--at", default="06:00", help="local time HH:MM")
    pr = sub.add_parser("probe", parents=[common], help="run a tracker on one URL and print what it parsed")
    pr.add_argument("url")
    sub.add_parser("status", parents=[common], help="show staged documents")
    sub.add_parser("local", parents=[common], help="list paywalled entries and their local copies")
    al = sub.add_parser("add-local", parents=[common], help="import purchased copies of a paywalled entry")
    al.add_argument("entry", help="e.g. standard:30")
    al.add_argument("files", nargs="+", type=Path)
    al.add_argument("--move", action="store_true", help="move instead of copy")
    al.add_argument("--replace", action="store_true", help="set aside the copies already there (kept in .superseded/)")
    al.add_argument("--stage", action="store_true", help="stage the entry right away and show its warnings")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if a.fetch:
        kg_ingest.set_fetch_mode(a.fetch)
    if a.pdf_engine:
        kg_ingest.set_pdf_engine(a.pdf_engine)
    kg_ingest.set_ocr(a.ocr, a.ocr_engine, a.ocr_lang)
    s = Settings(home=a.home, standards=a.standards, reports=a.reports, extractor=a.extractor, model=a.model,
                 ollama_host=a.ollama_host, llm=a.llm, min_interval=a.min_interval,
                 auto_apply=not a.no_auto_apply, assets=not a.no_assets, keep_archive=a.keep_archive,
                 local_dir=Path(a.local_dir) if a.local_dir else None,
                 only=[x for x in (a.only or "").split(",") if x], dry_run=getattr(a, "dry_run", False),
                 force=getattr(a, "force", False), prune=not getattr(a, "no_prune", False),
                 tables=getattr(a, "tables", False), tabledb_dsn=getattr(a, "tabledb_dsn", None), tabledb_schema=getattr(a, "tabledb_schema", "kgtables"),
                 embedder=getattr(a, "embedder", "ollama"), embed_model=getattr(a, "embed_model", None), kg_context=not getattr(a, "no_kg_context", False),
                 row_mode=getattr(a, "row_mode", "full"), llm_joins=getattr(a, "llm_joins", False), join_model=getattr(a, "join_model", None),
                 join_judge_model=getattr(a, "join_judge_model", None), join_samples=getattr(a, "join_samples", 3), join_judge_runs=getattr(a, "join_judge_runs", 3),
                 join_max_pairs=getattr(a, "join_max_pairs", 40), join_max_seconds=getattr(a, "join_max_seconds", None))
    if a.cmd == "run":
        st = run_once(s)
        print(st["report"] if s.dry_run else f"report appended to {Paths(s.home).report}\n{st['summary']}")
    elif a.cmd == "daemon":
        daemon(s, a.at)
    elif a.cmd == "probe":
        probe(s, a.url)
    elif a.cmd == "status":
        status(s)
    elif a.cmd == "local":
        list_local(s)
    elif a.cmd == "add-local":
        try:
            res = add_local(s, a.entry, a.files, a.move, a.replace, a.stage)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}")
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
