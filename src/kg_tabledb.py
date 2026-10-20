#!/usr/bin/env python3
"""Table database agent: the tables extracted from the staged documents -> PostgreSQL + pgvector.

Runs after the KG agent has finished staging (``kg_agent.py run --tables``, or on its own: ``kg_tabledb.py build``).
For every table CSV exported next to a document KG (``staging/documents/<id>/assets/<hash>/tables/*.csv``):

1. the data rows become a database table (typed columns, original text kept in ``_raw``);
2. every table gets an ``_embedding vector(DIM)`` column. DIM, model and recipe are the same for all tables (``kgt_meta``
   records them; a different embedder is refused until ``--rebuild``). A row's embedded text is: document / section / caption,
   column names (with units), the row's *text* cells, notes, and the KG neighbourhood (entities, cited identifiers).
   **Numeric cell values are not embedded.** The table as a whole is embedded too (``kgt_tables.table_embedding``);
3. the tables of one document are compared to infer join columns (names + values + KG relations, see ``kg_relmap``); the
   result is written as a data dictionary and a relationship map (JSON, Markdown, Mermaid) under ``<home>/tabledb/<docid>/``
   and into ``kgt_columns`` / ``kgt_joins``.

Incremental: a table is reloaded only when its CSV, its KG context or the embedder changed (or with ``--force``).
Tables whose source has disappeared are dropped. Everything is in schema ``kgtables``.

Read side (used by the MCP server): table registry, semantic search (two stages: rank tables by ``table_embedding``, then rows
by ``_embedding``), structured filters, joins, an optional guarded read-only SQL, with a lexical fallback when no embedder is
reachable. Requires ``psycopg`` (v3) and the pgvector extension in the database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import operator
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kg_llmjoin as L  # noqa: E402
import kg_relmap as R  # noqa: E402
import kg_tables as K  # noqa: E402
import kg_units as U  # noqa: E402
from kg_embed import Embedder, EmbedError, get_embedder  # noqa: E402
from kg_llm import LLM, get_llm  # noqa: E402
from kg_models import PAYWALL_HOSTS  # noqa: E402

log = logging.getLogger("kg_tabledb")
SCHEMA = "kgtables"
MAX_HNSW_DIM = 2000


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def paywall_info(url: str) -> bool:
    if url.startswith("file://"):
        return True                                           # a purchased local copy
    host = url.split("/")[2].lower() if "//" in url else ""
    return bool(host) and any(host == x or host.endswith("." + x) for x in PAYWALL_HOSTS)


class TableDBError(RuntimeError):
    pass


# ======================================================================================================== database
class TableDB:
    """Connection factory + DDL + the read-side queries."""

    def __init__(self, dsn: str, schema: str = SCHEMA):
        if not dsn:
            raise TableDBError("no database DSN: pass --dsn / --tabledb-dsn or set COGENT_TABLEDB_DSN")
        self.dsn, self.schema = dsn, schema

    def connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise TableDBError("psycopg (v3) is required: pip install 'psycopg[binary]'") from exc
        # search_path is a connection option (a SET inside the transaction would be undone by a rollback)
        return psycopg.connect(self.dsn, autocommit=False, options=f"-c search_path={self.schema},public")

    # ----------------------------------------------------------------------------------------------- DDL
    def ensure_schema(self, conn, spec: str, dim: int, rebuild: bool = False, recipe: str = K.recipe("full")) -> None:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        conn.execute("CREATE TABLE IF NOT EXISTS kgt_meta (key text PRIMARY KEY, value jsonb NOT NULL)")
        row = conn.execute("SELECT value FROM kgt_meta WHERE key = 'embedding'").fetchone()
        if row and (row[0]["spec"] != spec or row[0]["recipe"] != recipe):
            if not rebuild:
                raise TableDBError(f"the database was built with embedder {row[0]['spec']!r} / recipe {row[0]['recipe']}, "
                                   f"this run uses {spec!r} / {recipe}. Vectors of different models or recipes are not comparable: "
                                   "use the same embedder, or rebuild everything with --rebuild")
            self.drop_all(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS kgt_meta (key text PRIMARY KEY, value jsonb NOT NULL)")
            row = None
        if not row:
            conn.execute("INSERT INTO kgt_meta VALUES ('embedding', %s::jsonb) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         (json.dumps({"spec": spec, "dim": dim, "recipe": recipe, "created": utcnow()}),))
        conn.execute(f"""CREATE TABLE IF NOT EXISTS kgt_tables (
            table_id text PRIMARY KEY, pg_table text UNIQUE NOT NULL, docid text NOT NULL, asset_id text NOT NULL,
            source_url text, doc_label text, entries text[] NOT NULL DEFAULT '{{}}', page int, caption text, table_label text,
            section_path text, intro text, notes text, kg_entities text[], n_rows int, n_cols int, header_detected boolean,
            csv_sha text, context_sha text, spec text, paywalled boolean NOT NULL DEFAULT false,
            loaded_at timestamptz NOT NULL DEFAULT now(), embed_text text, table_embedding vector({dim}))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS kgt_columns (
            table_id text NOT NULL REFERENCES kgt_tables ON DELETE CASCADE, ordinal int NOT NULL, name text NOT NULL, header text,
            pg_type text, role text, unit text, n_nonnull int, n_distinct int, key_candidate boolean, samples jsonb,
            vmin double precision, vmax double precision, PRIMARY KEY (table_id, ordinal))""")
        for col, ty in (("quantity", "text"), ("unit_source", "text"), ("unit_confidence", "real"), ("unit_candidates", "jsonb")):   # unit deduction
            conn.execute(f"ALTER TABLE kgt_columns ADD COLUMN IF NOT EXISTS {col} {ty}")
        conn.execute("""CREATE TABLE IF NOT EXISTS kgt_joins (
            id serial PRIMARY KEY, docid text NOT NULL, kind text NOT NULL, left_table text NOT NULL, left_column text,
            right_table text NOT NULL, right_column text, cardinality text, score real, detail jsonb)""")
        for col, ty in (("status", "text"), ("methods", "text[]"), ("det_score", "real"), ("llm_agreement", "real"),
                        ("judge_verdict", "text"), ("judge_confidence", "real")):        # added with the LLM join inference
            conn.execute(f"ALTER TABLE kgt_joins ADD COLUMN IF NOT EXISTS {col} {ty}")
        conn.execute("CREATE INDEX IF NOT EXISTS kgt_tables_docid ON kgt_tables (docid)")
        if dim <= MAX_HNSW_DIM:
            conn.execute("CREATE INDEX IF NOT EXISTS kgt_tables_emb ON kgt_tables USING hnsw (table_embedding vector_cosine_ops)")

    def drop_all(self, conn) -> None:
        for (t,) in conn.execute("SELECT pg_table FROM kgt_tables").fetchall() if self._exists(conn, "kgt_tables") else []:
            conn.execute(f'DROP TABLE IF EXISTS "{self.schema}"."{t}"')
        for t in ("kgt_joins", "kgt_columns", "kgt_tables", "kgt_meta"):
            conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")

    def _exists(self, conn, name: str) -> bool:
        return conn.execute("SELECT to_regclass(%s)", (f'"{self.schema}".{name}',)).fetchone()[0] is not None

    def meta(self, conn) -> Optional[dict[str, Any]]:
        if not self._exists(conn, "kgt_meta"):
            return None
        r = conn.execute("SELECT value FROM kgt_meta WHERE key = 'embedding'").fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------------------------------------- write side
    def drop_table(self, conn, table_id: str) -> None:
        r = conn.execute("SELECT pg_table FROM kgt_tables WHERE table_id = %s", (table_id,)).fetchone()
        if r:
            conn.execute(f'DROP TABLE IF EXISTS "{self.schema}"."{r[0]}"')
        conn.execute("DELETE FROM kgt_tables WHERE table_id = %s", (table_id,))

    def load_table(self, conn, rec: "TableRecord", dim: int) -> None:
        """(Re)create the physical table with its rows, the registry row and the column rows, in the caller's transaction."""
        from psycopg import sql
        self.drop_table(conn, rec.table_id)
        defs = [sql.SQL("_row_no int PRIMARY KEY"), sql.SQL("_raw jsonb NOT NULL"), sql.SQL("_embed_text text NOT NULL")]
        defs += [sql.SQL("{} {}").format(sql.Identifier(c.name), sql.SQL(c.pg_type)) for c in rec.cols]
        defs.append(sql.SQL(f"_embedding vector({dim})"))
        conn.execute(sql.SQL("CREATE TABLE {}.{} ({})").format(sql.Identifier(self.schema), sql.Identifier(rec.pg_table), sql.SQL(", ").join(defs)))
        names = ["_row_no", "_raw", "_embed_text"] + [c.name for c in rec.cols] + ["_embedding"]
        ins = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
            sql.Identifier(self.schema), sql.Identifier(rec.pg_table), sql.SQL(", ").join(map(sql.Identifier, names)),
            sql.SQL(", ").join([sql.SQL("%s"), sql.SQL("%s::jsonb"), sql.SQL("%s")] + [sql.SQL("%s")] * len(rec.cols) + [sql.SQL("%s::vector")]))
        keys, seen = [], {}
        for c in rec.cols:                                          # unique JSON keys even for repeated headers
            seen[c.header] = seen.get(c.header, 0) + 1
            keys.append(c.header if seen[c.header] == 1 else f"{c.header} ({seen[c.header]})")
        data = []
        for i, (row, text, ev) in enumerate(zip(rec.rows, rec.row_texts, rec.row_vecs), 1):
            raw = dict(zip(keys, row))
            data.append([i, json.dumps(raw, ensure_ascii=False), text] + [K.typed_value(cell, c) for c, cell in zip(rec.cols, row)] + [vec(ev)])
        with conn.cursor() as cur:
            cur.executemany(ins, data)
        if dim <= MAX_HNSW_DIM and rec.rows:
            conn.execute(sql.SQL("CREATE INDEX {} ON {}.{} USING hnsw (_embedding vector_cosine_ops)").format(
                sql.Identifier(rec.pg_table + "_emb"), sql.Identifier(self.schema), sql.Identifier(rec.pg_table)))
        conn.execute("""INSERT INTO kgt_tables (table_id, pg_table, docid, asset_id, source_url, doc_label, entries, page, caption, table_label,
              section_path, intro, notes, kg_entities, n_rows, n_cols, header_detected, csv_sha, context_sha, spec, paywalled, embed_text,
              table_embedding) VALUES (%(table_id)s, %(pg_table)s, %(docid)s, %(asset_id)s, %(source_url)s, %(doc_label)s, %(entries)s, %(page)s,
              %(caption)s, %(table_label)s, %(section_path)s, %(intro)s, %(notes)s, %(kg_entities)s, %(n_rows)s, %(n_cols)s, %(header_detected)s,
              %(csv_sha)s, %(context_sha)s, %(spec)s, %(paywalled)s, %(embed_text)s, %(table_embedding)s::vector)""", rec.registry(vec(rec.table_vec)))
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO kgt_columns (table_id, ordinal, name, header, pg_type, role, unit, n_nonnull, n_distinct, key_candidate, samples, vmin, vmax,
                               quantity, unit_source, unit_confidence, unit_candidates) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb)""",
                            [(rec.table_id, i, c.name, c.header, c.pg_type, c.role, c.unit, c.n_nonnull, c.n_distinct, c.key_candidate,
                              json.dumps(c.samples, ensure_ascii=False), c.vmin, c.vmax, c.quantity, c.unit_source, c.unit_confidence,
                              json.dumps(c.unit_candidates, ensure_ascii=False)) for i, c in enumerate(rec.cols, 1)])

    def replace_joins(self, conn, docid: str, cands: list[dict[str, Any]], same_schema: Optional[list[dict[str, Any]]] = None) -> None:
        """Store *all* join candidates of a document (with their status, scores and the full evidence as JSON) plus its same-schema pairs.
        Which of them count as relationships is decided at query time by approach (``relationships``)."""
        conn.execute("DELETE FROM kgt_joins WHERE docid = %s", (docid,))
        rows = []
        for c in cands:
            j, l = c.get("judge"), c.get("llm")
            rows.append((docid, "join", c["left_table"], c["left_column"], c["right_table"], c["right_column"], c["cardinality"], c["final_score"], json.dumps(c, ensure_ascii=False),
                         c["status"], c["methods"], c["deterministic"]["score"], l["agreement"] if l else None, j["verdict"] if j else None, j["confidence"] if j else None))
        for r in same_schema or []:
            rows.append((docid, "same_schema", r["left_table"], None, r["right_table"], None, None, r["score"], json.dumps(r, ensure_ascii=False),
                         "same_schema", ["deterministic"], r["score"], None, None, None))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO kgt_joins (docid, kind, left_table, left_column, right_table, right_column, cardinality, score, detail, status, methods, "
                            "det_score, llm_agreement, judge_verdict, judge_confidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)", rows)

    def set_entries(self, conn, table_id: str, entries: list[str]) -> None:
        conn.execute("UPDATE kgt_tables SET entries = %s WHERE table_id = %s", (entries, table_id))

    def registry(self, conn) -> dict[str, dict[str, Any]]:
        if not self._exists(conn, "kgt_tables"):
            return {}
        rows = conn.execute("SELECT table_id, docid, csv_sha, context_sha, spec, pg_table FROM kgt_tables").fetchall()
        return {r[0]: {"docid": r[1], "csv_sha": r[2], "context_sha": r[3], "spec": r[4], "pg_table": r[5]} for r in rows}

    # -------------------------------------------------------------------------------------------- read side
    TABLE_COLS = ("table_id", "docid", "source_url", "doc_label", "entries", "page", "caption", "table_label", "section_path",
                  "n_rows", "n_cols", "paywalled", "loaded_at", "pg_table")

    def _tables_sql(self, extra: str = "", dist: bool = False) -> str:
        cols = ", ".join(self.TABLE_COLS)
        return f"SELECT {cols}{', table_embedding <=> %(q)s::vector AS distance' if dist else ''} FROM kgt_tables {extra}"

    def _row_to_table(self, row, cols: tuple[str, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        if "loaded_at" in d and d["loaded_at"] is not None:
            d["loaded_at"] = d["loaded_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
        d["section_path"] = d["section_path"] or ""
        return d

    def list_tables(self, conn, docids: Optional[list[str]] = None, query: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        where, args = [], {"lim": limit}
        if docids is not None:
            where.append("docid = ANY(%(docids)s)")
            args["docids"] = docids
        if query:
            where.append("(caption ILIKE %(like)s OR section_path ILIKE %(like)s OR doc_label ILIKE %(like)s OR embed_text ILIKE %(like)s)")
            args["like"] = f"%{query}%"
        sql_ = self._tables_sql(("WHERE " + " AND ".join(where) if where else "") + " ORDER BY docid, page NULLS LAST, table_id LIMIT %(lim)s")
        return [self._row_to_table(r, self.TABLE_COLS) for r in conn.execute(sql_, args).fetchall()]

    def describe(self, conn, table_id: str, sample_rows: int = 5) -> dict[str, Any]:
        r = conn.execute(self._tables_sql("WHERE table_id = %(t)s"), {"t": table_id}).fetchone()
        if r is None:
            raise TableDBError(f"unknown table {table_id!r}")
        t = self._row_to_table(r, self.TABLE_COLS)
        extra = conn.execute("SELECT intro, notes, kg_entities, header_detected FROM kgt_tables WHERE table_id = %s", (table_id,)).fetchone()
        t.update(intro=extra[0], notes=extra[1], kg_entities=extra[2], header_detected=extra[3])
        t["columns"] = [dict(zip(("ordinal", "name", "header", "pg_type", "role", "unit", "n_nonnull", "n_distinct", "key_candidate", "samples", "min", "max",
                                  "quantity", "unit_source", "unit_confidence", "unit_candidates"), c))
                        for c in conn.execute("SELECT ordinal, name, header, pg_type, role, unit, n_nonnull, n_distinct, key_candidate, samples, vmin, vmax, "
                                              "quantity, unit_source, unit_confidence, unit_candidates FROM kgt_columns WHERE table_id = %s ORDER BY ordinal",
                                              (table_id,)).fetchall()]
        if sample_rows:
            t["sample_rows"] = [r[0] for r in conn.execute(f'SELECT _raw FROM "{self.schema}"."{t["pg_table"]}" ORDER BY _row_no LIMIT %s', (sample_rows,)).fetchall()]
        return t

    def search_tables(self, conn, qvec: Optional[list[float]], question: str, docids: Optional[list[str]], k: int) -> list[dict[str, Any]]:
        where = "WHERE docid = ANY(%(docids)s)" if docids is not None else ""
        if qvec is not None:
            sql_ = self._tables_sql(f"{where} ORDER BY table_embedding <=> %(q)s::vector LIMIT %(k)s", dist=True)
            rows = conn.execute(sql_, {"q": vec(qvec), "k": k, "docids": docids}).fetchall()
            cols = self.TABLE_COLS + ("distance",)
        else:                                                      # lexical fallback
            w = ("AND " if where else "WHERE ") + "to_tsvector('simple', coalesce(caption,'') || ' ' || coalesce(embed_text,'')) @@ websearch_to_tsquery('simple', %(qs)s)"
            sql_ = self._tables_sql(f"{where} {w} LIMIT %(k)s")
            rows = conn.execute(sql_, {"qs": question, "k": k, "docids": docids}).fetchall()
            cols = self.TABLE_COLS
        out = [self._row_to_table(r, cols) for r in rows]
        for t in out:
            if "distance" in t:
                t["similarity"] = round(1 - float(t.pop("distance")), 4)
        return out

    def search_rows(self, conn, qvec: Optional[list[float]], question: str, tables: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
        hits = []
        for t in tables:
            tbl = f'"{self.schema}"."{t["pg_table"]}"'
            if qvec is not None:
                rows = conn.execute(f"SELECT _row_no, _raw, _embedding <=> %s::vector AS d FROM {tbl} ORDER BY d LIMIT %s", (vec(qvec), k)).fetchall()
                hits += [{"table_id": t["table_id"], "row": r[0], "values": r[1], "similarity": round(1 - float(r[2]), 4)} for r in rows]
            else:
                rows = conn.execute(f"SELECT _row_no, _raw, ts_rank(to_tsvector('simple', _embed_text), websearch_to_tsquery('simple', %s)) AS s FROM {tbl} "
                                    "WHERE to_tsvector('simple', _embed_text) @@ websearch_to_tsquery('simple', %s) ORDER BY s DESC LIMIT %s",
                                    (question, question, k)).fetchall()
                hits += [{"table_id": t["table_id"], "row": r[0], "values": r[1], "score": round(float(r[2]), 4)} for r in rows]
        hits.sort(key=lambda h: -h.get("similarity", h.get("score", 0)))
        return hits[:k]

    def lookup(self, conn, qvec: Optional[list[float]], question: str, docids: Optional[list[str]] = None, table_ids: Optional[list[str]] = None,
               k: int = 10, candidate_tables: int = 5, hybrid: bool = False) -> dict[str, Any]:
        """Two-stage retrieval: rank the tables (table embedding), search the rows of the best ones (row embeddings), merge by
        similarity. Without a query vector (no embedder) every table in scope is searched lexically instead. ``hybrid`` fuses the semantic
        and the lexical ranking (reciprocal rank fusion) at both stages: robust for exact terms such as codes and identifiers."""
        if table_ids:
            tables = [t for t in self.list_tables(conn, docids, None, 500) if t["table_id"] in set(table_ids)]
        elif qvec is not None:
            tables = self.search_tables(conn, qvec, question, docids, candidate_tables)
            if hybrid:
                seen = {t["table_id"] for t in tables}
                tables += [t for t in self.search_tables(conn, None, question, docids, candidate_tables) if t["table_id"] not in seen]
        else:
            tables = self.list_tables(conn, docids, None, 200)
        if hybrid and qvec is not None:
            sem, lex = self.search_rows(conn, qvec, question, tables, k * 3), self.search_rows(conn, None, question, tables, k * 3)
            fused: dict[tuple, dict[str, Any]] = {}
            for rank_list, tag in ((sem, "similarity"), (lex, "score")):
                for i, h in enumerate(rank_list):
                    key = (h["table_id"], h["row"])
                    f = fused.setdefault(key, {**h, "rrf": 0.0, "found_by": []})
                    f["rrf"] += 1.0 / (60 + i + 1)
                    f["found_by"].append("semantic" if tag == "similarity" else "lexical")
                    f.setdefault("similarity", h.get("similarity"))
            rows = sorted(fused.values(), key=lambda h: -h["rrf"])[:k]
            for r in rows:
                r["rrf"] = round(r["rrf"], 5)
                if r.get("similarity") is None:
                    r.pop("similarity", None)
            return {"mode": "hybrid", "tables": tables, "rows": rows}
        return {"mode": "semantic" if qvec is not None else "lexical", "tables": tables,
                "rows": self.search_rows(conn, qvec, question, tables, k)}

    def column_units(self, conn, table_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Numeric columns of the tables with their stored unit candidates: {table_id: [{header, name, unit, candidates}]}."""
        out: dict[str, list[dict[str, Any]]] = {}
        for tid, header, name, unit, cands in conn.execute("SELECT table_id, header, name, unit, unit_candidates FROM kgt_columns WHERE table_id = ANY(%s) "
                                                           "AND pg_type <> 'text' ORDER BY table_id, ordinal", (table_ids,)).fetchall():
            out.setdefault(tid, []).append({"header": header, "name": name, "unit": unit, "candidates": cands or []})
        return out

    # structured queries ------------------------------------------------------------------------------
    OPS = {"=": "=", "==": "=", "!=": "<>", "<>": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "like": "ILIKE", "contains": "ILIKE",
           "in": "IN", "between": "BETWEEN", "is_null": "IS NULL", "not_null": "IS NOT NULL"}

    def _columns_of(self, conn, table_id: str) -> tuple[str, dict[str, dict[str, str]]]:
        r = conn.execute("SELECT pg_table FROM kgt_tables WHERE table_id = %s", (table_id,)).fetchone()
        if r is None:
            raise TableDBError(f"unknown table {table_id!r}")
        cols = {}
        for name, header, ty in conn.execute("SELECT name, header, pg_type FROM kgt_columns WHERE table_id = %s ORDER BY ordinal", (table_id,)).fetchall():
            cols[name.lower()] = {"name": name, "type": ty}
            cols.setdefault(header.lower(), {"name": name, "type": ty})
        return r[0], cols

    @staticmethod
    def _col(cols: dict[str, dict[str, str]], key: str) -> dict[str, str]:
        c = cols.get(str(key).lower())
        if c is None:
            raise TableDBError(f"unknown column {key!r}; columns: {sorted({v['name'] for v in cols.values()})}")
        return c

    @staticmethod
    def _coerce(value: Any, ty: str) -> Any:
        if ty == "text":
            return str(value)
        try:
            return int(value) if ty == "bigint" and float(value).is_integer() else float(value)
        except (TypeError, ValueError):
            raise TableDBError(f"value {value!r} is not numeric but the column is {ty}") from None

    def query_table(self, conn, table_id: str, where: Optional[list[dict[str, Any]]] = None, columns: Optional[list[str]] = None,
                    order_by: Optional[str] = None, descending: bool = False, limit: int = 20) -> dict[str, Any]:
        from psycopg import sql
        pg_table, cols = self._columns_of(conn, table_id)
        sel = [self._col(cols, c)["name"] for c in columns] if columns else list(dict.fromkeys(v["name"] for v in cols.values()))
        clauses, params = [], []
        for w in where or []:
            c = self._col(cols, w.get("column"))
            op = self.OPS.get(str(w.get("op", "=")).lower())
            if op is None:
                raise TableDBError(f"unsupported operator {w.get('op')!r}; use one of {sorted(self.OPS)}")
            ident = sql.Identifier(c["name"])
            val = w.get("value")
            if op in ("IS NULL", "IS NOT NULL"):
                clauses.append(sql.SQL("{} " + op).format(ident))
            elif op == "IN":
                vs = [self._coerce(x, c["type"]) for x in (val if isinstance(val, list) else [val])]
                clauses.append(sql.SQL("{} = ANY(%s)").format(ident))
                params.append(vs)
            elif op == "BETWEEN":
                if not (isinstance(val, list) and len(val) == 2):
                    raise TableDBError("'between' needs value: [low, high]")
                clauses.append(sql.SQL("{} BETWEEN %s AND %s").format(ident))
                params += [self._coerce(x, c["type"]) for x in val]
            elif op == "ILIKE":
                if c["type"] != "text":
                    raise TableDBError(f"'like' needs a text column, {c['name']} is {c['type']}")
                s = str(val)
                clauses.append(sql.SQL("{} ILIKE %s").format(ident))
                params.append(s if "%" in s else f"%{s}%")
            else:
                clauses.append(sql.SQL("{} " + op + " %s").format(ident))
                params.append(self._coerce(val, c["type"]))
        q = sql.SQL("SELECT _row_no, _raw, {} FROM {}.{}").format(sql.SQL(", ").join(map(sql.Identifier, sel)), sql.Identifier(self.schema), sql.Identifier(pg_table))
        if clauses:
            q += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        order = sql.Identifier(self._col(cols, order_by)["name"]) if order_by else sql.SQL("_row_no")
        q += sql.SQL(" ORDER BY {} {}").format(order, sql.SQL("DESC" if descending else "ASC")) + sql.SQL(" LIMIT %s")
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(q, params).fetchall()
        return {"table_id": table_id, "columns": sel, "rows": [{"row": r[0], "values": r[1], "typed": dict(zip(sel, r[2:]))} for r in rows], "count": len(rows)}

    def join(self, conn, left: str, right: str, left_column: str, right_column: str, columns: Optional[list[str]] = None, limit: int = 20) -> dict[str, Any]:
        from psycopg import sql
        lt, lcols = self._columns_of(conn, left)
        rt, rcols = self._columns_of(conn, right)
        lc, rc = self._col(lcols, left_column), self._col(rcols, right_column)
        lcast = (lambda i: i) if lc["type"] == rc["type"] else (lambda i: sql.SQL("{}::text").format(i))
        parts = [sql.SQL("l.{} AS {}").format(sql.Identifier(n), sql.Identifier("l_" + n)) for n in dict.fromkeys(v["name"] for v in lcols.values())]
        parts += [sql.SQL("r.{} AS {}").format(sql.Identifier(n), sql.Identifier("r_" + n)) for n in dict.fromkeys(v["name"] for v in rcols.values())]
        q = sql.SQL("SELECT {} FROM {}.{} l JOIN {}.{} r ON {} = {} ORDER BY l._row_no, r._row_no LIMIT %s").format(
            sql.SQL(", ").join(parts), sql.Identifier(self.schema), sql.Identifier(lt), sql.Identifier(self.schema), sql.Identifier(rt),
            lcast(sql.SQL("l.{}").format(sql.Identifier(lc["name"]))), lcast(sql.SQL("r.{}").format(sql.Identifier(rc["name"]))))
        cur = conn.execute(q, (max(1, min(int(limit), 500)),))
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        if columns:
            want = {c.lower() for c in columns}
            rows = [{k: v for k, v in r.items() if k[2:].lower() in want or k.lower() in want} for r in rows]
        return {"left": left, "right": right, "on": f"{lc['name']} = {rc['name']}", "rows": rows, "count": len(rows)}

    def relationships(self, conn, docids: Optional[list[str]] = None, approach: str = "hybrid", min_score: Optional[float] = None,
                      include_rejected: bool = False, min_agreement: float = 0.0, same_schema: bool = False) -> list[dict[str, Any]]:
        """Relationships of the documents for an approach (deterministic | llm | hybrid | consensus), chosen at query time from the stored
        candidates (see ``kg_llmjoin.select``). Each row has ``kind``, the columns, ``cardinality``, ``score`` and ``status`` / ``methods`` /
        ``evidence`` (also under ``detail``)."""
        where, args = ("WHERE docid = ANY(%s)", (docids,)) if docids is not None else ("", ())
        by_doc: dict[str, list[dict[str, Any]]] = {}
        out: list[dict[str, Any]] = []
        for docid, kind, detail in conn.execute(f"SELECT docid, kind, detail FROM kgt_joins {where} ORDER BY id", args).fetchall():
            if kind == "join":
                by_doc.setdefault(docid, []).append(detail)
            elif same_schema:
                out.append({**detail, "docid": docid, "kind": "same_schema", "detail": detail})
        for docid, cands in by_doc.items():
            for r in L.candidates_to_relations(L.select(cands, approach, min_score, include_rejected, min_agreement)):
                out.append({**r, "docid": docid, "detail": r})
        return sorted(out, key=lambda r: -(r.get("score") or 0))

    _FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|vacuum|analyze|set|reset|"
                            r"pg_read_file|pg_ls_dir|lo_import|lo_export|dblink|pg_sleep|pg_terminate_backend|pg_cancel_backend|nextval|setval)\b", re.I)

    def run_sql(self, conn, query: str, limit: int = 100, timeout_ms: int = 5000) -> dict[str, Any]:
        """A single read-only SELECT / WITH statement against schema ``kgtables``. Defence in depth: keyword filter, read-only
        transaction, statement timeout, row cap. For real isolation connect with a role that has SELECT only (see docs/tabledb.md)."""
        q = query.strip().rstrip(";").strip()
        if ";" in q or not re.match(r"^(select|with)\b", q, re.I) or self._FORBIDDEN.search(re.sub(r"'(?:[^']|'')*'", "''", q)):
            raise TableDBError("only a single read-only SELECT statement is allowed")
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        cur = conn.execute(f"SELECT * FROM ({q}) AS q LIMIT {max(1, min(int(limit), 1000))}")
        names = [d.name for d in cur.description]
        return {"columns": names, "rows": [dict(zip(names, r)) for r in cur.fetchall()]}


