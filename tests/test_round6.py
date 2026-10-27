"""Test-round 6 fixes (batch 1): lookup confidence (R6-01), extraction-recipe refresh (R6-04 / R6-14 / R6-15), error messages (R6-11 / 12 / 13)."""
import json
import sys
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_agent  # noqa: E402
import kg_models  # noqa: E402
import kg_store  # noqa: E402
import kg_tableservice as TS  # noqa: E402
from test_agent import env  # noqa: E402,F401  (fixture)


# ------------------------------------------------------------------ R6-01
def _row(caption, section="", sim=0.68, **values):
    return {"table_id": "d/t1", "row": 1, "values": values or {"a": "1"}, "similarity": sim, "table": {"caption": caption, "source": {"section": section, "document": "Doc"}}}


def test_code_like_terms_are_found_in_questions():
    assert TS.code_tokens("allowable stress for ASTM A572 steel") == ["A572"]
    assert TS.code_tokens("SA-516 grade 70 yield") == ["SA-516"]
    assert TS.code_tokens("values in Regulatory Guide RG 1.233") == ["1.233"]
    assert TS.code_tokens("damping for welded steel structures") == []
    assert TS.code_tokens("pressure 25 mm2 at 3 psi") == []                       # units are not identifiers
    assert TS.code_tokens("what does RA-S-1.4 say; COVID-19 is unrelated") == ["1.4"]                        # RA-S is a family, COVID-19 is not an identifier


def test_lookup_without_the_named_identifier_is_no_confident_match():
    pts = [_row("Table 1-PTS Screening Criteria", "§ 50.61a", **{"RT MAX": "538"})]
    r = TS.assess_lookup("allowable stress for ASTM A572 steel", pts)
    assert r["no_confident_match"] and r["match_quality"] == "none" and r["rows"] == [] and r["nearest_neighbours"] == pts
    assert "A572" in r["message"] and r["missing_terms"] == ["A572"]


def test_lookup_with_the_identifier_keeps_only_rows_that_contain_it():
    hit, miss = _row("Table 2 ASTM A-572 grades", **{"Grade": "50"}), _row("Table 9 other")
    r = TS.assess_lookup("allowable stress A572", [miss, hit])
    assert r["match_quality"] == "lexical" and r["rows"] == [hit] and r["nearest_neighbours"] == [miss] and "no_confident_match" not in r
    assert TS.assess_lookup("NQA-1 value", [_row("Table 3-1", std="NQA-1")])["match_quality"] == "lexical"          # a cell holds it


def test_similarity_floor_flags_weak_rows_only_when_configured():
    rows = [_row("Table 4-1", sim=0.60)]
    assert TS.assess_lookup("damping for welded steel", rows)["match_quality"] == "semantic"
    weak = TS.assess_lookup("damping for welded steel", rows, 0.7)
    assert weak["no_confident_match"] and weak["rows"] == [] and weak["nearest_neighbours"] == rows


# ------------------------------------------------------------------ R6-11 / 12 / 13
@pytest.fixture()
def store(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"standards": [{"id": i, "document": f"Std {i}"} for i in (1, 2, 30)]}))
    (tmp_path / "r.json").write_text(json.dumps({"reports": [{"id": 1, "report": "R1"}, {"id": 15, "report": "R15"}]}))
    home = tmp_path / "home"
    g = nx.MultiDiGraph()
    for n in ("ent:consistent", "ent:existing", "ent:seismic design category", "ent:sdc"):
        g.add_node(n, type="Entity", label=n[4:])
    d = home / "staging" / "standards" / "1"
    d.mkdir(parents=True)
    (d / "kg.json").write_text(json.dumps(nx.node_link_data(g, edges="edges")))
    c = nx.MultiDiGraph()
    c.add_node("entry:standard:1", type="Entry", label="Std 1")
    c.add_node("entry:standard:30", type="Entry", label="Std 30")
    (home / "staging" / "combined").mkdir(parents=True)
    (home / "staging" / "combined" / "kg.json").write_text(json.dumps(nx.node_link_data(c, edges="edges")))
    return kg_store.Store(home, tmp_path / "s.json", tmp_path / "r.json")


