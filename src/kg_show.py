"""Visualise knowledge graphs produced by ``kg_ingest`` for inspection.

Produces a single self-contained, interactive HTML page (vis-network) with:

* a sidebar to search nodes, toggle node types and edge groups
  (hierarchy / reading order / mentions / extracted relations);
* a details panel (all attributes of the clicked node or edge; tables are
  rendered as tables) so you can trace a fact back to its source element/page;
* a statistics summary.

A static PNG (matplotlib) can be produced as well.

CLI::

    python kg_show.py paper.pdf                 # build (or load cached) KG, write HTML
    python kg_show.py kg_store/<hash>.json      # visualise a saved graph
    python kg_show.py paper.pdf -o kg.html --png kg.png --open

Python::

    from kg_show import render_html
    render_html(kg, "kg.html")

Install: ``pip install networkx`` (+ ``pyvis`` so the vis-network library can be
inlined for offline use, else it is loaded from a CDN; + ``matplotlib`` for PNG).
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Optional

import networkx as nx

# Colour per node type (unknown types fall back to grey).
TYPE_COLORS = {
    "Document": "#111827", "Heading": "#7c3aed", "Paragraph": "#9ca3af",
    "Table": "#d97706", "Figure": "#db2777", "Entity": "#2563eb",
    "TableRow": "#0d9488", "Value": "#65a30d",
}
STRUCTURAL = {"CONTAINS", "NEXT", "MENTIONS"}
_SIZES = {"Document": 28, "Heading": 20, "Table": 18, "Figure": 18}

CDN_URL = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_graph(src: str, out_dir: Optional[str] = None,
               assets_dir: Optional[str] = None) -> nx.MultiDiGraph:
    """Return a graph from a saved ``.json`` / ``.graphml`` file, or from a
    PDF/HTML path or URL (uses the saved KG if one exists, else builds it).
    With ``assets_dir`` the document is always rebuilt so that its figures and
    tables are exported there."""
    suffix = Path(src).suffix.lower()
    if suffix == ".json" and Path(src).is_file():
        g = nx.node_link_graph(json.loads(Path(src).read_text()), edges="edges")
    elif suffix == ".graphml" and Path(src).is_file():
        g = nx.read_graphml(src)
    else:
        from kg_ingest import (file_to_knowledge_graph, load_knowledge_graph,
                               save_knowledge_graph)
        g = None if assets_dir else load_knowledge_graph(src, out_dir)
        if g is None:
            g = file_to_knowledge_graph(src, assets_dir=assets_dir)
            save_knowledge_graph(g, src, out_dir)
    if not isinstance(g, nx.MultiDiGraph):
        g = nx.MultiDiGraph(g)
    return g


# --------------------------------------------------------------------------
# Conversion to vis-network data
# --------------------------------------------------------------------------


def _group_of(relation: str) -> str:
    return relation if relation in STRUCTURAL else "RELATION"


def to_vis_data(g: nx.MultiDiGraph) -> dict:
    """Convert a graph into JSON-serialisable nodes/edges for vis-network."""
    degree = dict(g.degree())
    nodes = []
    for n, d in g.nodes(data=True):
        typ = str(d.get("type", "Entity"))
        label = str(d.get("label") or n)
        nodes.append({
            "id": str(n), "type": typ,
            "label": label if len(label) <= 28 else label[:26] + "…",
            "color": TYPE_COLORS.get(typ, "#6b7280"),
            "size": _SIZES.get(typ, 10 + min(degree.get(n, 0), 12)),
            "attrs": {str(k): str(v) for k, v in d.items()},
        })
    edges = []
    for i, (u, v, k, d) in enumerate(g.edges(keys=True, data=True)):
        rel = str(d.get("relation", k))
        edges.append({
            "id": i, "from": str(u), "to": str(v), "relation": rel,
            "group": _group_of(rel),
            "label": "" if rel in STRUCTURAL else rel.replace("_", " "),
            "attrs": {str(a): str(b) for a, b in d.items()},
        })
    return {"nodes": nodes, "edges": edges, "colors": TYPE_COLORS}


def graph_stats(g: nx.MultiDiGraph) -> dict:
    types = Counter(str(d.get("type", "Entity")) for _, d in g.nodes(data=True))
    rels = Counter(str(d.get("relation", k)) for _, _, k, d in g.edges(keys=True, data=True))
    return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
            "node_types": dict(types), "relations": dict(rels.most_common(15))}


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------


def _vis_script() -> str:
    """Inline vis-network if bundled with pyvis (offline), else CDN <script>."""
    try:
        import pyvis
        for ver in ("vis-9.1.2", "vis-9.0.4"):
            p = Path(pyvis.__file__).parent / "lib" / ver / "vis-network.min.js"
            if p.is_file():
                return "<script>" + p.read_text(encoding="utf-8").replace("</script", "<\\/script") + "</script>"
    except ImportError:
        pass
    return f'<script src="{CDN_URL}"></script>'


def _json_for_html(obj) -> str:
    """JSON safe to embed in a <script> block."""
    return (json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_html(g: nx.MultiDiGraph, out_path: str = "kg.html",
                title: Optional[str] = None) -> Path:
    """Write an interactive HTML visualisation of ``g`` and return its path."""
    title = title or str(g.graph.get("source", "Knowledge graph"))
    page = (_TEMPLATE
            .replace("__TITLE__", _html_escape(title))
            .replace("__VIS__", _vis_script())
            .replace("__DATA__", _json_for_html(to_vis_data(g)))
            .replace("__STATS__", _json_for_html(graph_stats(g))))
    out = Path(out_path)
    out.write_text(page, encoding="utf-8")
    return out


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# --------------------------------------------------------------------------
# Static PNG
# --------------------------------------------------------------------------


def render_png(g: nx.MultiDiGraph, out_path: str = "kg.png",
               include_structure: bool = False, figsize=(16, 11)) -> Path:
    """Static matplotlib rendering. By default only entities and the relations
    between them are drawn; ``include_structure=True`` adds all nodes/edges."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    h = nx.Graph()  # collapse parallel edges for layout
    labels: dict[str, list[str]] = {}
    for n, d in g.nodes(data=True):
        if include_structure or d.get("type") in ("Entity", "TableRow", "Value"):
            h.add_node(n, type=str(d.get("type", "Entity")), label=str(d.get("label", n)))
    for u, v, k, d in g.edges(keys=True, data=True):
        rel = str(d.get("relation", k))
        if u in h and v in h and (include_structure or rel not in STRUCTURAL):
            h.add_edge(u, v)
            if rel not in STRUCTURAL:
                labels.setdefault((u, v), []).append(rel.replace("_", " "))
    fig, ax = plt.subplots(figsize=figsize)
    if h.number_of_nodes():
        pos = nx.spring_layout(h, k=1.2 / max(h.number_of_nodes(), 1) ** 0.5, seed=7)
        nx.draw_networkx_edges(g.subgraph(h.nodes), pos, ax=ax, alpha=0.4,
                               edge_color="#9ca3af", arrows=True, arrowsize=10)
        nx.draw_networkx_nodes(
            h, pos, ax=ax, node_size=[120 + 60 * h.degree(n) for n in h],
            node_color=[TYPE_COLORS.get(h.nodes[n]["type"], "#6b7280") for n in h])
        nx.draw_networkx_labels(h, pos, {n: h.nodes[n]["label"][:24] for n in h},
                                font_size=7, ax=ax)
        nx.draw_networkx_edge_labels(
            h, pos, {k: ", ".join(dict.fromkeys(v))[:30] for k, v in labels.items()},
            font_size=6, font_color="#374151", ax=ax)
    handles = [plt.Line2D([], [], marker="o", ls="", color=c, label=t)
               for t, c in TYPE_COLORS.items()]
    ax.legend(handles=handles, loc="upper left", fontsize=8)
    ax.set_title(str(g.graph.get("source", "Knowledge graph")))
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return Path(out_path)


