"""Relation vocabulary (X-01): no junk relation types from text spans, table headers or table-of-contents lines."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_ingest  # noqa: E402
import kg_predicates as P  # noqa: E402

SEMANTIC = {"related_to", "has_value", "comply_with", "require"}
JUNK = ["or", "and", "10", "the", "“", "—", "ADAMS_accession_No./web_link/_Federal_Register_citation", "-15881_(186_pages", "/30/2026",
        "https://www.federalregister.gov/d/2026-06048", "INTRODUCTION.....…", "", "   ", "of the"]


@pytest.mark.parametrize("raw", JUNK)
def test_junk_predicates_become_related_to(raw):
    assert P.relation(raw) == "related_to" and P.relation(raw, strict=False) == "related_to"


@pytest.mark.parametrize("raw,want", [("is required by", "require_by"), ("requires", "require"), ("shall be performed in", "perform_in"),
                                      ("endorsed in", "endorse_in"), ("as defined in", "define_in"), ("applies to", "apply_to"),
                                      ("in accordance with", "comply_with"), ("supersedes", "supersede"), ("related_to", "related_to")])
def test_verbs_are_reduced_to_lemma_and_preposition(raw, want):
    assert P.relation(raw) == want


def test_llm_predicates_only_need_to_be_clean():
    assert P.relation("located in", strict=False) == "located_in" and P.relation("founded by the", strict=False) == "founded_by"
    assert P.relation("see https://x.org/a", strict=False) == "related_to"


def test_table_cells_use_one_relation():
    assert P.relation("Return period (yr)", table=True) == "has_value"


def test_toc_lines():
    assert P.is_toc_like("INTRODUCTION.................. 1") and P.is_toc_like("Scope . . . . . . . . 4") and not P.is_toc_like("See section 5.2. Then continue.")


def test_graph_relation_types_are_few_and_clean(tmp_path):
    """The acceptance test of X-01 on text shaped like the Part 53 preamble: lists, URLs, numbers, TOC lines, a table with long headers."""
    para = ("The Commission or the staff and the applicant reviewed the rule. Nuclear Regulatory Commission 10 Federal Register 2026 notice. "
            "Applicants shall comply with Part 53 and Appendix B. The guidance is described in Regulatory Guide documents. "
            "ADAMS Accession No. ML26001A001 — see https://www.federalregister.gov/d/2026-06048 for Federal Register Notice. "
            "The Licensee requires Quality Assurance Program approval by the Commission.")
    toc = "<p>INTRODUCTION.................. 1</p><p>Definitions and Scope . . . . . . . . 4</p>"
    table = ("<table><tr><th>Facility Name</th><th>FFRDC Facilities Unm Thres</th><th>Atmospheric Pressure Change</th></tr>"
             "<tr><td>Alpha Plant</td><td>12</td><td>3.4</td></tr><tr><td>Beta Plant</td><td>15</td><td>2.2</td></tr></table>")
    src = tmp_path / "d.html"
    src.write_text(f"<html><body><h1>Rule</h1><p>{para}</p>{toc}{table}</body></html>")
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(tmp_path / "a"))
    rels = {d["relation"] for _, _, d in g.edges(data=True)}
    semantic = rels - {"CONTAINS", "NEXT", "MENTIONS", "CITES", "references", "HAS_DOCUMENT"}
    assert len(rels) <= 30 and semantic == SEMANTIC, semantic
    assert not [r for r in rels if re.fullmatch(r"[\W\d_]+", r) or "http" in r or "." in r]
    toc = {n for n, a in g.nodes(data=True) if "......" in str(a.get("text", "")) or ". . . ." in str(a.get("text", ""))}
    assert toc and not [1 for _, _, d in g.edges(data=True) if d.get("source") in toc]                  # no relations from TOC lines
    cols = {d.get("predicate") for _, _, d in g.edges(data=True) if d.get("relation") == "has_value"}
    assert {"FFRDC Facilities Unm Thres", "Atmospheric Pressure Change"} <= cols                       # the header stays on the edge