def test_unknown_entry_is_not_reported_as_unstaged(store):
    with pytest.raises(kg_store.NotFound) as e:
        store.node("standard:999", "x")
    assert "unknown entry 'standard:999'" in str(e.value) and "standard:1-30" in str(e.value) and "report:1-15" in str(e.value)
    with pytest.raises(kg_store.NotFound) as e:
        store.entry_metadata("standard:999")
    assert "unknown entry" in str(e.value) and "valid entries" in str(e.value)
    with pytest.raises(kg_store.NotFound) as e:
        store.node("standard:2", "x")                                  # a known entry that has no graph yet keeps the old wording
    assert "no knowledge graph staged" in str(e.value)


def test_node_suggestions_have_a_relevance_cutoff(store):
    with pytest.raises(kg_store.NotFound) as e:
        store.node("standard:1", "nonexistent")
    assert "consistent" not in str(e.value) and "existing" not in str(e.value) and "no close node id" in str(e.value)
    with pytest.raises(kg_store.NotFound) as e:
        store.node("standard:1", "ent:seismic design categroy")        # a typo does suggest the right node
    assert "'ent:seismic design category'" in str(e.value)
    with pytest.raises(kg_store.NotFound) as e:
        store.node("combined", "entry:standard:31")
    assert "'entry:standard:30'" in str(e.value) or "'entry:standard:1'" in str(e.value)
    with pytest.raises(kg_store.NotFound) as e:
        store.node("combined", "nonexistent")
    assert "no close node id" in str(e.value)


# ------------------------------------------------------------------ R6-04 / 14: extraction recipe
def _state_files(home):
    return sorted(home.glob("staging/documents/*/state.json"))


def test_documents_are_stamped_and_rebuilt_once_when_the_recipe_moves_on(env):
    site, s, sp, rp = env
    kg_agent.run_once(s)
    stamps = [json.loads(p.read_text())["extraction_recipe"] for p in _state_files(s.home)]
    assert stamps and set(stamps) == {kg_models.EXTRACTION_RECIPE}
    assert json.loads(next(s.home.glob("staging/documents/*/kg.json")).read_text())["graph"]["extraction_recipe"] == kg_models.EXTRACTION_RECIPE
    assert kg_agent.run_once(s)["summary"]["docs_unchanged"] == 3                      # same recipe: nothing is rebuilt

    for p in _state_files(s.home):                                                     # pretend the graphs came from an older extraction
        d = json.loads(p.read_text())
        d["extraction_recipe"] = "1"
        p.write_text(json.dumps(d))
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "recipe_refresh": False}))
    assert st["summary"]["docs_unchanged"] == 3                                        # opt-out keeps the old graphs
    site.hits.clear()
    st = kg_agent.run_once(s)
    assert st["summary"]["docs_refreshed"] == 3 and st["summary"]["docs_unchanged"] == 0 and not st["summary"]["forced"], st["report"]
    assert "new extraction recipe" in st["report"] and "extraction recipe 1 ->" in json.dumps(st["doc_actions"] if "doc_actions" in st else st["report"])
    assert any(h.endswith("rg1233_rev0.pdf") for h in site.hits)                       # re-downloaded, not answered with 304
    assert {json.loads(p.read_text())["extraction_recipe"] for p in _state_files(s.home)} == {kg_models.EXTRACTION_RECIPE}
    assert kg_agent.run_once(s)["summary"]["docs_unchanged"] == 3                      # and only once
    assert json.loads(_state_files(s.home)[0].read_text())["versions"] == 1            # a recipe refresh of identical text is not a new document version


