"""Secondary staging step: one combined graph that correlates which reports depend on which standards.

Runs as the last stage of every agent run (so it can never lag the primary staging) and is fully derived: it can be
deleted and rebuilt at any time. Output in ``<home>/staging/combined/``:

    kg.json          entry nodes + relations between entries (no body text: paywalled content is never merged)
    dependents.json  per entry: who depends on it / what it depends on, curated and inferred separately
    meta.json        built time, run id, per-input fingerprints, counts

Edges carry ``basis`` and ``confidence``:
    complies_with / overlaps_with   basis=config, confidence=curated   (from standards.json / reports.json)
    cites                           basis=text,   confidence=inferred  (an entry's text cites an identifier, e.g. 'RG 1.233',
                                                                        that is another entry's own identity; ``count`` = mentions)
"""
from __future__ import annotations

import hashlib
import json
import re
import os
from pathlib import Path
from typing import Any, Iterable, Optional

import networkx as nx

COMBINED = Path("staging") / "combined"


def _entry_dir(home: Path, key: str) -> Path:
    kind, ident = key.split(":")
    return home / "staging" / f"{kind}s" / ident


def cited_identifiers(kg_path: Path) -> dict[str, int]:
    """``ident:`` labels cited in an entry graph -> number of citing elements."""
    try:
        g = nx.node_link_graph(json.loads(kg_path.read_text(encoding="utf-8")), edges="edges")
    except (OSError, ValueError):
        return {}
    out: dict[str, int] = {}
    for _, v, d in g.edges(data=True):
        if d.get("relation") == "CITES" and "ident:" in str(v):
            label = str(v).split("ident:", 1)[1]
            out[label] = out.get(label, 0) + 1
    return out


_CFR_SECTION = re.compile(r"^CFR (\d+)\.\S+$")


def resolve_owner(label: str, citing: str, owner: dict[str, set[str]], by_key: dict[str, dict[str, Any]],
                  docs_of: dict[str, set[str]]) -> tuple[Optional[str], list[str]]:
    """The one entry that a citation of identifier ``label`` points to, and the other entries the label also matched.

    * An entry that IS the cited document (it owns the label) or shares a source document with the citing entry does not cite itself.
    * Otherwise the most specific owner wins: standards before reports, then the entry that stands for fewer identifiers (``CFR 50`` is Part 50,
      not an entry that covers Appendix B and a guide as well), then the lower key. The losers are returned so the edge can show them."""
    owners = owner.get(label, set())
    if citing in owners:
        return None, []
    cands = [o for o in owners if not (docs_of.get(o, set()) & docs_of.get(citing, set()))]
    if not cands:
        return None, []
    cands.sort(key=lambda o: (by_key[o]["kind"] != "standard", len(by_key[o].get("identities", [])), o))
    return cands[0], cands[1:]