# ======================================================================================================== sources
@dataclass
class Source:
    docid: str
    asset_id: str
    csv: Path
    caption: str
    page: Optional[int]
    url: str
    doc_label: str
    entries: list[str]
    kg_path: Path

    @property
    def table_id(self) -> str:
        return f"{self.docid}/{self.asset_id}"

    @property
    def pg_table(self) -> str:
        return re.sub(r"\W", "_", f"kgt_{self.docid[:10]}_{self.asset_id}").lower()[:60]


@dataclass
class TableRecord:
    src: Source
    fr: K.Frame
    cols: list[K.Col]
    ctx: K.Context
    csv_sha: str
    rows: list[list[str]] = field(default_factory=list)
    row_texts: list[str] = field(default_factory=list)
    row_vecs: list[list[float]] = field(default_factory=list)
    table_embed_text: str = ""
    table_vec: list[float] = field(default_factory=list)
    spec: str = ""

    @property
    def table_id(self) -> str:
        return self.src.table_id

    @property
    def pg_table(self) -> str:
        return self.src.pg_table

    def registry(self, table_embedding: str) -> dict[str, Any]:
        s, c = self.src, self.ctx
        return {"table_id": s.table_id, "pg_table": s.pg_table, "docid": s.docid, "asset_id": s.asset_id, "source_url": s.url, "doc_label": c.doc or s.doc_label,
                "entries": s.entries, "page": s.page, "caption": c.caption, "table_label": c.table_label, "section_path": " > ".join(c.section_path),
                "intro": c.intro, "notes": " ".join(c.notes), "kg_entities": c.entities + c.idents, "n_rows": len(self.rows), "n_cols": len(self.cols),
                "header_detected": self.fr.header_detected, "csv_sha": self.csv_sha, "context_sha": c.sha(), "spec": self.spec,
                "paywalled": paywall_info(s.url), "embed_text": self.table_embed_text, "table_embedding": table_embedding}

    def dict_entry(self) -> dict[str, Any]:
        c = self.ctx
        return {"table_id": self.table_id, "pg_table": self.pg_table, "node_id": self.src.asset_id, "caption": c.caption, "table_label": c.table_label,
                "page": self.src.page, "section_path": c.section_path, "n_rows": len(self.rows), "header_detected": self.fr.header_detected,
                "notes": c.notes, "source_file": self.src.csv.name, "csv_sha": self.csv_sha,
                "columns": [{"name": x.name, "header": x.header, "pg_type": x.pg_type, "role": x.role, "unit": x.unit, "unit_source": x.unit_source,
                             "unit_confidence": x.unit_confidence, "quantity": x.quantity, "unit_candidates": x.unit_candidates, "n_nonnull": x.n_nonnull,
                             "n_distinct": x.n_distinct, "key_candidate": x.key_candidate, "samples": x.samples, "min": x.vmin, "max": x.vmax} for x in self.cols]}


