"""Table validity classifier (TB-02 / TB-04): false tables become paragraphs, real ones survive."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_tablecheck as C  # noqa: E402
from kg_ingest import Element  # noqa: E402

# two-column OCR prose, split by page column (headers are sentence fragments, as in the ASCE 43 report)
PROSE = [["C 6.2.1 General", "the uncertainties in the estimation of hazard are addressed by"],
         ["The seismic hazard curve shall be developed for the site using a probabilistic seismic hazard analysis", "mean and median values of the annual frequency of exceedance should be reported together"],
         ["Where site response analysis is performed the uncertainty in soil properties shall be included in the", "analysis by varying the shear wave velocity profile within the range of the available measurements"],
         ["ground motion is then defined at the free field for each of the hazard levels considered in this", "standard and the corresponding design response spectra are obtained from the uniform hazard spectra"]]

FDC = [["Category", "Return period (yr)", "Annual probability"], ["FDC-1", "500", "2.0E-3"], ["FDC-2", "1,000", "1.0E-3"], ["FDC-3", "2,500", "4.0E-4"], ["FDC-4", "5,000", "2.0E-4"]]

TEXT_TABLE = [["Seismic Design Category", "Description", "Application"],
              ["SDC-1", "Ordinary buildings", "Commercial structures"], ["SDC-2", "Important facilities", "Hospitals and schools"],
              ["SDC-3", "Hazardous facilities", "Chemical plants"], ["SDC-4", "Nuclear facilities", "Reactor buildings"]]

ROSTER = [["Name", "Affiliation"], ["G. Bagchi", "NRC"], ["Jane Q. Public", "DOE"], ["R. Kennedy", "Consultant"], ["M. Hartzman", "Bechtel"]]


def test_two_column_prose_is_rejected():
    v = C.classify(PROSE)
    assert v.kind == "prose" and v.score >= 4 and any("running text" in r or "sentence" in r for r in v.reasons)


@pytest.mark.parametrize("rows", [FDC, TEXT_TABLE])
def test_real_tables_survive(rows):
    assert C.classify(rows).kind == "table"


def test_captioned_table_needs_stronger_evidence():
    assert C.classify(PROSE, caption="Table 3-1 Hazard levels").score == C.classify(PROSE).score          # same features ...
    borderline = [["a long header with many words in it today", "another long header with many words here"], ["short", "short"], ["x", "y"]]
    assert C.classify(borderline, caption="Table 2 Things").kind == "table"


def test_roster_is_flagged_not_rejected():
    v = C.classify(ROSTER)
    assert v.kind == "table" and v.roster


def test_demotion_restores_reading_order_and_renumbers():
    els = [Element("h0001", "heading", text="Intro", level=1), Element("tbl0002", "table", table=PROSE, text="x", page=3),
           Element("tbl0003", "table", table=FDC, text="Category", page=4), Element("tbl0004", "table", table=ROSTER, text="Name", page=5)]
    seen = []
    stats = C.apply(els, lambda xs: seen.append(len(xs)))
    assert stats == {"checked": 3, "demoted": 1, "rosters": 1, "front_matter": 0} and seen == [4]
    assert [e.kind for e in els] == ["heading", "paragraph", "table", "table"] and els[1].table is None
    text = els[1].text
    assert text.index("C 6.2.1 General") < text.index("The seismic hazard curve") < text.index("the uncertainties in the estimation")   # left column, then right
    assert els[1].meta["demoted_table"]["reasons"] and els[3].meta["roster"] and els[2].kind == "table"


def test_switch_off(monkeypatch):
    monkeypatch.setenv("KG_TABLE_CHECK", "off")
    els = [Element("tbl0001", "table", table=PROSE, text="x")]
    assert C.apply(els)["demoted"] == 0 and els[0].kind == "table"


def test_names_are_not_join_keys():
    import kg_relmap as R
    assert C.name_share(["G. Bagchi", "Jane Q. Public", "R. Kennedy"]) == 1.0 and C.name_share(["FDC-1", "SDC-2"]) == 0.0
    assert R.name_share is C.name_share


def test_rosters_do_not_produce_joins():
    """TB-04: committee lists that share a person's name are not related tables."""
    import kg_relmap as R
    import kg_tables as K

    def mk(tid, header, rows):
        fr = K.frame([header] + rows)
        return R.T(tid, tid, K.profile(fr), fr.rows, K.Context())
    a = mk("a/1", ["Member", "Affiliation"], [["G. Bagchi", "NRC"], ["Jane Q. Public", "DOE"], ["R. Kennedy", "Consultant"], ["M. Hartzman", "Bechtel"]])
    b = mk("b/2", ["Member", "Role"], [["G. Bagchi", "Chair"], ["Jane Q. Public", "Secretary"], ["R. Kennedy", "Member"], ["T. Smith", "Member"]])
    assert R.infer([a, b]) == []
    c = mk("c/3", ["Code", "Value"], [["FDC-1", "1"], ["FDC-2", "2"], ["FDC-3", "3"], ["FDC-4", "4"]])
    d = mk("d/4", ["Code", "Name"], [["FDC-1", "a"], ["FDC-2", "b"], ["FDC-3", "c"], ["FDC-5", "d"]])
    assert len(R.infer([c, d])) == 1                                                  # real code columns still join