def test_store_reports_entries_behind_the_recipe(env):
    site, s, sp, rp = env
    kg_agent.run_once(s)
    store = kg_store.Store(s.home, sp, rp)
    ov = store.overview()
    assert ov["extraction_recipe"] == kg_models.EXTRACTION_RECIPE and ov["entries_behind_extraction_recipe"] == [] and "note" not in ov
    p = _state_files(s.home)[0]
    d = json.loads(p.read_text())
    d.pop("extraction_recipe")                                                         # a graph built before recipes were recorded
    p.write_text(json.dumps(d))
    ov = store.overview()
    assert ov["entries_behind_extraction_recipe"] and "older extraction recipe" in ov["note"]
    key = ov["entries_behind_extraction_recipe"][0]
    meta = store.entry_metadata(key)
    assert meta["graph"]["documents_behind_recipe"] and any(not x["extraction_recipe_current"] for x in meta["documents"])
    assert next(r for r in store.catalog() if r["entry"] == key)["extraction_recipe_behind"] is True


# ------------------------------------------------------------------ R6-03 / R6-05: joins
import kg_relmap as R  # noqa: E402
import kg_tables as K  # noqa: E402

ISOTOPES = [f"{sym}-{n}" for sym, n in [("Ac", 225), ("Am", 241), ("Cm", 244), ("Co", 60), ("Cs", 137), ("H", 3), ("I", 131), ("Ir", 192), ("Kr", 85), ("Mo", 99),
                                          ("Np", 237), ("Pu", 238), ("Pu", 239), ("Ra", 226), ("Sr", 90), ("Tc", 99), ("U", 233), ("U", 235), ("Xe", 133), ("Zr", 95)]]


def mk(tid, header, rows, caption=""):
    fr = K.frame([header] + rows)
    return R.T(tid, tid.split("/")[-1], K.profile(fr), fr.rows, K.Context(caption=caption or tid))


def test_value_overlap_finds_a_key_join_when_the_headers_say_nothing():
    """NE-STD-1027 Table A-6 -> Table 1-1: garbage headers (column_1 / rdilid), the first cell of A-6 names Table 1-1, 20 of 21 values shared."""
    t11 = mk("d/tbl0224", ["rdilid", "HC-2 Ci", "HC-3 Ci"], [[i, str(10 + k), str(k)] for k, i in enumerate(ISOTOPES)], "Table 1-1: Thresholds")
    a6 = mk("d/tbl0324", ["column_1", "column_2"], [["Isotope Name Used in Table 1-1", "Form"]] + [[i, "metal" if k % 2 else "oxide"] for k, i in enumerate(ISOTOPES)], "Table A-6")
    rels = [r for r in R.infer([t11, a6]) if r["kind"] == "join"]
    assert rels, "the key join was not inferred"
    r = rels[0]
    assert {r["left_table"], r["right_table"]} == {"d/tbl0224", "d/tbl0324"} and {r["left_header"], r["right_header"]} == {"rdilid", "column_1"}
    assert r["score"] >= 0.8 and any("names the other table" in e for e in r["evidence"]) and not any("near-identical" in e for e in r["evidence"])


def test_list_numbers_and_table_references_are_not_join_keys():
    a = mk("d/tbl1", ["c_1", "c_2"], [["2.", "alpha"], ["3.", "beta"], ["4.", "gamma"], ["5.", "delta"], ["6.", "eps"]])
    b = mk("d/tbl2", ["c_1", "c_2"], [["2.", "red"], ["3.", "green"], ["4.", "blue"], ["5.", "pink"], ["6.", "grey"]])
    assert [r for r in R.infer([a, b]) if r["kind"] == "join"] == []                         # identical placeholder names are not evidence
    refs = [f"table {i}: table {i + 1}:" for i in range(1, 7)]
    c = mk("d/tbl3", ["column_4", "x"], [[v, str(k)] for k, v in enumerate(refs)])
    d = mk("d/tbl4", ["column_4", "y"], [[v, str(k * 2)] for k, v in enumerate(refs)])
    assert [r for r in R.infer([c, d]) if r["kind"] == "join"] == []                         # nor are cross-references
    assert R.name_similarity("column_1", "column_1") == 0.0 and R.name_similarity("Seismic Category", "seismic categories") > 0.85


