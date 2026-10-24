"""Phase 2 API behaviour of the graph store, on a hand-made agent home (no parser involved)."""
import json
import sys
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_mcp  # noqa: E402
import kg_store  # noqa: E402

LONG = "§ 53.220 Safety criteria for licensing-basis events other than design-basis accidents and the many other conditions that the applicant shall address"


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    t = tmp_path_factory.mktemp("api")
    home = t / "home"
    (t / "s.json").write_text(json.dumps({"standards": [{"id": 1, "document": "Std", "links": [{"text": "x", "url": "https://example.org/a.pdf"}]}]}))
    (t / "r.json").write_text(json.dumps({"reports": []}))
    g = nx.MultiDiGraph()
    g.add_node("d:h1", type="Heading", label=LONG[:80], text=LONG, level=1, page=1, doc="d")
    g.add_node("d:h2", type="Heading", label="2 Applicability", text="2 Applicability", level=1, page=2, doc="d")
    g.add_node("d:p1", type="Paragraph", label="PDSA applies", text="The PDSA cycle is described in DOE-STD-1020.", page=2)
    g.add_node("d:p2", type="Paragraph", label="NSDA", text="NSDA is another analysis.", page=3)
    g.add_node("d:tbl1", type="Table", label="Table 1", page=3, rows=json.dumps([["a", "b"]] + [[str(i), str(i * 2)] for i in range(450)]))
    e = home / "staging" / "standards" / "1"
    e.mkdir(parents=True)
    (e / "kg.json").write_text(json.dumps(nx.node_link_data(g, edges="edges")))
    (e / "meta.json").write_text(json.dumps({"built": "2026-10-01T00:00:00Z", "documents": [{"url": "https://example.org/a.pdf", "docid": "aaaa1111aaaa1111"}]}))
    c = nx.MultiDiGraph()
    c.add_node("entry:standard:1", type="Entry", label="Std")
    c.add_node("entry:standard:3", type="Entry", label="Other")
    c.add_edge("entry:standard:1", "entry:standard:3", relation="cites")
    (home / "staging" / "combined").mkdir(parents=True)
    (home / "staging" / "combined" / "kg.json").write_text(json.dumps(nx.node_link_data(c, edges="edges")))
    d = home / "staging" / "documents" / "aaaa1111aaaa1111"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"url": "https://example.org/a.pdf", "last_error": "HTTP 403 Forbidden", "consecutive_errors": 14,
                                              "last_checked": "2026-10-20T00:00:00Z"}))
    (home / "logs").mkdir()
    (home / "logs" / "runs.jsonl").write_text(json.dumps({"finished": "2026-10-20T01:00:00Z", "summary": {"errors": 2, "error_messages": ["document x: 403", "entry y: boom"]}}) + "\n")
    return kg_store.Store(home, t / "s.json", t / "r.json")


def test_table_is_paged(store):
    first = store.table("standard:1", "d:tbl1")
    assert len(first["rows"]) == 200 and first["total_rows"] == 451 and first["truncated"] and first["next_offset"] == 200 and "offset=200" in first["note"]
    last = store.table("standard:1", "d:tbl1", offset=400, limit=200)
    assert len(last["rows"]) == 51 and not last["truncated"] and last["next_offset"] is None and last["rows"][-1] == ["449", "898"]
    assert len(store.table("standard:1", "d:tbl1", limit=5000)["rows"]) == 451                        # the limit is capped, not refused


def test_node_ids_accept_both_spellings_in_the_combined_graph(store):
    assert store.node("combined", "standard:3")["id"] == "entry:standard:3"
    assert store.node("combined", "entry:standard:3")["id"] == "entry:standard:3"
    assert store.subgraph("combined", "standard:1")["center"] == "entry:standard:1"
    nb = store.neighbors("combined", "standard:1")
    assert [x["node"]["id"] if "node" in x else x.get("id") for x in nb["neighbors"]] == ["entry:standard:3"]
    with pytest.raises(kg_store.NotFound, match="did you mean 'entry:standard:3'"):
        store.node("combined", "entry:standard:33")


def test_search_hyphens_modes_and_empty_result_explanation(store):
    assert [n["id"] for n in store.search("standard:1", "DOE-STD")] == ["d:p1"]
    assert [n["id"] for n in store.search("standard:1", "DOE STD 1020")] == ["d:p1"]                   # hyphen = space on both sides
    rep: dict = {}
    assert store.search("standard:1", "PDSA NSDA", report=rep) == [] and rep["term_counts"] == {"pdsa": 1, "nsda": 1}
    assert sorted(n["id"] for n in store.search("standard:1", "PDSA NSDA", match="any")) == ["d:p1", "d:p2"]
    with pytest.raises(kg_store.NotFound):
        store.search("standard:1", "x", match="some")


def test_outline_titles_are_not_cut_at_80_characters(store):
    o = {x["id"]: x for x in store.outline("standard:1")}
    assert o["d:h1"]["title"] == LONG and "truncated" not in o["d:h1"] and o["d:h2"]["title"] == "2 Applicability"