def test_html_prose_table_becomes_paragraph_in_the_graph(tmp_path):
    """End to end through the ingest pipeline: the layout table of running text is a Paragraph (with a note), the real table stays a Table."""
    import kg_ingest
    prose_rows = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in PROSE)
    fdc_rows = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in FDC)
    html = (f"<html><body><h1>Seismic</h1><table>{prose_rows}</table>"
            f"<p>Table 5-1 Return periods</p><table>{fdc_rows}</table></body></html>")
    src = tmp_path / "doc.html"
    src.write_text(html)
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "assets"))
    tables = [n for n, a in g.nodes(data=True) if a.get("type") == "Table"]
    assert len(tables) == 1 and "FDC-1" in g.nodes[tables[0]]["rows"]
    assert g.graph["tables_demoted"] == 1
    assert any("seismic hazard curve" in (a.get("text") or "") and a.get("type") == "Paragraph" for _, a in g.nodes(data=True))
    assert not any("seismic hazard curve" in str(a.get("rows", "")) for _, a in g.nodes(data=True))


# NE-STD-1027 Table 1-1 (TB-03): a two-row header repeated on every page, with OCR noise in the repeats
H1 = ["Radionuclide", "Dose pathway (A)", "Limit (Sv)"]
H2 = ["", "Inhalation", "Ingestion"]
NOISY1 = ["Rdilid", "Dose pathway (A)", "Limit (Sv)"]
NOISY2 = ["", "Inhalatlon", "Ingestion"]
D = [["Cs-137", "1.0E-3", "2.0E-3"], ["Sr-90", "3.0E-3", "4.0E-3"], ["I-131", "5.0E-4", "6.0E-4"], ["Pu-239", "7.0E-6", "8.0E-6"]]


def test_repeated_two_row_header_is_removed_even_with_ocr_noise():
    rows = [H1, H2, D[0], D[1], NOISY1, NOISY2, D[2], D[3], H1, H2]
    clean, removed = C.drop_repeated_headers(rows)
    assert removed == 4 and clean == [H1, H2, D[0], D[1], D[2], D[3]]


def test_single_row_header_repeated_and_data_rows_untouched():
    rows = [H1, D[0], D[1], H1, D[2], D[3]]
    assert C.drop_repeated_headers(rows) == ([H1, D[0], D[1], D[2], D[3]], 1)
    same_data = [["Name", "Value"], ["a", "1"], ["a", "1"], ["a", "1"]]                      # repeated data rows are data
    assert C.drop_repeated_headers(same_data) == (same_data, 0)
    assert C.drop_repeated_headers([H1, D[0], D[1]]) == ([H1, D[0], D[1]], 0)                # too short to judge


def test_continuation_header_prefix():
    assert C.header_prefix_len([H1, H2, D[0]], [NOISY1, NOISY2, D[1]]) == 2
    assert C.header_prefix_len([H1, D[0]], [H1, D[1]]) == 1
    assert C.header_prefix_len([H1, H2, D[0]], [D[1], D[2]]) == 0


def test_apply_cleans_the_table_and_records_it():
    els = [Element("tbl0001", "table", table=[H1, H2, D[0], D[1], NOISY1, NOISY2, D[2], D[3]], text="Radionuclide | Dose pathway (A) | Limit (Sv)", page=3)]
    stats = C.apply(els)
    assert stats["header_rows_removed"] == 2 and els[0].meta["repeated_header_rows_removed"] == 2 and len(els[0].table) == 6 and els[0].kind == "table"


# ---- N-1 .. N-4: front matter, caption clean-up, display titles
TOC_LEADERS = [["1.0", "INTRODUCTION ..................", "6"], ["1.1", "Purpose ..................", "6"], ["1.2", "Scope ..................", "7"],
               ["2.0", "DEFINITIONS ..................", "9"], ["2.1", "Terms ..................", "9"]]
