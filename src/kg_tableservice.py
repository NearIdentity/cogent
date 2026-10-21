"""Query front-end over the table database for the MCP server: ref resolution, paywall policy, provenance, embedder handling, and
**per-session / per-task tailoring** of how joins and units are deduced and how rows are retrieved.

``kg_tabledb.TableDB`` does the SQL; this layer decides *what may be returned* and *how it is labelled*:

* ``ref`` is an entry key ('standard:22') or a document id; entries resolve to their documents through the staged ``meta.json``;
* tables from paywalled / purchased documents follow the server's ``--paywalled-assets`` policy: ``block`` removes them from every
  result, ``watermark`` (default) and ``allow`` return them with warnings and a copyright notice (text cannot be watermarked);
* the question is embedded with the same embedder the database was built with; if that embedder is unreachable or differs, the
  search falls back to lexical matching and says so rather than mixing vector spaces.

Tailoring (nothing is recomputed: the database stores *all* join candidates and *all* unit hypotheses with their evidence, and these
options choose among them at query time):

=============  =======================================================  ====================================================
option         values                                                   effect
=============  =======================================================  ====================================================
approach       deterministic | llm | hybrid | consensus                 which join candidates count (see kg_llmjoin.select)
min_score      0..1                                                     score floor for joins
include_rejected  bool                                                  also list joins the judge rejected / disputed (flagged)
min_agreement  0..1                                                     LLM approach: share of suggestion runs that proposed it
units          strict | broad | off                                     narrow (explicit + declared) vs diverse (inferred) unit deduction
retrieval      semantic | lexical | hybrid                              how lookup_values / search_tables match the question
=============  =======================================================  ====================================================

Resolution order, last wins: server default (profile ``balanced``) < the session's saved preferences < a ``profile`` named in the
call < explicit arguments of the call. A *session* is the MCP session when the server runs stateful, otherwise the authenticated
token plus a ``session_id`` the client chooses (``default`` if it gives none).
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Optional

import kg_llmjoin as L
import kg_relmap as R
import kg_tabledb as TDB
import kg_units as U
from kg_assets import notice
from kg_embed import Embedder, EmbedError
from kg_llm import LLMError, get_llm
from kg_store import NotFound, Store

log = logging.getLogger("kg_tableservice")
DISCLAIMER = ("Tables were extracted automatically from the source documents; cells can be merged, split or mis-read, units may be missing or inferred, "
              "and column types and join candidates are inferred. Verify design values against the source document before use.")
RETRIEVAL = ("semantic", "lexical", "hybrid")
SESSION_TTL = 8 * 3600


@dataclass
class Prefs:
    profile: str = "balanced"
    approach: str = "hybrid"
    min_score: float = 0.55
    include_rejected: bool = False
    min_agreement: float = 0.0
    units: str = "strict"
    retrieval: str = "semantic"

    def validate(self) -> "Prefs":
        if self.approach not in L.APPROACHES:
            raise NotFound(f"approach must be one of {L.APPROACHES}")
        if self.units not in U.MODES:
            raise NotFound(f"units must be one of {U.MODES}")
        if self.retrieval not in RETRIEVAL:
            raise NotFound(f"retrieval must be one of {RETRIEVAL}")
        if not 0 <= self.min_score <= 1 or not 0 <= self.min_agreement <= 1:
            raise NotFound("min_score and min_agreement must be between 0 and 1")
        return self


FIELDS = ("approach", "min_score", "include_rejected", "min_agreement", "units", "retrieval")


def _overlay(base: dict[str, Any], profile: Optional[str], overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if profile:
        if profile not in L.PROFILES:
            raise NotFound(f"unknown profile {profile!r}; profiles: {sorted(L.PROFILES)}")
        out.update({k: v for k, v in L.PROFILES[profile].items() if k in FIELDS})
        out["profile"] = profile
    out.update({k: v for k, v in overrides.items() if v is not None and k in FIELDS})
    if not profile and overrides.get("profile") in L.PROFILES:      # a saved session overlay remembers its profile name
        out["profile"] = overrides["profile"]
    return out


class Sessions:
    """Per-session preference overlays kept in memory (they are lost on restart; clients can set them again)."""

    def __init__(self, ttl: float = SESSION_TTL):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key: str) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            for k in [k for k, (_, exp) in self._d.items() if exp < now]:
                del self._d[k]
            return dict(self._d.get(key, ({}, 0))[0])

    def set(self, key: str, prefs: dict[str, Any]) -> None:
        with self._lock:
            self._d[key] = (dict(prefs), time.time() + self.ttl)

    def clear(self, key: str) -> bool:
        with self._lock:
            return self._d.pop(key, None) is not None


class TableService:
    def __init__(self, store: Store, dsn: Optional[str], schema: str = TDB.SCHEMA, embedder: Optional[Embedder] = None, allow_sql: bool = False,
                 llm_model: Optional[str] = None, judge_model: Optional[str] = None, llm_host: Optional[str] = None, llm_max_seconds: float = 180.0,
                 default_profile: str = "balanced"):
        self.store, self.db, self.embedder, self.allow_sql = store, (TDB.TableDB(dsn, schema) if dsn else None), embedder, allow_sql
        self.llm_model, self.judge_model, self.llm_host, self.llm_max_seconds = llm_model, judge_model, llm_host, llm_max_seconds
        self.sessions, self.default_profile, self._spec = Sessions(), default_profile, None
        if default_profile not in L.PROFILES:
            raise ValueError(f"unknown profile {default_profile!r}")

    # ----------------------------------------------------------------------------------------------- preferences
    def defaults(self) -> dict[str, Any]:
        return _overlay(asdict(Prefs()), self.default_profile, {})

    def resolve(self, session: str, profile: Optional[str] = None, **overrides: Any) -> Prefs:
        """Effective preferences for a call: defaults < session < ``profile`` < explicit arguments."""
        eff = _overlay(self.defaults(), None, self.sessions.get(session))
        eff = _overlay(eff, profile, overrides)
        return Prefs(**{k: eff[k] for k in ("profile", *FIELDS)}).validate()

    def set_session(self, session: str, profile: Optional[str] = None, **overrides: Any) -> dict[str, Any]:
        """Save preferences for the session (merged into what is already saved) and return the effective set."""
        cur = self.sessions.get(session)
        new = _overlay(cur, profile, overrides)
        Prefs(**{**asdict(Prefs()), **_overlay(self.defaults(), None, new)}).validate()
        self.sessions.set(session, new)
        return {"session": session, "saved": new, "effective": asdict(self.resolve(session)), "expires_in_hours": SESSION_TTL / 3600}

    def reset_session(self, session: str) -> dict[str, Any]:
        return {"session": session, "cleared": self.sessions.clear(session), "effective": asdict(self.resolve(session))}

    def profiles(self) -> dict[str, Any]:
        return {"default": self.default_profile, "profiles": {k: {f: v[f] for f in (*FIELDS, "description") if f in v} for k, v in L.PROFILES.items()},
                "approaches": {"deterministic": "joins proposed by the heuristics (names + values + KG); needs no LLM evidence",
                               "llm": "joins suggested by the LLM (agreement across runs) that the judge did not reject",
                               "hybrid": "deterministic and LLM-suggested joins, minus those the judge rejected",
                               "consensus": "proposed by both methods and accepted by the judge: strictest"},
                "units": {"strict": "narrow: only explicit units (header, cells, header phrase) and declarations in the caption / introduction / notes; a conflict gives no unit",
                          "broad": "diverse: also units mentioned in the section, KG entities, the document's dominant unit and the customary unit of the quantity, flagged inferred, with alternatives",
                          "off": "no deduced unit: only what the header or the cells say explicitly"},
                "retrieval": {"semantic": "vector similarity", "lexical": "full-text match (exact words and codes)", "hybrid": "both, fused by reciprocal rank"},
                "llm_inference_on_request": bool(self.llm_model)}

    # ------------------------------------------------------------------------------------------------ plumbing
    @property
    def has_db(self) -> bool:
        return self.db is not None

    def health(self) -> dict[str, Any]:
        """Status of the table database for ``get_overview`` and the start-up log (never raises)."""
        if self.db is None:
            return {"configured": False}
        h = self.db.health()
        h.pop("schema", None)
        return h

    @contextmanager
    def conn(self):
        if self.db is None:
            raise NotFound("no table database is configured on this server (--tabledb-dsn)")
        try:
            c = self.db.connect()
        except TDB.TableDBError as exc:
            raise NotFound(str(exc)) from None
        except Exception as exc:                                       # connection refused, bad password, ...
            raise NotFound(f"table database not reachable: {type(exc).__name__}: {exc}") from None
        try:
            if not self.db._exists(c, "kgt_tables"):
                raise NotFound("the table database is empty: run `python src/kg_tabledb.py build` (or `kg_agent.py run --tables`)")
            yield c
        finally:
            c.rollback()
            c.close()

    def _docids(self, c, ref: Optional[str]) -> list[str]:
        """Documents in scope for ``ref`` that the paywall policy lets us serve (never None: an explicit list)."""
        if ref:
            base = self.store._entry_docs(ref) if ":" in ref else [ref]
        else:
            base = [r[0] for r in c.execute("SELECT DISTINCT docid FROM kgt_tables ORDER BY docid").fetchall()]
        if self.store.paywalled_assets == "block":
            base = [d for d in base if not self.store.doc_paywall(d)["paywalled"]]
        return base

    def _allowed(self, c, table_id: str) -> str:
        docid = table_id.split("/")[0]
        if docid not in self._docids(c, docid):
            raise NotFound(f"table {table_id!r} comes from paywalled content and the server is configured not to serve it")
        return docid

    def _label(self, c, docid: str, cache: dict) -> dict[str, Any]:
        if docid not in cache:
            pw = self.store.doc_paywall(docid)
            cache[docid] = {"paywalled": pw["paywalled"], "warnings": self.store._asset_warnings(docid, pw) if pw["paywalled"] else [], "holder": pw["holder"]}
        return cache[docid]

    def _decorate(self, c, t: dict[str, Any], cache: dict) -> dict[str, Any]:
        lab = self._label(c, t["docid"], cache)
        src = t.get("source_url") or ""
        cfg = self.store._config()
        title = TDB.document_title(t.get("doc_label") or "", [cfg[k].get("document") or cfg[k].get("report") or "" for k in (t.get("entries") or []) if k in cfg], t["docid"])
        t = {**t, "doc_label": title, "paywalled": lab["paywalled"],
             "source": {"document_id": t["docid"], "document": title, "url": "local:" + src.rsplit("/", 1)[-1] if src.startswith("file://") else src,
                        "page": t.get("page"), "caption": t.get("caption"), "section": t.get("section_path")}}
        for k in ("source_url", "pg_table"):
            t.pop(k, None)
        return t

    def _wrap(self, c, payload: dict[str, Any], cache: dict, docids: set[str], prefs: Optional[Prefs] = None) -> dict[str, Any]:
        warn, paywalled, holder = [], False, None
        for d in sorted(docids):
            lab = self._label(c, d, cache)
            if lab["paywalled"]:
                paywalled, holder = True, lab["holder"]
                warn += [w for w in lab["warnings"] if w not in warn]
        meta = self.db.meta(c) or {}
        out = {**payload, "provenance": {"derived_from": "tables extracted from the staged documents", "embedder": meta.get("spec"), "recipe": meta.get("recipe"),
                                         "disclaimer": DISCLAIMER, **({"preferences": asdict(prefs)} if prefs else {})}}
        if paywalled:
            out["paywalled"] = True
            out["copyright_notice"] = notice(holder)
            out["warnings"] = warn
        return out

    def _qvec(self, c, question: str, prefs: Prefs) -> tuple[Optional[list[float]], Optional[str]]:
        if prefs.retrieval == "lexical":
            return None, None
        if self.embedder is None:
            return None, "no embedder configured on the server: lexical matching only"
        try:
            meta = self.db.meta(c) or {}
            spec = self._spec or self.embedder.spec
            if meta.get("spec") and meta["spec"] != spec:
                return None, (f"the database was built with embedder {meta['spec']!r} but the server uses {spec!r}; "
                              "vector spaces differ, so lexical matching was used instead")
            self._spec = spec
            return self.embedder.embed_query(question), None
        except EmbedError as exc:
            return None, f"embedder unavailable ({exc}); lexical matching was used instead"

    # ------------------------------------------------------------------------------------------------ units
    @staticmethod
    def _unit_view(header: str, cands: list[dict[str, Any]], explicit: Optional[str], mode: str, detail: bool) -> dict[str, Any]:
        d = U.decide(cands, mode) if cands else {"unit": None, "confidence": 0.0, "source": None, "inferred": False, "conflict": False, "evidence": []}
        unit = d["unit"] or (explicit if mode == "off" and explicit and not cands else None)
        view: dict[str, Any] = {"unit": unit, "mode": mode}
        if unit:
            view.update(confidence=d["confidence"], source=d["source"], inferred=d["inferred"])
        if d.get("conflict"):
            view.update(conflict=True, note="conflicting unit declarations; none chosen in strict mode (use units=broad to see the alternatives)")
        if detail:
            view.update(evidence=d["evidence"], **({"alternatives": d["alternatives"]} if d.get("alternatives") else {}))
        return view

    def _row_units(self, c, rows_by_table: dict[str, list[Any]], prefs: Prefs) -> dict[str, dict[str, Any]]:
        """{table_id: {"units": {header: unit}, "unit_notes": [...]}} for the numeric columns of the tables, per the units mode."""
        if prefs.units == "off" and not rows_by_table:
            return {}
        out = {}
        for tid, cols in self.db.column_units(c, list(rows_by_table)).items():
            units, notes = {}, []
            for col in cols:
                v = self._unit_view(col["header"], col["candidates"], col["unit"], prefs.units, False)
                if v["unit"]:
                    units[col["header"]] = v["unit"]
                    if v.get("inferred"):
                        notes.append(f"{col['header']}: unit {v['unit']} is inferred (confidence {v['confidence']}, from {v['source']}); check the source")
                elif v.get("conflict"):
                    notes.append(f"{col['header']}: {v['note']}")
            out[tid] = {"units": units, **({"unit_notes": notes} if notes else {})}
        return out

    # ------------------------------------------------------------------------------------------------ tools
    def list_tables(self, ref: Optional[str] = None, query: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
        with self.conn() as c:
            docs, cache = self._docids(c, ref), {}
            tabs = [self._decorate(c, t, cache) for t in self.db.list_tables(c, docs, query, max(1, min(limit, 200)))]
            return self._wrap(c, {"ref": ref, "tables": tabs, "count": len(tabs)}, cache, {t["docid"] for t in tabs})

    def describe_table(self, table_id: str, sample_rows: int = 5, prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            docid, cache = self._allowed(c, table_id), {}
            try:
                t = self.db.describe(c, table_id, max(0, min(sample_rows, 50)))
            except TDB.TableDBError as exc:
                raise NotFound(str(exc)) from None
            for col in t["columns"]:
                cands = col.pop("unit_candidates", None) or []
                if col["pg_type"] != "text":
                    v = self._unit_view(col["header"], cands, col.get("unit"), prefs.units, detail=prefs.units == "broad")
                    col["unit"], col["unit_deduction"] = v["unit"], v
                    col.pop("unit_source", None), col.pop("unit_confidence", None)
            return self._wrap(c, {"table": self._decorate(c, t, cache)}, cache, {docid}, prefs)

    def explain_units(self, table_id: str, column: Optional[str] = None, prefs: Optional[Prefs] = None) -> dict[str, Any]:
        """Every unit hypothesis of the numeric column(s) with its evidence, and what strict / broad / off would answer."""
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            docid = self._allowed(c, table_id)
            cols = self.db.column_units(c, [table_id]).get(table_id)
            if cols is None:
                raise NotFound(f"unknown table {table_id!r}")
            if column:
                cols = [x for x in cols if column.lower() in (x["header"].lower(), x["name"].lower())]
                if not cols:
                    raise NotFound(f"no numeric column {column!r} in {table_id}")
            out = [{"header": x["header"], "column": x["name"], "stored_unit": x["unit"], "candidates": x["candidates"],
                    "strict": self._unit_view(x["header"], x["candidates"], x["unit"], "strict", True), "broad": self._unit_view(x["header"], x["candidates"], x["unit"], "broad", True),
                    "off": self._unit_view(x["header"], x["candidates"], x["unit"], "off", True)} for x in cols]
            return self._wrap(c, {"table_id": table_id, "active_mode": prefs.units, "columns": out,
                                  "note": "units are labels with provenance; no value was converted"}, {}, {docid}, prefs)

    def search_tables(self, question: str, ref: Optional[str] = None, k: int = 5, prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            docs, cache = self._docids(c, ref), {}
            qv, note = self._qvec(c, question, prefs)
            k = max(1, min(k, 50))
            tabs = self.db.search_tables(c, qv, question, docs, k)
            if prefs.retrieval == "hybrid" and qv is not None:           # add exact-word matches the vectors missed
                have = {t["table_id"] for t in tabs}
                tabs += [t for t in self.db.search_tables(c, None, question, docs, k) if t["table_id"] not in have][:k]
            tabs = [self._decorate(c, t, cache) for t in tabs]
            return self._wrap(c, {"question": question, "mode": "hybrid" if (prefs.retrieval == "hybrid" and qv) else "semantic" if qv else "lexical",
                                  **({"note": note} if note else {}), "tables": tabs}, cache, {t["docid"] for t in tabs}, prefs)

    def lookup_values(self, question: str, ref: Optional[str] = None, table_ids: Optional[list[str]] = None, k: int = 10, candidate_tables: int = 5,
                      prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            docs, cache = self._docids(c, ref), {}
            for t in table_ids or []:
                self._allowed(c, t)
            qv, note = self._qvec(c, question, prefs)
            res = self.db.lookup(c, qv, question, docs, table_ids, max(1, min(k, 50)), max(1, min(candidate_tables, 20)), hybrid=prefs.retrieval == "hybrid")
            tabs = {t["table_id"]: self._decorate(c, t, cache) for t in res["tables"]}
            hit_tables = {h["table_id"] for h in res["rows"] if h["table_id"] in tabs}
            units = self._row_units(c, {t: [] for t in hit_tables}, prefs) if prefs.units != "off" or hit_tables else {}
            rows = [{**h, "table": {k2: tabs[h["table_id"]][k2] for k2 in ("caption", "source", "paywalled")}, **units.get(h["table_id"], {})} for h in res["rows"] if h["table_id"] in tabs]
            return self._wrap(c, {"question": question, "mode": res["mode"], **({"note": note} if note else {}), "rows": rows,
                                  "tables_searched": [{"table_id": t["table_id"], "caption": t["caption"], **({"similarity": t["similarity"]} if "similarity" in t else {})}
                                                      for t in tabs.values()]}, cache, {tabs[t]["docid"] for t in hit_tables}, prefs)

    def query_table(self, table_id: str, where: Optional[list[dict[str, Any]]] = None, columns: Optional[list[str]] = None, order_by: Optional[str] = None,
                    descending: bool = False, limit: int = 20, prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            docid, cache = self._allowed(c, table_id), {}
            try:
                res = self.db.query_table(c, table_id, where, columns, order_by, descending, limit)
            except TDB.TableDBError as exc:
                raise NotFound(str(exc)) from None
            info = self._decorate(c, self.db.describe(c, table_id, 0), cache)
            units = self._row_units(c, {table_id: []}, prefs).get(table_id, {})
            return self._wrap(c, {**res, **units, "table": {k: info[k] for k in ("caption", "source", "paywalled")}}, cache, {docid}, prefs)

    def join_tables(self, left_table: str, right_table: str, left_column: Optional[str] = None, right_column: Optional[str] = None, limit: int = 20,
                    prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            d1, d2, cache = self._allowed(c, left_table), self._allowed(c, right_table), {}
            used = None
            if not (left_column and right_column):                       # use the inferred relationship between the two tables
                rels = [r for r in self.db.relationships(c, [d1], prefs.approach, prefs.min_score, prefs.include_rejected, prefs.min_agreement)
                        if {r["left_table"], r["right_table"]} == {left_table, right_table}]
                if not rels:
                    raise NotFound(f"no inferred join between these tables with approach={prefs.approach}, min_score={prefs.min_score}; pass left_column and right_column "
                                   "explicitly, or loosen the approach (profile 'exploratory')")
                r = rels[0]
                used = {"approach": prefs.approach, "score": r["score"], "status": r["status"], "methods": r["methods"], "rejected": r["rejected"],
                        "cardinality": r["cardinality"], "evidence": r["evidence"]}
                if r["left_table"] == left_table:
                    left_column, right_column = r["left_column"], r["right_column"]
                else:
                    left_column, right_column = r["right_column"], r["left_column"]
            try:
                res = self.db.join(c, left_table, right_table, left_column, right_column, None, limit)
            except TDB.TableDBError as exc:
                raise NotFound(str(exc)) from None
            return self._wrap(c, {**res, **({"inferred_relationship": used} if used else {})}, cache, {d1, d2}, prefs)

    # ---------------------------------------------------------------------- dictionaries / relationships (published files)
    def _evaluation(self, ref: str) -> list[dict[str, Any]]:
        """join_evaluation.json of each document of ``ref`` (policy applied); documents published before the LLM stage fall back to relationships.json."""
        try:
            return self.store._published(ref, "join_evaluation.json", False)
        except NotFound:
            return []

    def _apply(self, d: dict[str, Any], cands: Optional[list[dict[str, Any]]], fallback_rels: list[dict[str, Any]], prefs: Prefs) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The dictionary and relationship list of a document for the preferences: joins by approach, units by mode."""
        if cands is not None:
            rels = L.candidates_to_relations(L.select(cands, prefs.approach, prefs.min_score, prefs.include_rejected, prefs.min_agreement))
        else:
            rels = [r for r in fallback_rels if r.get("kind") == "join"]
        tables = []
        for t in d["tables"]:
            cols = []
            for col in t["columns"]:
                col = {k: v for k, v in col.items() if k != "joins"}
                cu = col.pop("unit_candidates", None)
                if col["pg_type"] != "text" and cu is not None:
                    v = self._unit_view(col["header"], cu, col.get("unit"), prefs.units, detail=prefs.units == "broad")
                    col["unit"], col["unit_deduction"] = v["unit"], v
                    col.pop("unit_source", None), col.pop("unit_confidence", None)
                cols.append(col)
            tables.append({**t, "columns": cols})
        out = R.data_dictionary(d["document_id"], d["document"], d["source"], tables, rels, d.get("paywalled", False))
        out.update({k: d[k] for k in ("generated", "embedder") if k in d}, approach=prefs.approach, units_mode=prefs.units)
        return out, rels

    def _docs(self, ref: str, prefs: Prefs, want_md: bool = False) -> list[dict[str, Any]]:
        dicts = self.store._published(ref, "data_dictionary.json", False)
        evals = {e["document_id"]: e for e in self._evaluation(ref)}
        out = []
        for item in dicts:
            ev = evals.get(item["document_id"])
            rel_file = None
            if ev is None:
                try:
                    rel_file = next((r for r in self.store._published(item["document_id"], "relationships.json", False)), None)
                except NotFound:
                    rel_file = None
            d, rels = self._apply(item["content"], ev["content"]["candidates"] if ev else None, (rel_file or {}).get("content", {}).get("relationships", []), prefs)
            it = {k: v for k, v in item.items() if k != "content"}
            it["content"] = R.dictionary_markdown(d, rels) if want_md else d
            it["_rels"], it["_dict"] = rels, d
            it["evaluation_available"] = ev is not None
            out.append(it)
        return out

    def data_dictionary(self, ref: str, fmt: str, prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        if fmt not in ("json", "markdown"):
            raise NotFound("format must be 'json' or 'markdown'")
        docs = self._docs(ref, prefs, fmt == "markdown")
        for d in docs:
            d.pop("_rels"), d.pop("_dict")
        return {"ref": ref, "format": fmt, "preferences": asdict(prefs), "documents": docs}

    def relationship_map(self, ref: str, fmt: str, prefs: Optional[Prefs] = None) -> dict[str, Any]:
        prefs = prefs or self.resolve("default")
        if fmt not in ("json", "mermaid"):
            raise NotFound("format must be 'json' or 'mermaid'")
        docs = self._docs(ref, prefs)
        out = []
        for d in docs:
            rels, dd = d.pop("_rels"), d.pop("_dict")
            d.pop("content")
            d["content"] = R.relationships_mermaid(dd, rels) if fmt == "mermaid" else {"document_id": d["document_id"], "approach": prefs.approach, "min_score": prefs.min_score,
                                                                                  "include_rejected": prefs.include_rejected, "relationships": rels}
            out.append(d)
        return {"ref": ref, "format": fmt, "preferences": asdict(prefs), "documents": out}

    def join_evaluation(self, ref: str, fmt: str = "json") -> dict[str, Any]:
        """The LLM-as-judge evaluation published for the documents: every candidate with its verdicts, plus the agreement statistics."""
        if fmt not in ("json", "markdown"):
            raise NotFound("format must be 'json' or 'markdown'")
        items = self.store._published(ref, "join_evaluation.md" if fmt == "markdown" else "join_evaluation.json", fmt == "markdown")
        return {"ref": ref, "format": fmt, "documents": items}

    # ------------------------------------------------------------------------------------- on-request LLM evaluation
    def evaluate_joins(self, ref: str, prefs: Optional[Prefs] = None, samples: int = 2, judge_runs: int = 3, max_pairs: int = 20,
                       suggester_model: Optional[str] = None, judge_model: Optional[str] = None, max_seconds: Optional[float] = None) -> dict[str, Any]:
        """Run suggest -> judge -> reconcile now, with the caller's parameters, for the documents of ``ref`` (at most 3). The result is
        returned and not stored; it needs ``--llm-model`` on the server."""
        if not self.llm_model:
            raise NotFound("LLM join evaluation on request is not enabled: start the server with --llm-model (and an Ollama server)")
        prefs = prefs or self.resolve("default")
        with self.conn() as c:
            docs = self._docids(c, ref)
        if not docs:
            raise NotFound(f"no servable documents for {ref!r}")
        cfg = L.Config(samples=max(1, min(samples, 5)), judge_runs=max(1, min(judge_runs, 5)), max_pairs=max(1, min(max_pairs, 60)),
                       max_seconds=min(max_seconds or self.llm_max_seconds, 600.0))
        suggester = get_llm("ollama", suggester_model or self.llm_model, self.llm_host)
        judge = get_llm("ollama", judge_model or self.judge_model or suggester.model, self.llm_host)
        out, cache = [], {}
        for docid in docs[:3]:
            recs = TDB.load_tables(self.store.home, docid)
            if len(recs) < 2:
                continue
            tables = [R.T(r.table_id, r.src.asset_id, r.cols, r.rows, r.ctx) for r in recs]
            ev = L.evaluate(tables, R.infer(tables), suggester, judge, cfg)
            sel = L.candidates_to_relations(L.select(ev["candidates"], prefs.approach, prefs.min_score, prefs.include_rejected, prefs.min_agreement))
            with self.conn() as c:
                lab = self._label(c, docid, cache)
            out.append({"document_id": docid, "paywalled": lab["paywalled"], "relationships": sel, "evaluation": {k: ev[k] for k in ("metrics", "status", "models", "config", "judge_note")},
                        "candidates": [{k: x[k] for k in ("id", "left_table", "left_column", "right_table", "right_column", "status", "final_score", "methods", "judge", "llm", "deterministic")}
                                       for x in ev["candidates"]]})
        if not out:
            raise NotFound("the documents have fewer than two tables: nothing to join")
        with self.conn() as c:
            return self._wrap(c, {"ref": ref, "ephemeral": True, "note": "computed now with the parameters you gave; not stored", "documents": out}, cache, set(docs[:3]), prefs)

    def run_sql(self, query: str, limit: int = 100) -> dict[str, Any]:
        if not self.allow_sql:
            raise NotFound("run_sql is disabled; start the server with --allow-sql (and a read-only database role)")
        with self.conn() as c:
            if self.store.paywalled_assets == "block" and c.execute("SELECT 1 FROM kgt_tables WHERE paywalled LIMIT 1").fetchone():
                raise NotFound("run_sql is unavailable while paywalled tables are in the database and the policy is 'block'")
            try:
                res = self.db.run_sql(c, query, limit)
            except TDB.TableDBError as exc:
                raise NotFound(str(exc)) from None
            except Exception as exc:
                raise NotFound(f"SQL error: {str(exc).splitlines()[0]}") from None
            cache: dict = {}
            return self._wrap(c, {**res, "count": len(res["rows"])}, cache, {r[0] for r in c.execute("SELECT DISTINCT docid FROM kgt_tables WHERE paywalled").fetchall()})