def test_columns_that_repeat_on_both_sides_are_not_join_keys():
    vocab = ["yes", "no", "n/a", "none", "maybe", "other"]
    a = mk("d/t1", ["col_a", "n"], [[v, str(k)] for k, v in enumerate(vocab * 2)])
    b = mk("d/t2", ["col_b", "m"], [[v, str(k)] for k, v in enumerate(vocab * 3)])
    assert [r for r in R.infer([a, b]) if r["kind"] == "join"] == []                         # neither column is unique: a shared vocabulary, not a key


# ------------------------------------------------------------------ R6-06: non-data tables
import kg_tablecheck as TC  # noqa: E402


def test_store_boxes_and_indexes_are_front_matter_but_real_tables_are_not():
    assert TC.front_matter([["Quantity", "Item"], ["1", "PDF download"], ["2", "Print + PDF"]], "") == "store"
    assert TC.front_matter([["Name", "Price", "Add to cart"], ["ASME NQA-1-2019", "$100", "Add to cart"]], "") == "store"
    assert TC.front_matter([["Quantity", "Item"], ["10", "Drums"]], "Table 3-2 Inventory") is None             # a captioned data table keeps being data
    idx = [["Interpretation", "Subject", "Section"]] + [[f"1-{i}", "welding", "2.1"] for i in range(5)]
    assert TC.front_matter(idx, "Subject index") == "index"
    assert TC.front_matter(idx, "", "NQA-1 > Numerical Index") == "index"
    assert TC.front_matter(idx, "Table 2 Welding parameters", "Part II") is None


def test_header_fragment_captions_are_replaced_by_the_section_heading():
    from kg_ingest import Element
    els = [Element(id="h1", kind="heading", text="§ 50.75 Reporting and recordkeeping for decommissioning planning", level=2),
           Element(id="t1", kind="table", text="| Millions", caption="| Millions", table=[["Reactor", "Amount"], ["PWR", "105"], ["BWR", "135"]]),
           Element(id="t2", kind="table", text="Table 4-1 Limits", caption="Table 4-1 Limits", table=[["Class", "Limit"], ["A", "1"], ["B", "2"]])]
    stats = TC.apply(els)
    assert els[1].caption == "§ 50.75 Reporting and recordkeeping for decommissioning planning" and els[1].meta["caption_fragment"] == "| Millions"
    assert els[2].caption == "Table 4-1 Limits" and stats["captions_replaced"] == 1
    assert TC.caption_fragment("| Millions") and not TC.caption_fragment("Table 5-1") and not TC.caption_fragment("Allowable stress values for bolting")


# ------------------------------------------------------------------ R6-02: scope of a link
from kg_ingest import Element, apply_scope  # noqa: E402


def _part50():
    return [Element(id="h1", kind="heading", text="PART 50 DOMESTIC LICENSING", level=1),
            Element(id="h2", kind="heading", text="§ 50.61a Alternate fracture toughness requirements", level=3),
            Element(id="t1", kind="table", text="Table 1-PTS", caption="Table 1-PTS", table=[["a", "b"], ["1", "2"], ["3", "4"]]),
            Element(id="h3", kind="heading", text="Appendix B to Part 50 - Quality Assurance Criteria", level=2),
            Element(id="p1", kind="paragraph", text="Introduction. Every applicant shall establish a QA program.", level=0),
            Element(id="h4", kind="heading", text="I. Organization", level=3),
            Element(id="p2", kind="paragraph", text="The applicant shall be responsible.", level=0),
            Element(id="h5", kind="heading", text="Appendix C to Part 50 - A Program", level=2),
            Element(id="p3", kind="paragraph", text="Another appendix.", level=0)]


def test_scope_keeps_only_the_appendix_subtree():
    kept, info = apply_scope(_part50(), "Appendix B to Part 50")
    assert [e.id for e in kept] == ["h3", "p1", "h4", "p2"] and info["matched"] and info["kept"] == 4 and info["dropped"] == 5
    assert not any(e.kind == "table" for e in kept)                                      # the § 50.61a table is no longer part of this document