TOC_PLAIN = [["Section", "Title", "Page"], ["1", "Introduction", "3"], ["2", "Scope", "4"], ["2.1", "Applicability", "4"], ["3", "Design basis", "8"], ["A", "Appendix tables", "21"]]
GLOSSARY = [["Acronym", "Meaning"], ["NPH", "natural phenomena hazard"], ["SDC", "seismic design category"], ["WDC", "wind design category"],
            ["FDC", "flood design category"], ["SSC", "structure, system and component"], ["PAC", "protective action criteria"]]


def test_front_matter_is_recognised():
    assert C.front_matter(TOC_LEADERS) == "toc" and C.front_matter(TOC_PLAIN[1:]) == "toc" and C.front_matter(GLOSSARY) == "glossary"
    assert C.front_matter([["x", "y"]] * 5, caption="Table of Contents") == "toc"


@pytest.mark.parametrize("rows", [FDC, TEXT_TABLE, ROSTER, [["Material", "Allowable Stress", "Count"], ["A36", "21.6", "1"], ["A572", "30.0", "2"], ["A992", "32.4", "3"], ["A500", "40.0", "4"]]])
def test_real_tables_are_not_front_matter(rows):
    assert C.front_matter(rows) is None


def test_front_matter_is_flagged_not_demoted_and_not_exported(tmp_path):
    import json

    import kg_ingest
    toc_html = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in TOC_LEADERS)
    gl_html = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in GLOSSARY)
    fdc_html = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in FDC)
    src = tmp_path / "d.html"
    src.write_text(f"<html><body><h1>Std</h1><table>{toc_html}</table><table>{gl_html}</table><p>Table 5‑1 Return periods</p><table>{fdc_html}</table></body></html>")
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "a"))
    tabs = {n: a for n, a in g.nodes(data=True) if a.get("type") == "Table"}
    assert sorted(a.get("front_matter", "") for a in tabs.values()) == ["", "glossary", "toc"]       # all three stay in the graph
    man = json.loads(next((tmp_path / "a").glob("*/manifest.json")).read_text())
    assert len([a for a in man["assets"] if a["kind"] == "table"]) == 1                               # only the data table is exported
    assert sorted(x["reason"] for x in man["skipped"]) == ["front matter (glossary)", "front matter (toc)"]


@pytest.mark.parametrize("raw,want", [("Table 5‑1 Flood return periods", "Table 5-1 Flood return periods"), ("Table 2--1: Design categories", "Table 2-1: Design categories"),
                                      ("Table 5−2 x", "Table 5-2 x"), ("Applies to SDC-1 andSDC-2 only", "Applies to SDC-1 and SDC-2 only"),
                                      ("The McKinney iPhone  table", "The McKinney iPhone table"), ("Table 3-1: Coefficients (SDC-1/2)", "Table 3-1: Coefficients (SDC-1/2)")])
def test_caption_clean_up(raw, want):
    assert C.fix_caption(raw) == want


def test_table_label_keeps_the_subnumber():
    import kg_tables as K
    for cap, label in [("Table 5‑1 Flood", "Table 5-1"), ("Table 5‑2: Flood", "Table 5-2"), ("Table 2--1 Design", "Table 2-1"), ("Table 5-3 x", "Table 5-3")]:
        assert K.kg_context(None, "x", cap).table_label == label


def test_displayed_document_title_replaces_pdf():
    from types import SimpleNamespace

    import kg_tableservice as TS
    svc = object.__new__(TS.TableService)
    svc.store = SimpleNamespace(_config=lambda: {"standard:16": {"document": "NE-STD-1020 Natural phenomena hazards"}})
    svc._label = lambda c, d, cache: {"paywalled": False, "warnings": [], "holder": None}
    t = svc._decorate(None, {"docid": "d1", "doc_label": "PDF", "entries": ["standard:16"], "source_url": "https://x/a.pdf", "caption": "c"}, {})
    assert t["doc_label"] == "NE-STD-1020 Natural phenomena hazards" and t["source"]["document"] == t["doc_label"]
    t2 = svc._decorate(None, {"docid": "d1", "doc_label": "Seismic Design Criteria for SSCs", "entries": ["standard:16"], "source_url": "", "caption": "c"}, {})
    assert t2["doc_label"] == "Seismic Design Criteria for SSCs"


@pytest.mark.parametrize("raw,want", [("2.3.3Criteria and Guidance", "2.3.3 Criteria and Guidance"), ("4.1.2SCOPE OF WORK", "4.1.2 SCOPE OF WORK"),
                                      ("2.3.3 Criteria", "2.3.3 Criteria"), ("3D models", "3D models"), ("2Scope", "2Scope"), ("1.2.3a lower", "1.2.3a lower")])
def test_heading_number_gets_its_space(raw, want):
    assert C.fix_heading(raw) == want
