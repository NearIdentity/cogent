"""Identifier extraction (X-02, X-03, X-04): lists, plural prefixes, ASME/ANS RA-S forms, and no 'None' in labels."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_ingest  # noqa: E402
import kg_tools as T  # noqa: E402


def labels(text):
    return [(i.label, i.edition) for i in T.parse_identities(text)]


def test_plural_and_list_forms_give_one_identifier_per_member():                        # X-02
    assert labels("endorsed in RGs 1.200 and 1.247 of the NRC") == [("RG 1.200", None), ("RG 1.247", None)]
    assert labels("Regulatory Guides 1.200, 1.247, and 1.250") == [("RG 1.200", None), ("RG 1.247", None), ("RG 1.250", None)]
    assert labels("NEIs 18-04 and 17-01") == [("NEI 18-04", None), ("NEI 17-01", None)]
    assert labels("10 CFR 50.34, 50.36, and 50.36a") == [("CFR 50.34", None), ("CFR 50.36", None), ("CFR 50.36a", None)]
    assert labels("10 CFR Parts 50 and 52") == [("CFR 50", None), ("CFR 52", None)]


def test_lists_do_not_run_on_into_ordinary_numbers():
    assert labels("see RG 1.200 and 10 more items") == [("RG 1.200", None)]
    assert labels("10 CFR 50.69 and 2 other items") == [("CFR 50.69", None)]
    assert labels("RG 1.233 Rev. 0") == [("RG 1.233", "0")]                              # singular forms unchanged


@pytest.mark.parametrize("text,edition", [("ASME/ANS RA-S-1.4", None), ("ANS-Ra-S-1.4-2013", "2013"), ("ASME/ANS RA-S-1.4-2021 requires", "2021"),
                                          ("RA-S 1.4", None)])
def test_ans_standard_is_an_identifier_with_or_without_edition(text, edition):        # X-03
    assert labels(text) == [("RA-S 1.4", edition)]


def test_identifier_labels_never_contain_none():                                       # X-04
    for fam in T.FAMILIES.values():
        assert "None" not in fam.fmt("1.4", None)
    assert T.FAMILIES["RA-S"].fmt("1.4", None) == "RA-S-1.4" and T.FAMILIES["RA-S"].fmt("1.4", "2013") == "RA-S-1.4-2013"


def test_spans_cover_lists_for_entity_masking():
    t = "endorsed in RGs 1.200 and 1.247 of the NRC"
    (a, b), = T.identifier_spans(t)
    assert t[a:b] == "RGs 1.200 and 1.247"


def test_graph_has_identifiers_not_entities_for_references(tmp_path):
    src = tmp_path / "d.html"
    src.write_text("<html><body><h1>Rule</h1><p>The staff endorsed this in RGs 1.200 and 1.247. See also ASME/ANS RA-S-1.4 for the method "
                   "and 10 CFR 50.34, 50.36 for the content.</p></body></html>")
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "a"))
    T.add_identifier_citations(g)                                                      # as the agent does after ingest
    T.add_cfr_crossrefs(g)
    idents = {a["label"] for _, a in g.nodes(data=True) if a.get("type") == "Identifier"}
    assert {"RG 1.200", "RG 1.247", "RA-S-1.4", "10 CFR 50.34", "10 CFR 50.36"} <= idents
    assert not any("None" in str(a.get("label")) for _, a in g.nodes(data=True))
    junk = [a["label"] for _, a in g.nodes(data=True) if a.get("type") in ("Entity", "Value") and ("1.200" in a["label"] or "RA-S" in a["label"] or "CFR" in a["label"])]
    assert junk == []
    cites = {v for _, v, d in g.edges(data=True) if d.get("relation") == "CITES"}
    assert {"ident:RG 1.200", "ident:RG 1.247", "ident:RA-S 1.4"} <= cites


def test_combined_graph_gets_the_entry_level_edges_from_a_list(tmp_path):
    """X-02 acceptance: a paragraph citing 'RGs 1.200 and 1.247' makes the entry cite both guides' entries."""
    import json

    import networkx as nx

    import kg_combine
    src = tmp_path / "d.html"
    src.write_text("<html><body><h1>Rule</h1><p>These methods are endorsed in RGs 1.200 and 1.247.</p></body></html>")
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "a"))
    T.add_identifier_citations(g)
    home = tmp_path / "home"
    for key, graph in (("standard:6", g), ("standard:26", nx.MultiDiGraph()), ("standard:27", nx.MultiDiGraph())):
        d = home / "staging" / "standards" / key.split(":")[1]
        d.mkdir(parents=True)
        (d / "kg.json").write_text(json.dumps(nx.node_link_data(graph, edges="edges")))
        (d / "meta.json").write_text(json.dumps({"signature": "x"}))
    mk = lambda key, name, ids: {"key": key, "kind": "standard", "name": name, "paywalled": False, "complies_with": [], "overlaps": [],
                                 "identities": ids, "version_status": "current", "latest": [], "on_board": []}
    cg, dep, _ = kg_combine.build(home, [mk("standard:6", "Part 53", []), mk("standard:26", "RG 1.200", ["RG 1.200"]), mk("standard:27", "RG 1.247", ["RG 1.247"])])
    assert {c["entry"] for c in dep["standard:6"]["cites"]} == {"standard:26", "standard:27"}
    assert cg.has_edge("entry:standard:6", "entry:standard:26")
