"""Combined graph: identifier-to-entry resolution (X-05) and 10 CFR 'part NNN' references (X-06)."""
import json
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_combine  # noqa: E402
import kg_ingest  # noqa: E402
import kg_tools as T  # noqa: E402


def entry(key, name, idents, kind="standard"):
    return {"key": key, "kind": kind, "name": name, "paywalled": False, "complies_with": [], "overlaps": [], "identities": idents,
            "version_status": "current", "latest": [], "on_board": []}


def stage(home, key, g, docs=()):
    d = home / "staging" / f"{key.split(':')[0]}s" / key.split(":")[1]
    d.mkdir(parents=True)
    (d / "kg.json").write_text(json.dumps(nx.node_link_data(g, edges="edges")))
    (d / "meta.json").write_text(json.dumps({"signature": key, "documents": [{"url": f"u/{x}", "docid": x} for x in docs]}))


def citing(*labels):
    g = nx.MultiDiGraph()
    g.add_node("p1", type="Paragraph", label="text", text="text")
    for lab in labels:
        g.add_node(f"ident:{lab}", type="Identifier", label=lab)
        g.add_edge("p1", f"ident:{lab}", key="CITES", relation="CITES")
    return g


def cites(dep, key):
    return {c["entry"]: c for c in dep[key]["cites"]}


def test_an_entry_does_not_cite_itself_through_a_report_on_the_same_document(tmp_path):
    """standard:22 IS NEI 18-04 and report:1's on-board document is the same NEI 18-04: standard:22 mentioning its own number cites nothing."""
    home = tmp_path
    stage(home, "standard:22", citing("NEI 18-04"), docs=["d22"])
    stage(home, "report:1", nx.MultiDiGraph(), docs=["d22"])
    _, dep, _ = kg_combine.build(home, [entry("standard:22", "NEI 18-04", ["NEI 18-04"]), entry("report:1", "Report on NEI 18-04", ["NEI 18-04"], "report")])
    assert dep["standard:22"]["cites"] == [] and dep["report:1"]["cited_by"] == []


def test_a_label_resolves_to_one_most_specific_standard(tmp_path):
    home = tmp_path
    stage(home, "standard:6", citing("CFR 50", "CFR 50.69"), docs=["d6"])
    for k in ("standard:1", "standard:28", "standard:3", "report:4"):
        stage(home, k, nx.MultiDiGraph(), docs=["d" + k])
    ents = [entry("standard:6", "Part 53", ["CFR 53"]), entry("standard:1", "10 CFR Part 50", ["CFR 50"]),
            entry("standard:28", "Appendix B and RG 1.28", ["CFR 50", "RG 1.28"]), entry("standard:3", "10 CFR 50.69", ["CFR 50.69"]),
            entry("report:4", "50.69 submittal", ["CFR 50.69"], "report")]
    g, dep, _ = kg_combine.build(home, ents)
    c = cites(dep, "standard:6")
    assert set(c) == {"standard:1", "standard:3"}                                   # not standard:28, not report:4
    e = next(d for _, v, d in g.edges("entry:standard:6", data=True) if v == "entry:standard:1")
    assert e["also_matched"] == ["standard:28"]                                      # the dropped alternative stays visible


def test_part_of_edge_from_a_section_to_its_part(tmp_path):
    home = tmp_path
    for k in ("standard:1", "standard:3"):
        stage(home, k, nx.MultiDiGraph(), docs=["d" + k])
    g, dep, _ = kg_combine.build(home, [entry("standard:1", "10 CFR Part 50", ["CFR 50"]), entry("standard:3", "10 CFR 50.69", ["CFR 50.69"])])
    assert dep["standard:3"]["part_of"] == ["standard:1"] and g.has_edge("entry:standard:3", "entry:standard:1", key="part_of")
    assert dep["standard:1"]["part_of"] == []


def test_part_forms_in_cfr_text_become_identifiers():
    assert T.part_references("as set out in appendix A to part 100 of this chapter") == ["100"]
    assert T.part_references("parts 50 and 52, and part 54") == ["50", "52", "54"]
    assert T.part_references("Subpart B of part 53; part 2.5 is not one; page 200") == ["53"]


def test_part_100_citation_reaches_the_entry_level(tmp_path):
    html = ("<html><body><h1>§ 50.69 Categorization</h1><p>Siting follows appendix A to part 100 of this chapter.</p>"
            "<h1>§ 50.70 Other</h1><p>Nothing here.</p></body></html>")
    src = tmp_path / "d.html"
    src.write_text(html)
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "a"))
    T.add_identifier_citations(g)
    assert "ident:CFR 100" in g and T.is_cfr_graph(g)
    home = tmp_path / "home"
    stage(home, "standard:3", g, docs=["d3"])
    stage(home, "standard:5", nx.MultiDiGraph(), docs=["d5"])
    cg, dep, _ = kg_combine.build(home, [entry("standard:3", "10 CFR 50.69", ["CFR 50.69"]), entry("standard:5", "10 CFR Part 100", ["CFR 100"])])
    assert "standard:5" in cites(dep, "standard:3") and cg.has_edge("entry:standard:3", "entry:standard:5", key="cites")


def test_bare_part_numbers_in_other_documents_are_ignored(tmp_path):
    src = tmp_path / "d.html"
    src.write_text("<html><body><h1>Guide</h1><p>See part 3 of this guide and part 12 of the form.</p></body></html>")
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "a"))
    T.add_identifier_citations(g)
    assert not T.is_cfr_graph(g) and not [n for n in g if str(n).startswith("ident:CFR")]