def discover(home: Path) -> list[Source]:
    """Every table CSV exported next to a staged document KG, with the entries that use the document."""
    entry_docs: dict[str, list[str]] = {}
    for kind in ("standard", "report"):
        for meta in sorted((home / "staging" / f"{kind}s").glob("*/meta.json")):
            try:
                for d in json.loads(meta.read_text(encoding="utf-8")).get("documents", []):
                    entry_docs.setdefault(d["docid"], []).append(f"{kind}:{meta.parent.name}")
            except (OSError, ValueError, KeyError):
                continue
    out = []
    for state_file in sorted((home / "staging" / "documents").glob("*/state.json")):
        ddir = state_file.parent
        mans = sorted((ddir / "assets").glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not (ddir / "kg.json").exists() or not mans:
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            man = json.loads(mans[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for a in man.get("assets", []):
            if a.get("kind") == "table" and (mans[0].parent / a["file"]).is_file():
                out.append(Source(ddir.name, a["id"], mans[0].parent / a["file"], a.get("caption") or "", a.get("page"), state.get("url", ""),
                                  "", sorted(entry_docs.get(ddir.name, [])), ddir / "kg.json"))
    return out


# ========================================================================================================== agent
@dataclass
class Settings:
    home: Path
    dsn: str
    embedder: Embedder
    only: list[str] = field(default_factory=list)       # entry keys ('standard:22') or document ids
    force: bool = False                                 # reload every table in scope
    rebuild: bool = False                               # drop the database and rebuild (needed after changing embedder / recipe)
    kg_context: bool = True                             # use the KG neighbourhood of a table in the embedded text
    batch: int = 64
    schema: str = SCHEMA
    row_mode: str = "full"                              # full: embed every cell of a row | text: leave numeric values out
    llm: Optional[LLM] = None                           # suggester LLM for join inference (None: deterministic joins only)
    judge_llm: Optional[LLM] = None                     # judge LLM (default: the suggester)
    llm_cfg: L.Config = field(default_factory=L.Config)


class State(TypedDict, total=False):
    sources: list[Source]
    scope: Optional[set]
    todo: list[Source]
    loaded: Annotated[list[dict], operator.add]
    skipped: Annotated[list[dict], operator.add]
    dropped: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    docs_changed: set
    joins: dict
    evaluations: dict
    summary: dict


def _scope(settings: Settings, sources: list[Source]) -> Optional[set]:
    if not settings.only:
        return None
    ids = set()
    for o in settings.only:
        ids |= {s.docid for s in sources if o in s.entries or s.docid.startswith(o)}
    return ids


class Agent:
    def __init__(self, settings: Settings):
        self.s, self.db = settings, TableDB(settings.dsn, settings.schema)
        self.out = settings.home / "tabledb"
        self._graphs: dict[Path, Optional[nx.MultiDiGraph]] = {}
        self._stats: dict[Path, dict[str, dict[str, int]]] = {}

    def graph(self, p: Path) -> Optional[nx.MultiDiGraph]:
        if p not in self._graphs:
            try:
                self._graphs[p] = nx.node_link_graph(json.loads(p.read_text(encoding="utf-8")), edges="edges")
            except (OSError, ValueError):
                self._graphs[p] = None
        return self._graphs[p]

    def prepare(self, src: Source) -> Optional[TableRecord]:
        """CSV + KG -> typed columns, context, csv hash; None if the CSV holds no usable table."""
        raw = K.read_csv(src.csv)
        fr = K.frame(raw)
        if fr is None:
            return None
        g = self.graph(src.kg_path)
        ctx = K.kg_context(g, src.asset_id, src.caption, self.s.kg_context)
        if not ctx.doc:
            ctx.doc = src.doc_label
        cols = K.profile(fr)
        U.apply(cols, fr.rows, ctx, self.unit_stats(src.kg_path))                 # unit deduction from table metadata and the KG
        return TableRecord(src, fr, cols, ctx, hashlib.sha256(src.csv.read_bytes()).hexdigest()[:16], rows=fr.rows)

    def unit_stats(self, kg_path: Path) -> dict[str, dict[str, int]]:
        """Unit mentions by dimension over all paragraphs, headings and captions of a document KG (cached per document)."""
        if kg_path not in self._stats:
            g = self.graph(kg_path)
            self._stats[kg_path] = U.doc_unit_stats([str(d.get("text") or d.get("label") or "") for _, d in g.nodes(data=True)
                                                    if d.get("type") in ("Paragraph", "Heading", "Table", "Figure")]) if g is not None else {}
        return self._stats[kg_path]

    def embed(self, rec: TableRecord) -> None:
        emb = self.s.embedder
        rec.spec = emb.spec
        rec.row_texts = [K.row_text(rec.ctx, rec.cols, r, self.s.row_mode) for r in rec.rows]
        rec.row_vecs = []
        for i in range(0, len(rec.row_texts), self.s.batch):
            rec.row_vecs += emb.embed_docs(rec.row_texts[i:i + self.s.batch])
        rec.table_embed_text = K.table_text(rec.ctx, rec.cols, rec.rows)
        rec.table_vec = emb.embed_docs([rec.table_embed_text])[0]

    # ------------------------------------------------------------------------------------------ workflow
    def run(self) -> dict[str, Any]:
        from langgraph.graph import END, StateGraph
        wf = StateGraph(State)
        nodes = {"discover": self.n_discover, "load": self.n_load, "prune": self.n_prune, "relmap": self.n_relmap, "finalize": self.n_finalize}
        for n, fn in nodes.items():
            wf.add_node(n, fn)
        wf.set_entry_point("discover")
        names = list(nodes)
        for a, b in zip(names, names[1:]):
            wf.add_edge(a, b)
        wf.add_edge("finalize", END)
        return wf.compile().invoke({})["summary"]

    def n_discover(self, state: State) -> State:
        sources = discover(self.s.home)
        return {"sources": sources, "scope": _scope(self.s, sources), "warnings": ([f"no staged tables found under {self.s.home}/staging/documents"] if not sources else [])}

    def n_load(self, state: State) -> State:
        s, db = self.s, self.db
        scope = state["scope"]
        todo = [x for x in state["sources"] if scope is None or x.docid in scope]
        out: State = {"loaded": [], "skipped": [], "errors": [], "docs_changed": set()}
        dim = s.embedder.dim
        conn = db.connect()
        try:
            db.ensure_schema(conn, s.embedder.spec, dim, s.rebuild, K.recipe(s.row_mode))
            conn.commit()
            reg = db.registry(conn)
            for src in todo:
                try:
                    rec = self.prepare(src)
                    if rec is None:
                        out["skipped"].append({"table_id": src.table_id, "reason": "no usable table (needs >= 2 columns and >= 1 data row)"})
                        if src.table_id in reg:
                            db.drop_table(conn, src.table_id)
                            conn.commit()
                        continue
                    old = reg.get(src.table_id)
                    same = old and old["csv_sha"] == rec.csv_sha and old["context_sha"] == rec.ctx.sha() and old["spec"] == s.embedder.spec
                    if same and not s.force:
                        db.set_entries(conn, src.table_id, src.entries)
                        conn.commit()
                        out["skipped"].append({"table_id": src.table_id, "reason": "unchanged"})
                        continue
                    self.embed(rec)
                    db.load_table(conn, rec, dim)
                    conn.commit()
                    out["docs_changed"].add(src.docid)
                    out["loaded"].append({"table_id": src.table_id, "rows": len(rec.rows), "cols": len(rec.cols), "caption": rec.ctx.caption[:80],
                                          "action": "reloaded" if old else "created"})
                except EmbedError as exc:
                    conn.rollback()
                    out["errors"].append(f"{src.table_id}: {exc}")
                    out["warnings"] = [f"embedding failed for {src.table_id}; the table was left as it was"]
                    break                                          # the embedder is down: do not try every other table
                except Exception as exc:
                    conn.rollback()
                    log.exception("loading %s failed", src.table_id)
                    out["errors"].append(f"{src.table_id}: {type(exc).__name__}: {exc}")
        finally:
            conn.close()
        return out

    def signature(self, recs: list[TableRecord]) -> str:
        """What the published relationships of a document depend on: its tables, their KG context and the join-inference configuration."""
        s = self.s
        cfg = {"det": R.VERSION, "llm": [getattr(s.llm, "model", None), getattr(s.judge_llm or s.llm, "model", None), s.llm_cfg.as_dict()] if s.llm else None}
        return hashlib.sha256(json.dumps([[r.table_id, r.csv_sha, r.ctx.sha()] for r in recs] + [cfg], sort_keys=True).encode()).hexdigest()[:16]

    def n_relmap(self, state: State) -> State:
        """Infer joins (deterministic + optional LLM suggester with LLM-as-judge) and publish data dictionaries / relationship maps for documents
        whose tables or join-inference configuration changed (or that were never published)."""
        s, db = self.s, self.db
        by_doc: dict[str, list[Source]] = {}
        for x in state["sources"]:
            by_doc.setdefault(x.docid, []).append(x)
        scope = state["scope"]
        changed = state.get("docs_changed", set())
        joins, errors, warnings, evals = {}, [], [], {}
        conn = db.connect()
        try:
            for docid, srcs in sorted(by_doc.items()):
                if scope is not None and docid not in scope:
                    continue
                try:
                    recs = [r for r in (self.prepare(x) for x in srcs) if r is not None]
                    if not recs:
                        continue
                    sig = self.signature(recs)
                    prev = self.out / docid / "data_dictionary.json"
                    prev_sig = json.loads(prev.read_text(encoding="utf-8")).get("signature") if prev.exists() else None
                    if docid not in changed and prev_sig == sig and not s.force:
                        continue
                    tables = [R.T(r.table_id, r.src.asset_id, r.cols, r.rows, r.ctx) for r in recs]
                    det = R.infer(tables)
                    ev = L.evaluate(tables, det, s.llm, s.judge_llm, s.llm_cfg)
                    cands = ev["candidates"]
                    rels = L.candidates_to_relations(L.select(cands, "hybrid")) + [r for r in det if r["kind"] == "same_schema"]
                    url = srcs[0].url
                    label = recs[0].ctx.doc or docid
                    d = R.data_dictionary(docid, label, url if not url.startswith("file://") else "local:" + url.rsplit("/", 1)[-1],
                                          [r.dict_entry() for r in recs], rels, paywall_info(url))
                    d["generated"], d["embedder"], d["signature"], d["approach"] = utcnow(), s.embedder.spec, sig, "hybrid"
                    od = self.out / docid
                    od.mkdir(parents=True, exist_ok=True)
                    (od / "data_dictionary.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
                    (od / "data_dictionary.md").write_text(R.dictionary_markdown(d, rels), encoding="utf-8")
                    (od / "relationships.json").write_text(json.dumps({"document_id": docid, "document": label, "generated": d["generated"], "approach": "hybrid",
                                                                       "relationships": rels, "evaluation": {"metrics": ev["metrics"], "status": ev["status"], "models": ev["models"]}},
                                                                      indent=2, ensure_ascii=False), encoding="utf-8")
                    (od / "relationships.mmd").write_text(R.relationships_mermaid(d, rels), encoding="utf-8")
                    (od / "join_evaluation.json").write_text(json.dumps({"document_id": docid, "document": label, "generated": d["generated"], **ev}, indent=2, ensure_ascii=False), encoding="utf-8")
                    (od / "join_evaluation.md").write_text(L.evaluation_markdown(label, ev), encoding="utf-8")
                    registered = {r[0] for r in conn.execute("SELECT table_id FROM kgt_tables WHERE docid = %s", (docid,)).fetchall()}
                    ok = lambda r: r["left_table"] in registered and r["right_table"] in registered
                    db.replace_joins(conn, docid, [c for c in cands if ok(c)], [r for r in det if r["kind"] == "same_schema" and ok(r)])
                    conn.commit()
                    joins[docid] = len([r for r in rels if r["kind"] == "join"])
                    evals[docid] = {"metrics": ev["metrics"], "status": ev["status"]}
                    if s.llm and ev["status"]["llm"] in ("unavailable", "partial"):
                        warnings.append(f"LLM join inference for {docid} is {ev['status']['llm']}: " + "; ".join(ev["status"]["errors"][:2]))
                except Exception as exc:
                    conn.rollback()
                    log.exception("relationship inference failed for %s", docid)
                    errors.append(f"relationships {docid}: {type(exc).__name__}: {exc}")
        finally:
            conn.close()
        index = {x: {"tables": len(v)} for x, v in by_doc.items()}
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "index.json").write_text(json.dumps({"generated": utcnow(), "documents": index}, indent=2), encoding="utf-8")
        return {"joins": joins, "errors": errors, "warnings": warnings, "evaluations": evals}

    def n_prune(self, state: State) -> State:
        """Drop tables whose source table no longer exists (only for the documents in scope; everything if no scope)."""
        conn, dropped = self.db.connect(), []
        try:
            live = {x.table_id for x in state["sources"]}
            scope = state["scope"]
            for tid, r in self.db.registry(conn).items():
                if tid not in live and (scope is None or r["docid"] in scope):
                    self.db.drop_table(conn, tid)
                    dropped.append(tid)
            live_docs = {x.docid for x in state["sources"]}
            for docid in ({r["docid"] for r in self.db.registry(conn).values()} | {r[0] for r in conn.execute("SELECT DISTINCT docid FROM kgt_joins").fetchall()}) - live_docs:
                if scope is None:                                   # a document without any table left: forget its joins
                    conn.execute("DELETE FROM kgt_joins WHERE docid = %s", (docid,))
            conn.commit()
        finally:
            conn.close()
        if scope is None and self.out.is_dir():                     # ... and its published dictionary / map
            for d in self.out.iterdir():
                if d.is_dir() and d.name not in live_docs:
                    shutil.rmtree(d)
        changed = set(state.get("docs_changed", set())) | {t.split("/")[0] for t in dropped}
        return {"dropped": dropped, "docs_changed": changed}

    def _llm_summary(self, evals: dict[str, Any]) -> Optional[dict[str, Any]]:
        if self.s.llm is None:
            return None
        evals = {k: e for k, e in evals.items() if e["status"]["llm"] != "not run"}          # one-table documents have nothing to join
        m = [e["metrics"] for e in evals.values()]
        sts = {e["status"]["llm"] for e in evals.values()}
        sm = lambda k: sum((x.get("statuses") or {}).get(k, 0) for x in m)
        return {"suggester": getattr(self.s.llm, "model", None), "judge": getattr(self.s.judge_llm or self.s.llm, "model", None), "documents": len(m),
                "candidates": sum(x["candidates"] for x in m), "confirmed": sm("confirmed"), "deterministic_only": sm("deterministic_only"), "llm_only": sm("llm_only"),
                "disputed": sm("disputed"), "rejected": sm("rejected"), "uncertain": sm("uncertain"), "llm_status": sorted(sts)}

    def n_finalize(self, state: State) -> State:
        s = self.s
        summary = {"run": utcnow(), "embedder": s.embedder.spec, "forced": s.force, "sources": len(state["sources"]),
                   "loaded": len(state.get("loaded", [])), "unchanged": sum(1 for x in state.get("skipped", []) if x["reason"] == "unchanged"),
                   "skipped": sum(1 for x in state.get("skipped", []) if x["reason"] != "unchanged"), "dropped": len(state.get("dropped", [])),
                   "documents_with_joins": len(state.get("joins", {})), "joins": sum(state.get("joins", {}).values()),
                   "llm_joins": self._llm_summary(state.get("evaluations", {})),
                   "errors": state.get("errors", []), "warnings": state.get("warnings", [])}
        logs = s.home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "tabledb.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**summary, "tables": state.get("loaded", []), "skipped_tables": [x for x in state.get("skipped", []) if x["reason"] != "unchanged"],
                                 "dropped_tables": state.get("dropped", [])}, ensure_ascii=False) + "\n")
        L = [f"## Table database run {summary['run']}" + (" (FORCED)" if s.force else ""), "",
             f"{summary['sources']} table(s) staged: **{summary['loaded']} loaded**, {summary['unchanged']} unchanged, {summary['skipped']} skipped, "
             f"{summary['dropped']} dropped; {summary['joins']} join(s) inferred in {summary['documents_with_joins']} document(s). Embedder `{summary['embedder']}`.", ""]
        lj = summary["llm_joins"]
        if lj:
            L += [f"LLM join inference: suggester `{lj['suggester']}`, judge `{lj['judge']}`: {lj['candidates']} candidates ({lj['confirmed']} confirmed, {lj['deterministic_only']} deterministic only, "
                  f"{lj['llm_only']} LLM only, {lj['disputed']} disputed, {lj['rejected']} rejected, {lj['uncertain']} uncertain); status {', '.join(lj['llm_status']) or '-'}.", ""]
        L += [f"- loaded `{x['table_id']}` ({x['rows']} rows × {x['cols']} cols): {x['caption']}" for x in state.get("loaded", [])]
        L += [f"- skipped `{x['table_id']}`: {x['reason']}" for x in state.get("skipped", []) if x["reason"] != "unchanged"]
        L += [f"- dropped `{t}` (source gone)" for t in state.get("dropped", [])]
        L += [f"- **WARNING** {w}" for w in summary["warnings"]] + [f"- **ERROR** {e}" for e in summary["errors"]]
        with (logs / "tabledb_report.md").open("a", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n\n---\n\n")
        return {"summary": summary}


def run(home: Path, dsn: str, embedder: Embedder, **kw) -> dict[str, Any]:
    """One table-database pass (used by kg_agent's ``--tables`` hook and by the CLI)."""
    return Agent(Settings(home=Path(home), dsn=dsn, embedder=embedder, **kw)).run()


def status(home: Path, dsn: str, schema: str = SCHEMA) -> dict[str, Any]:
    db = TableDB(dsn, schema)
    conn = db.connect()
    try:
        meta = db.meta(conn)
        if meta is None:
            return {"initialised": False}
        n = conn.execute("SELECT count(*), coalesce(sum(n_rows),0), count(DISTINCT docid) FROM kgt_tables").fetchone()
        j = conn.execute("SELECT count(*) FROM kgt_joins WHERE kind = 'join'").fetchone()[0]
        return {"initialised": True, "embedding": meta, "tables": n[0], "rows": int(n[1]), "documents": n[2], "joins": j}
    finally:
        conn.close()


# ============================================================================================================ CLI
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build / query the PostgreSQL + pgvector database of the tables extracted from the staged documents.")
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("COGENT_HOME", "kg_agent_home")))
    ap.add_argument("--dsn", default=os.environ.get("COGENT_TABLEDB_DSN"), help="postgresql://user:pass@host/db (or COGENT_TABLEDB_DSN)")
    ap.add_argument("--embedder", choices=["ollama", "hash"], default=os.environ.get("COGENT_EMBEDDER", "ollama"))
    ap.add_argument("--embed-model", default=None, help="Ollama embedding model (default nomic-embed-text)")
    ap.add_argument("--ollama-host", default=None)
    ap.add_argument("--embed-dim", type=int, default=None, help="dimension for --embedder hash (default 64)")
    ap.add_argument("--schema", default=SCHEMA, help="database schema (default kgtables)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="load / refresh the tables (incremental)")
    b.add_argument("--only", help="comma-separated entries (standard:22) or document ids")
    b.add_argument("--force", action="store_true", help="reload and re-embed every table in scope")
    b.add_argument("--rebuild", action="store_true", help="drop the whole database first (after changing embedder or recipe)")
    b.add_argument("--row-mode", choices=K.ROW_MODES, default="full", help="full: embed every cell of a row (default); text: leave numeric values out")
    b.add_argument("--llm-joins", action="store_true", help="also infer joins with an Ollama LLM and evaluate all candidates with an LLM judge")
    b.add_argument("--llm-model", default=os.environ.get("COGENT_LLM_MODEL"), help="Ollama chat model for the suggester (default llama3.1:8b)")
    b.add_argument("--judge-model", default=None, help="Ollama chat model for the judge (default: the suggester model)")
    b.add_argument("--llm-samples", type=int, default=3, help="independent suggestion runs per table pair")
    b.add_argument("--judge-runs", type=int, default=3, help="judge votes per candidate")
    b.add_argument("--llm-max-pairs", type=int, default=40, help="table pairs per document sent to the suggester")
    b.add_argument("--llm-max-seconds", type=float, default=None, help="time budget per document for the LLM stage")
    b.add_argument("--no-kg-context", action="store_true", help="do not use the KG neighbourhood in the embedded text")
    sub.add_parser("status", help="what is in the database")
    q = sub.add_parser("search", help="semantic search over the tables")
    q.add_argument("question")
    q.add_argument("-k", type=int, default=5)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if a.cmd == "status":
            print(json.dumps(status(a.home, a.dsn, a.schema), indent=2, default=str))
            return 0
        emb = get_embedder(a.embedder, a.embed_model, a.ollama_host, a.embed_dim)
        if a.cmd == "build":
            s = run(a.home, a.dsn, emb, only=[x for x in (a.only or "").split(",") if x], force=a.force, rebuild=a.rebuild, kg_context=not a.no_kg_context, schema=a.schema, row_mode=a.row_mode,
                  **llm_kwargs(a.llm_joins, a.llm_model, a.judge_model, a.ollama_host, a.llm_samples, a.judge_runs, a.llm_max_pairs, a.llm_max_seconds))
            print(json.dumps(s, indent=2))
            return 1 if s["errors"] else 0
        db = TableDB(a.dsn, a.schema)
        conn = db.connect()
        try:
            qv = emb.embed_query(a.question)
            tabs = db.search_tables(conn, qv, a.question, None, a.k)
            for hit in db.search_rows(conn, qv, a.question, tabs, a.k):
                print(json.dumps(hit, ensure_ascii=False))
        finally:
            conn.close()
        return 0
    except (TableDBError, EmbedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


def llm_kwargs(enabled: bool, model: Optional[str], judge_model: Optional[str], host: Optional[str], samples: int = 3, judge_runs: int = 3,
               max_pairs: int = 40, max_seconds: Optional[float] = None) -> dict[str, Any]:
    """Settings for the LLM join stage (empty when disabled)."""
    if not enabled:
        return {}
    llm = get_llm("ollama", model, host)
    judge = get_llm("ollama", judge_model, host) if judge_model and judge_model != llm.model else None
    return {"llm": llm, "judge_llm": judge, "llm_cfg": L.Config(samples=samples, judge_runs=judge_runs, max_pairs=max_pairs, max_seconds=max_seconds)}


def load_tables(home: Path, docid: str, kg_context: bool = True) -> list[TableRecord]:
    """The prepared tables (typed columns, KG context, unit candidates) of one staged document, without touching any database:
    the input of join inference when it is run on request (kg_tableservice.evaluate_joins)."""
    agent = Agent(Settings(home=Path(home), dsn="unused", embedder=None, kg_context=kg_context))      # type: ignore[arg-type]
    return [r for r in (agent.prepare(x) for x in discover(Path(home)) if x.docid == docid) if r is not None]