def build(home: Path, entries: Iterable[dict[str, Any]], run_id: str = "") -> tuple[nx.MultiDiGraph, dict[str, Any], dict[str, Any]]:
    """``entries``: dicts with key, kind, name, paywalled, complies_with, overlaps, identities (labels),
    version_status, latest (labels), on_board (labels). Returns (graph, dependents, meta)."""
    entries = list(entries)
    by_key = {e["key"]: e for e in entries}
    owner: dict[str, set[str]] = {}                       # identity label -> entries that ARE that document
    for e in entries:
        for lab in e.get("identities", []):
            owner.setdefault(lab, set()).add(e["key"])

    cited: dict[str, dict[str, int]] = {}
    g = nx.MultiDiGraph(built="", run_id=run_id, note="derived; curated edges from configuration, 'cites' edges inferred from text")
    inputs: dict[str, str] = {}
    cites: dict[tuple[str, str], tuple[int, list[str]]] = {}
    docs_of: dict[str, set[str]] = {}                      # entry -> ids of its source documents (entries that share a document are one source)
    alternatives: dict[tuple[str, str], set[str]] = {}     # (citing, target) -> other entries the label also matched but were not chosen
    for e in entries:
        kgp = _entry_dir(home, e["key"]) / "kg.json"
        meta = {}
        try:
            meta = json.loads((_entry_dir(home, e["key"]) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        inputs[e["key"]] = meta.get("signature", "")[:16] if kgp.exists() else "none"
        docs_of[e["key"]] = {d.get("docid") for d in meta.get("documents", []) if d.get("docid")}
        g.add_node(f"entry:{e['key']}", type=e["kind"].capitalize(), label=e["name"], kind=e["kind"], paywalled=e["paywalled"],
                   has_graph=kgp.exists(), graph_built=meta.get("built"), version_status=e.get("version_status"),
                   latest_available=e.get("latest", []), on_board=e.get("on_board", []))
        if kgp.exists():
            cited[e["key"]] = cited_identifiers(kgp)
    for src, labels in cited.items():
        for lab, n in labels.items():
            tgt, others = resolve_owner(lab, src, owner, by_key, docs_of)
            if tgt is None:
                continue
            c, labs = cites.get((src, tgt), (0, []))
            cites[(src, tgt)] = (c + n, labs + [lab])
            if others:
                alternatives.setdefault((src, tgt), set()).update(others)
    dep: dict[str, dict[str, Any]] = {e["key"]: {"depends_on": [], "declared_dependents": [], "overlaps": [], "cited_by": [], "cites": [], "part_of": []}
                                      for e in entries}
    for e in entries:
        for sid in e.get("complies_with", []):
            t = f"standard:{sid}"
            if t in by_key:
                g.add_edge(f"entry:{e['key']}", f"entry:{t}", key="complies_with", relation="complies_with", basis="config", confidence="curated")
                dep[e["key"]]["depends_on"].append(t)
                dep[t]["declared_dependents"].append(e["key"])
        for sid in e.get("overlaps", []):
            t = f"standard:{sid}"
            if t in by_key and t != e["key"]:
                g.add_edge(f"entry:{e['key']}", f"entry:{t}", key="overlaps_with", relation="overlaps_with", basis="config", confidence="curated")
                for a, b in ((e["key"], t), (t, e["key"])):
                    if b not in dep[a]["overlaps"]:
                        dep[a]["overlaps"].append(b)
    for (src, tgt), (n, labs) in sorted(cites.items()):
        alt = sorted(alternatives.get((src, tgt), ()))
        g.add_edge(f"entry:{src}", f"entry:{tgt}", key="cites", relation="cites", basis="text", confidence="inferred",
                   count=n, identifiers=sorted(set(labs)), **({"also_matched": alt} if alt else {}))
        dep[src]["cites"].append({"entry": tgt, "count": n, "identifiers": sorted(set(labs))})
        dep[tgt]["cited_by"].append({"entry": src, "count": n, "identifiers": sorted(set(labs))})
    for e in entries:                                      # 10 CFR 50.69 is part of 10 CFR 50 (when another standard entry is that part)
        for lab in e.get("identities", []):
            m = _CFR_SECTION.match(lab)
            if not m:
                continue
            tgt, _ = resolve_owner(f"CFR {m.group(1)}", e["key"], owner, by_key, docs_of)
            if tgt and tgt not in dep[e["key"]]["part_of"]:
                g.add_edge(f"entry:{e['key']}", f"entry:{tgt}", key="part_of", relation="part_of", basis="identifier", confidence="inferred",
                           identifiers=[lab, f"CFR {m.group(1)}"])
                dep[e["key"]]["part_of"].append(tgt)
    for d in dep.values():
        for k in ("depends_on", "declared_dependents", "overlaps"):
            d[k] = sorted(d[k])

    def affected(key: str) -> dict[str, Any]:
        """Reports that are affected if ``key`` changes: declared (curated) first; inferred listed separately."""
        declared = [r for r in dep[key]["declared_dependents"] if by_key[r]["kind"] == "report"]
        # a change in a standard also touches reports of the standards it overlaps with (curated overlap)
        via_overlap = sorted({r for o in dep[key]["overlaps"] for r in dep[o]["declared_dependents"]
                              if by_key[r]["kind"] == "report"} - set(declared))
        inferred = sorted({c["entry"] for c in dep[key]["cited_by"] if by_key[c["entry"]]["kind"] == "report"} - set(declared) - set(via_overlap))
        return {"declared": declared, "via_overlap": via_overlap, "inferred": inferred}
    for key in dep:
        dep[key]["affects_reports"] = affected(key) if by_key[key]["kind"] == "standard" else {"declared": [], "via_overlap": [], "inferred": []}
    meta = {"run_id": run_id, "nodes": g.number_of_nodes(), "edges": g.number_of_edges(), "inputs": inputs,
            "entries_without_graph": sorted(k for k, v in inputs.items() if v == "none"),
            "signature": hashlib.sha256(json.dumps([inputs, dep], sort_keys=True).encode()).hexdigest()[:16]}
    return g, dep, meta


def write(home: Path, g: nx.MultiDiGraph, dep: dict[str, Any], meta: dict[str, Any], built: str) -> bool:
    """Write the three files atomically; returns False when nothing changed."""
    d = home / COMBINED
    old = {}
    try:
        old = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if old.get("signature") == meta["signature"] and (d / "kg.json").exists():
        return False
    d.mkdir(parents=True, exist_ok=True)
    g.graph["built"] = meta["built"] = built
    for name, obj in (("kg.json", nx.node_link_data(g, edges="edges")), ("dependents.json", dep), ("meta.json", meta)):
        tmp = d / f".{name}.tmp"
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, d / name)
    return True


def load_dependents(home: Path) -> dict[str, Any]:
    try:
        return json.loads((home / COMBINED / "dependents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
