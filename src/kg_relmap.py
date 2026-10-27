"""Join inference between the tables of one source document, and the published data dictionary / relationship map.

For every pair of tables of a document the candidate join columns are scored from

* **column names**  near-identical after normalisation (case, punctuation, units in brackets, 'no.' = 'number', plural 's');
* **values**        how many of the distinct values of the smaller column occur in the other (containment) and how alike
                    the two sets are (Jaccard); decimal-valued columns are never join keys;
* **the KG**        tables in the same section, a table that refers to the other ("see Table 5-2" in its caption, intro,
                    notes or a paragraph that cites it), shared entities.

score = 0.40 name + 0.45 value containment + 0.05 same type + 0.10 KG boost; a pair is kept at >= 0.55. The evidence for each
relationship is recorded next to its score, and cardinality (1:1, 1:N, N:1, N:M) follows from key-uniqueness of the two sides.
Tables with (nearly) the same columns are reported as ``same_schema`` (typically one table split in parts: appendable, not joinable).

Outputs: ``data_dictionary.json|md`` and ``relationships.json|mmd`` (Mermaid ER diagram). Pure functions; no DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional

from kg_tablecheck import name_share
from kg_tables import Col, Context, parse_number

THRESHOLD = 0.55
VERSION = "3"                  # bump when the deterministic scoring changes (republishes relationships)
MAX_PER_PAIR = 3
_SYN = {"no": "no", "num": "no", "number": "no", "nr": "no", "#": "no", "cat": "category", "id": "id", "ident": "id"}
_STOP = {"the", "of", "for", "and", "a", "an", "in", "to"}


def norm_name(header: str) -> str:
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", header.lower())
    toks = re.findall(r"[a-z0-9#]+", s)
    out = []
    for t in toks:
        t = _SYN.get(t, t)
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
            t = t[:-1]
        if t not in _STOP:
            out.append(t)
    return " ".join(out)


_PLACEHOLDER = re.compile(r"^(?:column|col|field|unnamed)[\s_:-]*\d+$|^c_\d+$", re.I)     # names the extractor generates; "C1" / "f1" can be real variables
_ORDINAL = re.compile(r"^\(?\d{1,3}[.)]?$")
_XREF = re.compile(r"^(?:tables?|figures?|fig\.?|sections?|appendix|see|entire|refer)\b", re.I)
_TABLE_REF = re.compile(r"\btable\s*([a-z]?\d+(?:[-.]\d+)*)", re.I)


def placeholder(header: str) -> bool:
    """A header that only numbers the column (``column_1``, ``c_4``, empty): it says nothing about what the column holds."""
    h = (header or "").strip()
    return not h or bool(_PLACEHOLDER.match(h)) or bool(_ORDINAL.match(h))      # a header that is only a list number ("1.", "2)") numbers the column too


def junk_values(common: set[str]) -> tuple[bool, bool]:
    """(all shared values are list markers like '2.' / '3', most shared values are cross-references like 'table 1:' or 'entire repo')."""
    if not common:
        return False, False
    return all(_ORDINAL.match(v) for v in common), sum(bool(_XREF.match(v)) for v in common) / len(common) >= 0.5


def _table_numbers(t: "T") -> set[str]:
    nums = set()
    for s in (t.ctx.table_label or "", t.ctx.caption or ""):
        nums |= {m.lower().replace(".", "-") for m in _TABLE_REF.findall(s)}
    return nums


def names_table(a: "T", ca: Col, b: "T") -> bool:
    """Does the header or the first cell of column ``ca`` of ``a`` name table ``b`` ('Isotope Name Used in Table 1-1')? Evidence that the column is a key into ``b``."""
    j = a.cols.index(ca)
    heads = [ca.header] + ([a.rows[0][j]] if a.rows and j < len(a.rows[0]) else [])
    want = _table_numbers(b)
    return bool(want) and any(m.lower().replace(".", "-") in want for h in heads for m in _TABLE_REF.findall(h or ""))


def name_similarity(a: str, b: str) -> float:
    if placeholder(a) or placeholder(b):               # 'column_1' = 'column_1' is a coincidence of numbering, not a shared name
        return 0.0
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    return max(len(ta & tb) / len(ta | tb), SequenceMatcher(None, na, nb).ratio())


def value_set(cells: list[str]) -> set[str]:
    out = set()
    for c in cells:
        c = re.sub(r"\s+", " ", c).strip().lower()
        if not c:
            continue
        p = parse_number(c)
        out.add(f"{p[0]:g}" if p and not p[1] else c)
    return out


@dataclass
class T:
    """One table as the relationship code sees it."""
    table_id: str
    node_id: str
    cols: list[Col]
    rows: list[list[str]]
    ctx: Context


def pair_features(a: T, ca: Col, b: T, cb: Col, boost: Optional[tuple[float, list[str]]] = None) -> dict[str, Any]:
    """Deterministic facts about a column pair (for the judge and for scoring), whether or not any method proposed it."""
    ia, ib = a.cols.index(ca), b.cols.index(cb)
    va, vb = value_set([r[ia] for r in a.rows]), value_set([r[ib] for r in b.rows])
    common = va & vb
    kg, kg_ev = boost if boost is not None else _kg_boost(a, b)
    return {"name_similarity": round(name_similarity(ca.header, cb.header), 3),
            "containment": round(len(common) / min(len(va), len(vb)), 3) if va and vb else 0.0,
            "jaccard": round(len(common) / len(va | vb), 3) if (va or vb) else 0.0,
            "n_common": len(common), "n_left": len(va), "n_right": len(vb), "shared_values": sorted(common)[:5],
            "left_unique": ca.key_candidate, "right_unique": cb.key_candidate,
            "left_type": ca.pg_type, "right_type": cb.pg_type, "kg_boost": round(kg, 3), "kg_evidence": kg_ev}


def cardinality(ca: Col, cb: Col) -> str:
    return "1:1" if ca.key_candidate and cb.key_candidate else "1:N" if ca.key_candidate else "N:1" if cb.key_candidate else "N:M"


def _joinable(c: Col) -> bool:
    return c.n_distinct >= 2 and c.pg_type != "double precision"


def _kg_boost(a: T, b: T) -> tuple[float, list[str]]:
    ev, boost = [], 0.0
    pa, pb = a.ctx.section_path, b.ctx.section_path
    if pa and pb and pa[-1] == pb[-1]:
        boost, ev = boost + 0.5, ev + [f"same section: {pa[-1]}"]
    elif pa and pb and pa[0] == pb[0]:
        boost, ev = boost + 0.25, ev + [f"same top-level section: {pa[0]}"]
    texts = lambda c: " ".join([c.intro, *c.notes]).lower()
    refd = (b.node_id in a.ctx.referenced_by or a.node_id in b.ctx.referenced_by
            or (b.ctx.table_label and b.ctx.table_label.lower() in texts(a.ctx))
            or (a.ctx.table_label and a.ctx.table_label.lower() in texts(b.ctx)))
    if refd:
        boost, ev = boost + 1.0, ev + [f"{a.ctx.table_label or a.table_id} and {b.ctx.table_label or b.table_id} refer to each other"]
    shared = sorted(set(a.ctx.entities) & set(b.ctx.entities))
    if len(shared) >= 2:
        boost, ev = boost + 0.5, ev + ["shared entities: " + ", ".join(shared[:4])]
    return min(1.0, boost), ev


def near_misses(tables: list[T], limit: int = 3) -> list[dict[str, Any]]:
    """The best column pairs that the join rules rejected, with what was missing (to explain 'no joins found')."""
    vals = {(t.table_id, c.name): value_set([r[j] for r in t.rows]) for t in tables for j, c in enumerate(t.cols)}
    out = []
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            for ca in a.cols:
                for cb in b.cols:
                    if not (_joinable(ca) and _joinable(cb)):
                        continue
                    va, vb = vals[(a.table_id, ca.name)], vals[(b.table_id, cb.name)]
                    common = va & vb
                    sim = name_similarity(ca.header, cb.header)
                    contain = len(common) / min(len(va), len(vb)) if common else 0.0
                    if sim < 0.5 and contain == 0:
                        continue
                    why = ("no shared values" if not common else "too few shared values" if contain < 0.3 else "") or \
                          ("column names differ" if sim < 0.85 else "") or "score below the threshold"
                    out.append({"left_table": a.table_id, "left_column": ca.header, "right_table": b.table_id, "right_column": cb.header,
                                "name_similarity": round(sim, 2), "containment": round(contain, 2), "missing": why})
    out.sort(key=lambda x: -(0.4 * x["name_similarity"] + 0.6 * x["containment"]))
    return out[:limit]


def infer(tables: list[T]) -> list[dict[str, Any]]:
    """Join and same-schema relationships among ``tables`` (all from one source document)."""
    rels: list[dict[str, Any]] = []
    vals = {(t.table_id, c.name): value_set([r[j] for r in t.rows]) for t in tables for j, c in enumerate(t.cols)}
    # columns of person names (committee rosters, author lists) are not join keys: the same person in two lists is not a relationship
    people = {(t.table_id, c.name) for t in tables for j, c in enumerate(t.cols) if c.pg_type == "text" and name_share([r[j] for r in t.rows]) >= 0.7}
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            boost, kg_ev = _kg_boost(a, b)
            found = []
            for ca in a.cols:
                for cb in b.cols:
                    if not (_joinable(ca) and _joinable(cb)):
                        continue
                    if (a.table_id, ca.name) in people and (b.table_id, cb.name) in people:
                        continue
                    va, vb = vals[(a.table_id, ca.name)], vals[(b.table_id, cb.name)]
                    common = va & vb
                    if not common:
                        continue
                    contain = len(common) / min(len(va), len(vb))
                    jac = len(common) / len(va | vb)
                    sim = name_similarity(ca.header, cb.header)
                    ordinal, xref = junk_values(common)
                    ref = names_table(a, ca, b) or names_table(b, cb, a)         # a header / first cell says "... Used in Table 1-1"
                    strong_name = sim >= 0.85 and contain >= 0.3
                    # values alone carry a join only when they identify something: texts, mostly shared, enough of them, and unique on at least one side
                    strong_vals = (contain >= 0.8 and min(len(va), len(vb)) >= 4 and ca.pg_type == "text" and cb.pg_type == "text"
                                   and len(common) >= 5 and (ca.key_candidate or cb.key_candidate))
                    if xref or (ordinal and not strong_name) or not (strong_name or strong_vals):
                        continue                                                   # list numbers and table references are not keys
                    score = 0.40 * sim + 0.45 * contain + 0.05 * (ca.pg_type == cb.pg_type) + 0.10 * boost
                    if strong_vals:                                                # meaningless headers (column_1) must not cap the score of well-matched values
                        score = max(score, 0.60 * contain + 0.15 * 1 + 0.05 * (ca.pg_type == cb.pg_type) + 0.10 * min(1.0, len(common) / 20) + 0.10 * boost)
                    if ref:
                        score = min(1.0, score + 0.10)
                    if score < THRESHOLD:
                        continue
                    ua, ub = ca.key_candidate, cb.key_candidate
                    card = "1:1" if ua and ub else "1:N" if ua else "N:1" if ub else "N:M"
                    left, right, lc, rc = (a, b, ca, cb) if card != "N:1" else (b, a, cb, ca)
                    card = "1:N" if card == "N:1" else card
                    ev = ([f"column names {'identical' if sim == 1 else 'near-identical'} ({ca.header!r} ~ {cb.header!r}, {sim:.2f})"] if sim >= 0.85 else
                          [f"column names say little ({ca.header!r}, {cb.header!r}); the values identify the match"] if placeholder(ca.header) or placeholder(cb.header) else []) + \
                         [f"{len(common)} of {min(len(va), len(vb))} distinct values in common (Jaccard {jac:.2f})"] + \
                         (["a header or first cell names the other table"] if ref else []) + kg_ev
                    found.append({"kind": "join", "left_table": left.table_id, "left_column": lc.name, "left_header": lc.header,
                                  "right_table": right.table_id, "right_column": rc.name, "right_header": rc.header,
                                  "cardinality": card, "score": round(score, 3), "name_similarity": round(sim, 3),
                                  "containment": round(contain, 3), "jaccard": round(jac, 3), "evidence": ev,
                                  "shared_values": sorted(common)[:5]})
            found.sort(key=lambda r: -r["score"])
            rels += found[:MAX_PER_PAIR]
            na, nb = {norm_name(c.header) for c in a.cols}, {norm_name(c.header) for c in b.cols}
            if sum(not placeholder(c.header) for c in a.cols) >= 3 and len(na & nb) / len(na | nb) >= 0.8:
                rels.append({"kind": "same_schema", "left_table": a.table_id, "right_table": b.table_id,
                             "score": round(len(na & nb) / len(na | nb), 3),
                             "evidence": [f"{len(na & nb)} of {len(na | nb)} column names in common: the tables look like parts of one table"] + kg_ev})
    return rels


# --------------------------------------------------------------------------------------------- publication
def data_dictionary(docid: str, doc_label: str, source_url: str, tables: list[dict[str, Any]], rels: list[dict[str, Any]],
                    paywalled: bool = False) -> dict[str, Any]:
    """``tables``: registry-style dicts (table_id, pg_table, node_id, caption, page, section_path, n_rows, columns: [Col dicts])."""
    links: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rels:
        if r["kind"] != "join":
            continue
        links.setdefault((r["left_table"], r["left_column"]), []).append(
            {"table": r["right_table"], "column": r["right_column"], "cardinality": r["cardinality"], "score": r["score"]})
        links.setdefault((r["right_table"], r["right_column"]), []).append(
            {"table": r["left_table"], "column": r["left_column"], "cardinality": r["cardinality"][::-1], "score": r["score"]})
    out_tables = []
    for t in tables:
        cols = [{**c, "joins": links.get((t["table_id"], c["name"]), [])} for c in t["columns"]]
        out_tables.append({**{k: v for k, v in t.items() if k != "columns"}, "columns": cols})
    return {"document_id": docid, "document": doc_label, "source": source_url, "paywalled": paywalled,
            "tables": out_tables, "relationships": len([r for r in rels if r["kind"] == "join"]),
            "note": "Generated from tables extracted from the document; column types and join candidates are inferred and "
                    "may be wrong. Verify against the source document."}


def dictionary_markdown(d: dict[str, Any], rels: list[dict[str, Any]]) -> str:
    L = [f"# Data dictionary: {d['document'] or d['document_id']}", "", f"Source: {d['source']}", ""]
    if d.get("paywalled"):
        L += ["> **Copyrighted material** from a paywalled standard (purchased copy). For reference only; not for redistribution.", ""]
    L += [d["note"], ""]
    for t in d["tables"]:
        L += [f"## {t['caption'] or t['table_id']}", "",
              f"`{t['table_id']}` · database table `{t['pg_table']}` · page {t.get('page') or '?'} · {t['n_rows']} rows"
              + (f" · section: {' > '.join(t['section_path'])}" if t.get("section_path") else ""), "",
              "| column | header | type | unit | distinct | key? | samples | joins to |", "|---|---|---|---|---|---|---|---|"]
        for c in t["columns"]:
            j = "; ".join(f"{x['table'].split('/')[-1]}.{x['column']} ({x['cardinality']}, {x['score']})" for x in c["joins"]) or "-"
            L.append(f"| `{c['name']}` | {c['header']} | {c['pg_type']} | {c.get('unit') or ''} | {c['n_distinct']} | "
                     f"{'yes' if c['key_candidate'] else ''} | {'; '.join(c['samples'][:3]).replace('|', '/')} | {j} |")
        L.append("")
    if rels:
        L += ["## Relationship map", ""]
        for r in rels:
            if r["kind"] == "join":
                L.append(f"- `{r['left_table'].split('/')[-1]}.{r['left_column']}` {r['cardinality']} "
                         f"`{r['right_table'].split('/')[-1]}.{r['right_column']}` (score {r['score']}): " + "; ".join(r["evidence"]))
            else:
                L.append(f"- `{r['left_table'].split('/')[-1]}` ≈ `{r['right_table'].split('/')[-1]}` (same schema, score {r['score']}): {r['evidence'][0]}")
        L.append("")
    return "\n".join(L)


def relationships_mermaid(d: dict[str, Any], rels: list[dict[str, Any]]) -> str:
    """Mermaid ``erDiagram`` of the tables and inferred joins."""
    ent = lambda tid: "t_" + re.sub(r"\W", "_", tid.split("/")[-1])
    L = ["erDiagram"]
    for t in d["tables"]:
        L.append(f"  {ent(t['table_id'])} {{")
        for c in t["columns"][:12]:
            ty = "float" if c["pg_type"] == "double precision" else "int" if c["pg_type"] == "bigint" else "string"
            L.append(f"    {ty} {c['name']}{' PK' if c['key_candidate'] else ''}")
        L.append("  }")
    sym = {"1:1": "||--||", "1:N": "||--o{", "N:M": "}o--o{"}
    for r in rels:
        if r["kind"] == "join":
            L.append(f"  {ent(r['left_table'])} {sym.get(r['cardinality'], '}o--o{')} {ent(r['right_table'])} : \"{r['left_column']} = {r['right_column']}\"")
        else:
            L.append(f"  %% same schema: {ent(r['left_table'])} ~ {ent(r['right_table'])}")
    return "\n".join(L) + "\n"