# --------------------------------------------------------------------------
# HTML template (placeholders __X__ avoid clashing with JS/CSS braces)
# --------------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KG: __TITLE__</title>
<style>
:root{--bg:#fff;--fg:#111827;--mut:#6b7280;--line:#e5e7eb;--panel:#f9fafb}
@media(prefers-color-scheme:dark){:root{--bg:#111827;--fg:#f3f4f6;--mut:#9ca3af;--line:#374151;--panel:#1f2937}}
*{box-sizing:border-box}body{margin:0;font:13px system-ui,sans-serif;background:var(--bg);color:var(--fg);display:flex;height:100vh}
#side{width:320px;min-width:260px;overflow:auto;padding:12px;border-right:1px solid var(--line);background:var(--panel)}
#net{flex:1;min-width:0}h1{font-size:14px;margin:0 0 4px;word-break:break-all}h2{font-size:12px;margin:14px 0 6px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
input[type=search]{width:100%;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
label.row{display:flex;align-items:center;gap:6px;margin:3px 0;cursor:pointer}.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.cnt{margin-left:auto;color:var(--mut)}#details table{border-collapse:collapse;width:100%;margin:4px 0}
#details td,#details th{border:1px solid var(--line);padding:2px 5px;text-align:left;vertical-align:top;word-break:break-word}
#details .k{color:var(--mut);white-space:nowrap}.mut{color:var(--mut)}button{padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
@media(max-width:700px){body{flex-direction:column}#side{width:100%;max-height:45vh;border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div id="side">
  <h1>__TITLE__</h1><div class="mut" id="stats"></div>
  <h2>Search</h2><input type="search" id="q" placeholder="node label or text…">
  <h2>Node types</h2><div id="types"></div>
  <h2>Edges</h2><div id="groups"></div>
  <h2>Layout</h2><button id="fit">Fit view</button> <button id="phys">Toggle physics</button>
  <h2>Details</h2><div id="details" class="mut">Click a node or edge.</div>
</div>
<div id="net"></div>
__VIS__
<script>
const DATA = __DATA__, STATS = __STATS__;
const GROUPS = [["CONTAINS","Hierarchy (contains)",true],["NEXT","Reading order",false],
                ["MENTIONS","Element → entity mentions",true],["RELATION","Extracted relations",true]];
const hiddenTypes = new Set(), shownGroups = new Set(GROUPS.filter(g=>g[2]).map(g=>g[0]));
let query = "";
const el = (tag, props={}, ...kids) => { const e=document.createElement(tag); Object.assign(e,props); kids.forEach(k=>e.append(k)); return e; };

document.getElementById("stats").textContent = STATS.nodes + " nodes · " + STATS.edges + " edges";

const nodes = new vis.DataSet(DATA.nodes.map(n => ({...n, font:{size:12,color:"#374151"}})));
const edges = new vis.DataSet(DATA.edges.map(e => ({...e, arrows:"to", font:{size:10,align:"middle"},
  color:{color: e.group==="RELATION" ? "#2563eb" : "#9ca3af", opacity: e.group==="RELATION"?1:.5},
  dashes: e.group==="NEXT"||e.group==="MENTIONS", width: e.group==="RELATION"?2:1})));
const net = new vis.Network(document.getElementById("net"), {nodes, edges}, {
  physics:{solver:"forceAtlas2Based",forceAtlas2Based:{gravitationalConstant:-60,springLength:110},stabilization:{iterations:250}},
  interaction:{hover:true,tooltipDelay:200}, nodes:{shape:"dot",borderWidth:1}, edges:{smooth:{type:"continuous"}}});
net.once("stabilizationIterationsDone", () => net.setOptions({physics:false}));

function refresh(){
  const q = query.trim().toLowerCase(), visible = new Set(), hits = [];
  nodes.update(DATA.nodes.map(n => {
    const hide = hiddenTypes.has(n.type);
    if(!hide) visible.add(n.id);
    const hit = q && (n.label.toLowerCase().includes(q) || Object.values(n.attrs).some(v=>v.toLowerCase().includes(q)));
    if(hit && !hide) hits.push(n.id);
    return {id:n.id, hidden:hide, borderWidth: hit?4:1, color:{background:n.color, border: hit?"#ef4444":n.color}};
  }));
  edges.update(DATA.edges.map(e => ({id:e.id, hidden: !(shownGroups.has(e.group) && visible.has(e.from) && visible.has(e.to))})));
  if(hits.length) net.selectNodes(hits), net.fit({nodes:hits,animation:true});
}
function mkChecks(box, items, isOn, toggle){
  items.forEach(([key,label,color,count]) => {
    const cb = el("input",{type:"checkbox",checked:isOn(key)}); cb.onchange = () => { toggle(key,cb.checked); refresh(); };
    const row = el("label",{className:"row"}, cb);
    if(color) row.append(el("span",{className:"dot"})), row.lastChild.style.background = color;
    row.append(label); if(count!==undefined) row.append(el("span",{className:"cnt",textContent:count}));
    box.append(row);
  });
}
mkChecks(document.getElementById("types"),
  Object.entries(STATS.node_types).map(([t,c])=>[t,t,DATA.colors[t]||"#6b7280",c]),
  t=>true, (t,on)=> on?hiddenTypes.delete(t):hiddenTypes.add(t));
const gc = {}; DATA.edges.forEach(e=>gc[e.group]=(gc[e.group]||0)+1);
mkChecks(document.getElementById("groups"), GROUPS.map(g=>[g[0],g[1],null,gc[g[0]]||0]),
  k=>shownGroups.has(k), (k,on)=> on?shownGroups.add(k):shownGroups.delete(k));
document.getElementById("q").oninput = ev => { query = ev.target.value; refresh(); };
document.getElementById("fit").onclick = () => net.fit({animation:true});
let phys = false; document.getElementById("phys").onclick = () => net.setOptions({physics: (phys=!phys)});

function show(title, attrs){
  const box = document.getElementById("details"); box.className=""; box.replaceChildren(el("b",{textContent:title}));
  const t = el("table");
  Object.entries(attrs).forEach(([k,v]) => {
    if(k==="rows"){ try{ const rows=JSON.parse(v), tb=el("table");
        rows.forEach((r,i)=>tb.append(el("tr",{},...r.map(c=>el(i?"td":"th",{textContent:c})))));
        t.append(el("tr",{},el("td",{className:"k",textContent:k}),el("td",{},tb))); return; }catch(e){} }
    t.append(el("tr",{},el("td",{className:"k",textContent:k}),el("td",{textContent:v})));
  });
  box.append(t);
}
net.on("click", p => {
  if(p.nodes.length){ const n = nodes.get(p.nodes[0]), d = DATA.nodes.find(x=>x.id===n.id); show(d.attrs.label||d.id, {id:d.id, ...d.attrs}); }
  else if(p.edges.length){ const d = DATA.edges.find(x=>x.id===p.edges[0]); show(d.relation, {from:d.from, to:d.to, ...d.attrs}); }
});
refresh();
</script></body></html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Visualise a knowledge graph.")
    ap.add_argument("input", help="saved .json/.graphml KG, or a PDF/HTML path or URL")
    ap.add_argument("-o", "--output", default="kg.html", help="HTML output (default kg.html)")
    ap.add_argument("--png", metavar="FILE", help="also write a static PNG")
    ap.add_argument("--png-structure", action="store_true",
                    help="include structural nodes/edges in the PNG")
    ap.add_argument("--kg-dir", help="saved-KG directory (default $KG_OUTPUT_DIR or ./kg_store)")
    ap.add_argument("--open", action="store_true", help="open the HTML in a browser")
    ap.add_argument("--assets-dir", metavar="DIR",
                    help="export the document's figures (PNG) and tables (CSV) to DIR/<hash>/")
    ap.add_argument("--fetch", choices=["browser", "http", "auto"],
                    help="how to download URLs: headless browser (default), plain http, or auto")
    ap.add_argument("--pdf-engine", choices=["auto", "layout", "basic"],
                    help="PDF parser: layout (pymupdf-layout), basic, or auto (default)")
    ap.add_argument("--ocr", choices=["auto", "off", "force"], help="PDF OCR: auto = only pages without a text layer")
    ap.add_argument("--ocr-engine", choices=["auto", "tesseract", "rapidocr"])
    ap.add_argument("--ocr-lang")
    a = ap.parse_args(argv)
    if a.ocr or a.ocr_engine or a.ocr_lang:
        import kg_ingest as _ki
        _ki.set_ocr(a.ocr, a.ocr_engine, a.ocr_lang)
    if a.fetch or a.pdf_engine:
        import kg_ingest
        if a.fetch:
            kg_ingest.set_fetch_mode(a.fetch)
        if a.pdf_engine:
            kg_ingest.set_pdf_engine(a.pdf_engine)

    g = load_graph(a.input, a.kg_dir, a.assets_dir)
    out = render_html(g, a.output)
    print(f"wrote {out}  ({g.number_of_nodes()} nodes, {g.number_of_edges()} edges)")
    if a.png:
        print("wrote", render_png(g, a.png, include_structure=a.png_structure))
    if a.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