def test_overview_itemises_run_and_document_errors(store):
    ov = store.overview()
    assert ov["run_errors"] == ["document x: 403", "entry y: boom"]
    assert ov["document_errors"] == [{"document_id": "aaaa1111aaaa1111", "entries": ["standard:1"], "url": "https://example.org/a.pdf",
                                      "error": "HTTP 403 Forbidden", "consecutive_errors": 14, "last_checked": "2026-10-20T00:00:00Z"}]


DEF = "Safety-related means structures, systems and components that are relied upon to remain functional during and following design basis events."


def test_search_ranks_regulation_text_first_and_merges_duplicate_paragraphs(tmp_path):
    """D-02: the Federal Register rule and the eCFR text of a part both contain the definitions; show each once, codified text first."""
    home = tmp_path / "home"
    (tmp_path / "s.json").write_text(json.dumps({"standards": [{"id": 6, "document": "Part 53", "links": [{"text": "x", "url": "https://www.ecfr.gov/p53"}]}]}))
    (tmp_path / "r.json").write_text(json.dumps({"reports": []}))
    fr, ec = "6526ad48" + "a" * 8, "43929258" + "b" * 8
    g = nx.MultiDiGraph()
    g.add_node(f"{fr[:8]}:p0225", type="Paragraph", label=DEF[:80], text=DEF, page=1)
    g.add_node(f"{ec[:8]}:p0010", type="Paragraph", label=DEF[:80], text=DEF, page=2)
    g.add_node(f"{ec[:8]}:p0011", type="Paragraph", label="Other", text="Safety-related is also used in section 53.220 for another purpose entirely, quite different.")
    e = home / "staging" / "standards" / "6"
    e.mkdir(parents=True)
    (e / "kg.json").write_text(json.dumps(nx.node_link_data(g, edges="edges")))
    (e / "meta.json").write_text(json.dumps({"documents": [{"url": "https://www.federalregister.gov/d/1", "docid": fr}, {"url": "https://www.ecfr.gov/p53", "docid": ec}]}))
    for docid, url in ((fr, "https://www.federalregister.gov/d/1"), (ec, "https://www.ecfr.gov/p53")):
        d = home / "staging" / "documents" / docid
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"url": url}))
    st = kg_store.Store(home, tmp_path / "s.json", tmp_path / "r.json")
    rep: dict = {}
    res = st.search("standard:6", "safety-related", report=rep)
    assert [r["id"] for r in res][0] == f"{ec[:8]}:p0010" and res[0]["source_role"] == "regulatory_text"
    assert res[0]["also_in"] == [f"{fr[:8]}:p0225"] and rep["duplicates_merged"] == 1
    assert len(res) == 2 and kg_store.doc_role("file:///x.pdf") == "local_copy" and kg_store.doc_role("https://www.nrc.gov/a.pdf") == "document"


def test_outline_warns_when_section_numbers_are_out_of_sequence(store, tmp_path):
    assert store.outline_warnings("standard:1") == []                         # the fixture's headings are not a numbering break
    g = nx.MultiDiGraph()
    for i, t in enumerate(["1 PURPOSE", "3 RESPONSIBILITY", "2 APPLICABILITY", "4 SCOPE"]):
        g.add_node(f"d:h{i}", type="Heading", label=t, text=t, level=1, page=1, doc="d")
    e = store.home / "staging" / "standards" / "2"
    e.mkdir(parents=True)
    (e / "kg.json").write_text(json.dumps(nx.node_link_data(g, edges="edges")))
    w = store.outline_warnings("standard:2")
    assert len(w) == 1 and "APPLICABILITY" in w[0] and w[0].startswith("d: ")


def test_graph_cache_is_bounded_and_least_recently_used(tmp_path):
    """Scale-out: every worker holds its own graphs; the cache keeps at most max_cached_graphs of them (least recently used go first)."""
    home = tmp_path / "home"
    (tmp_path / "s.json").write_text(json.dumps({"standards": [{"id": i, "document": f"S{i}", "links": []} for i in (1, 2, 3)]}))
    (tmp_path / "r.json").write_text(json.dumps({"reports": []}))
    for i in (1, 2, 3):
        d = home / "staging" / "standards" / str(i)
        d.mkdir(parents=True)
        g = nx.MultiDiGraph()
        g.add_node(f"n{i}", type="Paragraph", label=f"node {i}", text=f"node {i}")
        (d / "kg.json").write_text(json.dumps(nx.node_link_data(g, edges="edges")))
    st = kg_store.Store(home, tmp_path / "s.json", tmp_path / "r.json", max_cached_graphs=2)
    st.graph("standard:1"), st.graph("standard:2")
    st.graph("standard:1")                                    # standard:1 is now the most recently used
    st.graph("standard:3")                                    # evicts standard:2
    assert len(st._cache) == 2 and st.cache_stats == {"hits": 1, "loads": 3, "evictions": 1}
    assert any(k.endswith("/1/kg.json") for k in st._cache) and not any(k.endswith("/2/kg.json") for k in st._cache)
    unbounded = kg_store.Store(home, tmp_path / "s.json", tmp_path / "r.json", max_cached_graphs=0)
    for i in (1, 2, 3):
        unbounded.graph(f"standard:{i}")
    assert len(unbounded._cache) == 3 and unbounded.cache_stats["evictions"] == 0