def test_scope_end_pattern_and_unmatched_scope():
    flat = [Element(id=f"h{i}", kind="heading", text=t, level=2) for i, t in enumerate(["Appendix B to Part 50", "I. Organization", "II. QA Program", "Appendix C to Part 50"])]
    kept, _ = apply_scope(flat, "Appendix B to Part 50", scope_end="Appendix C")
    assert [e.text for e in kept] == ["Appendix B to Part 50", "I. Organization", "II. QA Program"]
    kept, info = apply_scope(_part50(), "Appendix Z")
    assert len(kept) == len(_part50()) and info["matched"] is False and info["dropped"] == 0   # a wrong pattern never empties a document


def test_scope_flows_through_the_agent_and_changing_it_rebuilds(env):
    site, s, sp, rp = env
    cfg = json.loads(sp.read_text())
    link = cfg["standards"][1]["links"][0]
    link["scope"] = "no heading says this"
    sp.write_text(json.dumps(cfg))
    st = kg_agent.run_once(s)
    assert "matched no heading" in st["report"]
    docs = {json.loads(p.read_text())["url"]: json.loads(p.read_text()) for p in s.home.glob("staging/documents/*/state.json")}
    assert docs[link["url"]]["scope"].startswith("no heading says this")
    del link["scope"]
    sp.write_text(json.dumps(cfg))
    again = kg_agent.run_once(s)["summary"]
    assert again["docs_refreshed"] == 1 and again["docs_unchanged"] == 2                  # only the document whose scope changed is rebuilt


# ------------------------------------------------------------------ R6-07 / 08 / 09 / 10: identifiers, entities
import kg_ingest as KI  # noqa: E402
import kg_tools as KT  # noqa: E402


def test_ans_references_are_identifiers_not_entities():
    ids = KT.parse_identities("as defined in [ANSI/ANS-2.26-2004] and ANSI/ANS-2.27-2020, also ANS 58.14 and ASME/ANS RA-S-1.4")
    got = {(i.family, i.key, i.edition) for i in ids}
    assert ("ANS", "2.26", "2004") in got and ("ANS", "2.27", "2020") in got and ("ANS", "58.14", None) in got and ("RA-S", "1.4", None) in got
    assert next(i for i in ids if i.key == "2.26").label == "ANS 2.26"
    text = "The category is defined in [ANSI/ANS-2.26-2004] for Seismic Design."
    assert [text[a:b] for a, b in KT.identifier_spans(text)] == ["ANSI/ANS-2.26-2004"]
    names = [n for n, _, _ in KI.RuleBasedExtractor().entities(text)]
    assert "ANSI" not in names and "ANS-2" not in names and "Seismic Design" in names


def test_singular_and_plural_forms_are_one_entity_with_aliases():
    r = KI.EntityResolver()
    assert r.resolve("SDC") == r.resolve("SDCs") == r.resolve("sdcs") == "sdc"
    assert r.resolve("Safety Systems") == r.resolve("Safety System") == "safety system"
    assert r.display("sdc") == "SDCs" and sorted(r.aliases("sdc")) == ["SDC", "sdcs"]
    for word in ("analysis", "status", "process", "series", "physics", "gas", "bias"):
        assert KI.singular(word) == word
    assert KI.singular("categories") == "category" and KI.singular("boxes") == "box" and KI.singular("classes") == "class"


def _g(*nodes):
    g = nx.MultiDiGraph()
    for n, d in nodes:
        g.add_node(n, **d)
    return g


def test_acronym_expansion_is_one_abbreviation_edge_not_a_circle():
    g = _g(("ent:seismic design category", {"type": "Entity", "label": "Seismic Design Category"}), ("ent:sdc", {"type": "Entity", "label": "SDC"}),
           ("ent:other", {"type": "Entity", "label": "Other Thing"}))
    g.add_edge("ent:seismic design category", "ent:sdc", key="related_to|p1", relation="related_to", source="p1")
    g.add_edge("ent:sdc", "ent:seismic design category", key="has_value|t1", relation="has_value", source="t1")
    g.add_edge("ent:other", "ent:sdc", key="related_to|p2", relation="related_to", source="p2")
    assert KI.link_abbreviations(g) == 1
    rels = sorted((u, v, d["relation"]) for u, v, d in g.edges(data=True))
    assert rels == [("ent:other", "ent:sdc", "related_to"), ("ent:sdc", "ent:seismic design category", "abbreviation_of")]
    assert KI.is_abbreviation("SDCs", "Seismic Design Categories") and not KI.is_abbreviation("SDC", "Other Thing") and not KI.is_abbreviation("SDC", "Seismic")


def test_glossary_rows_become_abbreviation_edges():
    g = _g(("t1", {"type": "Table", "front_matter": "glossary", "rows": json.dumps([["Acronym", "Meaning"], ["SSC", "Structures, Systems and Components"], ["FOO", "Something Else Entirely"]])}))
    assert KI.link_abbreviations(g) == 1
    assert [(u, d["relation"]) for u, v, d in g.edges(data=True)] == [("ent:ssc", "abbreviation_of")]


def test_entities_are_linked_to_the_tables_that_use_them():
    rows = [["Seismic Design Category", "Coefficient"], ["SDC-1", "0.5"], ["SDC-2", "0.7"]]
    g = _g(("t3", {"type": "Table", "label": "Table 3-1 Coefficients", "text": "Table 3-1 Coefficients", "rows": json.dumps(rows)}),
           ("t9", {"type": "Table", "label": "Contents", "rows": json.dumps(rows), "front_matter": "toc"}),
           ("ent:seismic design category", {"type": "Entity", "label": "Seismic Design Category"}), ("ent:sdc1", {"type": "Entity", "label": "SDC-1"}),
           ("ent:coefficients", {"type": "Entity", "label": "coefficients"}), ("ent:unrelated", {"type": "Entity", "label": "Unrelated Phrase"}))
    n = KI.link_table_entities(g)
    got = {(u, v): d["via"] for u, v, d in g.edges(data=True) if d["relation"] == "MENTIONS"}
    assert got[("t3", "ent:seismic design category")] == "header" and got[("t3", "ent:sdc1")] == "key cell"
    assert ("t3", "ent:coefficients") not in got and ("t3", "ent:unrelated") not in got and not any(u == "t9" for u, _ in got)    # lower-case single words and front matter are skipped
    assert n == 2 and KI.link_table_entities(g) == 0                                                                           # idempotent


def test_pipeline_produces_merged_entities_abbreviation_edges_and_table_links(tmp_path):
    html = tmp_path / "doc.html"
    html.write_text("""<html><body><h1>Seismic Design</h1>
    <p>The Seismic Design Category (SDC) is chosen by the owner. Each SDC has limits. Facilities in SDCs 3 and 4 need review.</p>
    <p>See ANSI/ANS-2.26-2004 for the method.</p>
    <table><caption>Table 3-1 Coefficients by Seismic Design Category</caption>
    <tr><th>Seismic Design Category</th><th>Coefficient</th></tr><tr><td>SDC-1</td><td>0.5</td></tr><tr><td>SDC-2</td><td>0.7</td></tr><tr><td>SDC-3</td><td>0.9</td></tr></table>
    </body></html>""")
    g = KI.file_to_knowledge_graph(str(html))
    KT.add_identifier_citations(g)                                                            # what the agent does after ingest
    labels = {d.get("label") for n, d in g.nodes(data=True) if str(n).startswith("ent:")}
    assert not ({"SDC", "SDCs"} <= labels)                                                    # one node for both spellings
    assert any(n == "ident:ANS 2.26" for n in g.nodes)
    assert not any(l in ("ANSI", "ANS-2") for l in labels)
    assert any(d.get("relation") == "abbreviation_of" for _, _, d in g.edges(data=True))
    tbl = next(n for n, d in g.nodes(data=True) if d.get("type") == "Table")
    mentioned = {v for _, v, d in g.out_edges(tbl, data=True) if d.get("relation") == "MENTIONS"}
    assert "ent:seismic design category" in mentioned


# ------------------------------------------------------------------ PR 9 review follow-ups
TABLE_1_1 = {"Radionuclide": "Cs-137", "HC-2 Ci": "1.0E+03", "Hazard Category 3 Ci": "1.0E+01"}


def test_category_labels_rank_rows_but_do_not_empty_the_answer():
    """A1: 'HC-3 threshold quantity for Cs-137' used to be 'none' because the table says 'Hazard Category 3', not 'HC-3'."""
    row = _row("Table 1-1 Thresholds", "Hazard Category thresholds", **TABLE_1_1)
    r = TS.assess_lookup("HC-3 threshold quantity for Cs-137", [row])
    assert r["match_quality"] == "lexical" and r["rows"] == [row] and "no_confident_match" not in r       # HC-3 is found as 'Hazard Category 3'
    r = TS.assess_lookup("PDC-3 threshold quantity for Cs-137", [row])
    assert r["match_quality"] == "partial" and r["rows"] == [row] and r["missing_terms"] == ["PDC-3"] and "no_confident_match" not in r
    assert TS.assess_lookup("HC-3 threshold", [row])["match_quality"] == "semantic"                          # only a category label: nothing to gate on
    assert TS.split_tokens("HC-3 Cs-137 SA-516 SDC-2") == (["Cs-137", "SA-516"], ["HC-3", "SDC-2"])
    other = _row("Table 9", "x", **{"Radionuclide": "Sr-90"})
    ranked = TS.assess_lookup("HC-3 limit for Cs-137", [other, row])
    assert ranked["rows"] == [row] and ranked["nearest_neighbours"] == [other]


def test_neighbouring_cells_do_not_join_into_a_false_hit():
    """A4: matching per field; 'A572' must not be assembled from the end of one cell and the start of the next."""
    r = _row("Table 2", "Steel", **{"Grade": "A", "Number": "572"})
    assert TS.assess_lookup("stress for A572", [r])["match_quality"] == "none"


def test_similarity_floor_is_a_preference_and_strict_tightens_it():
    import kg_llmjoin as LJ
    assert LJ.PROFILES["strict"]["min_similarity"] > 0 and LJ.PROFILES["balanced"]["min_similarity"] == 0.0
    assert "min_similarity" in TS.FIELDS
    with pytest.raises(kg_store.NotFound):
        TS.Prefs(min_similarity=1.5).validate()


def test_an_ordinal_header_is_not_a_name_and_real_short_headers_are():
    """B1 / B2: header '1.' on both sides with values '2.', '3.' ... is not a join; 'C1' and 'f1' are real variables."""
    assert R.placeholder("1.") and R.placeholder("2)") and R.placeholder("column_4") and R.placeholder("c_1") and R.placeholder("")
    assert not R.placeholder("C1") and not R.placeholder("f1") and not R.placeholder("Isotope")
    a = mk("d/t1", ["1.", "x"], [["2.", "alpha"], ["3.", "beta"], ["4.", "gamma"], ["5.", "delta"], ["6.", "eps"]])
    b = mk("d/t2", ["1.", "y"], [["2.", "red"], ["3.", "green"], ["4.", "blue"], ["5.", "pink"], ["6.", "grey"]])
    assert [r for r in R.infer([a, b]) if r["kind"] == "join"] == []
    assert R.name_similarity("C1", "C1") == 1.0


def test_header_fragment_captions_and_real_short_captions():
    """C1: the Table 1-1 caption is a fragment, 'Damping Values' is not; the label hidden in the fragment is recovered."""
    frag = "Rdilid | TABLE 1 Hazard Ca | -1: THRESHOL tegory 2(A) | HC-2 (g)"
    assert TC.caption_fragment(frag) and TC.caption_fragment("| Millions") and TC.caption_fragment("Rdilid", ["Rdilid", "HC-2 Ci", "HC-3 Ci"])
    assert not TC.caption_fragment("Damping Values") and not TC.caption_fragment("Table 5-1 Limits") and not TC.caption_fragment("Allowable stress values for bolting")
    assert TC.recover_label(frag) == "Table 1-1" and TC.recover_label("TABLE 4-2 Limits | x") == "Table 4-2" and TC.recover_label("| Millions") == ""
    from kg_ingest import Element
    els = [Element(id="h1", kind="heading", text="Hazard category thresholds", level=2),
           Element(id="t1", kind="table", text=frag, caption=frag, table=[["Rdilid", "HC-2 Ci", "HC-3 Ci"], ["Cs-137", "1", "2"], ["Sr-90", "3", "4"]]),
           Element(id="t2", kind="table", text="Damping Values", caption="Damping Values", table=[["Type", "Damping"], ["Welded", "3"], ["Bolted", "5"]])]
    TC.apply(els)
    assert els[1].caption == "Table 1-1 (Hazard category thresholds)" and els[1].meta["recovered_label"] == "Table 1-1" and els[1].meta["caption_fragment"] == frag
    assert els[2].caption == "Damping Values"


def test_index_detection_needs_the_title_to_be_an_index():
    """C2: 'Index Properties of Soils' and 'General Index of Seismic Parameters' are data; 'Subject Index' is not."""
    rows = [["Interpretation", "Subject", "Section"]] + [[f"1-{i}", "welding", "2.1"] for i in range(5)]
    assert TC.front_matter(rows, "Subject Index") == "index" and TC.front_matter(rows, "", "NQA-1 > 5 Numerical Index") == "index"
    assert TC.front_matter(rows, "", "Part 2 > Index of Interpretations") == "index" and TC.front_matter(rows, "Index") == "index"
    assert TC.front_matter(rows, "", "Index Properties of Soils") is None
    assert TC.front_matter(rows, "Table 4-1: General Index of Seismic Parameters") is None
    assert TC.front_matter(rows, "General Index of Seismic Parameters") is None


def test_hyphenated_expansions_and_irregular_plurals():
    """E1 / E2 / E3."""
    assert KI.is_abbreviation("DBA", "Design-Basis Accident") and KI.is_abbreviation("LBE", "Licensing-Basis Event") and KI.is_abbreviation("DBAs", "Design Basis Accidents")
    assert KI.is_abbreviation("SDC", "Seismic Design Category") and not KI.is_abbreviation("DBA", "Design Accident Basis Thing")
    assert KI.singular("analyses") == "analysis" and KI.singular("hypotheses") == "hypothesis" and KI.singular("axes") == "axis" and KI.singular("cases") == "case"
    g = _g(("t3", {"type": "Table", "label": "Table 3-1 Return Category", "text": "Table 3-1 Return Category", "rows": json.dumps([["Category", "Value"], ["SDC-1", "1"], ["SDC-2", "2"]])}),
           ("ent:category", {"type": "Entity", "label": "Category"}), ("ent:sdc1", {"type": "Entity", "label": "SDC-1"}), ("ent:return", {"type": "Entity", "label": "Return"}))
    KI.link_table_entities(g)
    assert [v for _, v in g.out_edges("t3")] == ["ent:sdc1"]                                       # 'Category' and 'Return' are common nouns, not evidence


def test_conflicting_scopes_for_one_url_are_reported(env):
    site, s, sp, rp = env
    cfg = json.loads(sp.read_text())
    url = cfg["standards"][1]["links"][0]["url"]
    cfg["standards"][1]["links"][0]["scope"] = "Appendix B"
    cfg["standards"].append({"id": 9, "document": "Another entry on the same page", "links": [{"text": "same", "url": url}], "update_tracking_urls": []})
    sp.write_text(json.dumps(cfg))
    st = kg_agent.run_once(s)
    assert "disagree about its scope" in st["report"] or any("disagree about its scope" in e for e in st["summary"].get("error_messages", []))
