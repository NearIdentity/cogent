"""Unit deduction for numeric columns: evidence from headers, cells, table metadata and the KG; strict vs broad vs off."""
import json

import networkx as nx
import pytest

import kg_tables as K
import kg_units as U
from conftest import DOC_U, stage_document


def cx(**kw):
    return {"caption": "", "intro": "", "notes": [], "section_texts": [], "entities": [], **kw}


def cands(header, cells, ctx=None, stats=None):
    return U.candidates(header, cells, True, ctx or cx(), stats)


def view(c, mode):
    d = U.decide(c, mode)
    return d["unit"]


def test_normalize_and_find_units():
    assert U.normalize("kips per square inch").symbol == "ksi" and U.normalize("kPa").symbol == "kPa" and U.normalize("per year").symbol == "1/yr"
    assert U.normalize("percent").symbol == "%" and U.normalize("xyz") is None
    assert [u.symbol for u, _ in U.find_units("Pressure of 5 psi and 200 kPa")] == ["kPa", "psi"] or {u.symbol for u, _ in U.find_units("5 psi and 200 kPa")} == {"psi", "kPa"}
    assert "Pa" not in {u.symbol for u, _ in U.find_units("200 kPa")}                       # the spelling inside a longer one is not a second unit
    assert U.find_units("a number in the table of m") == []                                  # ambiguous words are not units in running text
    assert {u.symbol for u, _ in U.find_units("per year and per hour")} == {"1/yr", "1/h"}


def test_declared_units():
    d = lambda t: {u.symbol for u, _ in U.declared_units(t)}
    assert d("All dimensions in mm unless otherwise noted") == {"mm"} and d("Units: kPa") == {"kPa"} and d("values are given in psi") == {"psi"}
    assert d("Lengths in m") == {"m"} and d("Dimensions in mm unless otherwise noted") == {"mm"}
    assert d("All widths are given in inches") == {"in"} and d("The plate is 12 mm thick") == set()          # a mention is not a declaration


def test_candidate_sources_and_confidences():
    c = cands("Damping (%)", ["4", "5"])
    assert [(x["unit"], x["source"]) for x in c] == [("%", "header")]
    assert [(x["unit"], x["source"]) for x in cands("Annual probability per year", ["1e-4"])][0] == ("1/yr", "header_phrase")
    assert cands("Spacing", ["12 in.", "16 in.", "20 in."])[0]["source"] == "cells"
    decl = cands("Allowable Stress", ["20", "30"], cx(caption="Table 5-2: values in ksi"))
    assert decl[0]["unit"] == "ksi" and decl[0]["source"] == "table_declaration" and decl[0]["confidence"] == U.BASE_CONF["table_declaration"]
    assert cands("Allowable Stress", ["20"], cx(intro="A stress of 200 kPa was observed"))[0]["source"] == "table_text"
    assert cands("Thickness", ["1", "2"], cx(section_texts=["Plates are 12 mm thick."]))[0]["source"] == "section_text"
    assert cands("Pressure", ["1", "2"], cx(entities=["MPa"]))[0]["source"] == "kg_entity"
    assert cands("Frequency", ["1"], None, {"frequency": {"Hz": 5}})[0]["source"] == "document"
    assert cands("Damping", ["1"])[0] == {"unit": "%", "quantity": "ratio", "source": "header_semantics", "confidence": 0.35, "evidence": "'Damping' is a ratio; customary unit %"}
    assert cands("Count", ["1", "2"]) == [] and U.candidates("Name", ["a"], False) == []
    # dimension compatibility: a length declaration does not become the unit of a pressure column
    assert not [x for x in cands("Pressure", ["1"], cx(caption="Dimensions in mm")) if x["unit"] == "mm"]
    # a column with its own unit does not inherit the table's
    own = cands("Stress (MPa)", ["1"], cx(caption="values in ksi"))
    assert [x["unit"] for x in own] == ["MPa"]


def test_strict_is_narrow_broad_is_diverse_off_is_explicit_only():
    only_semantics = cands("Damping", ["4", "5"])
    assert view(only_semantics, "strict") is None and view(only_semantics, "broad") == "%" and view(only_semantics, "off") is None
    b = U.decide(only_semantics, "broad")
    assert b["inferred"] is True and b["confidence"] == 0.35 and b["sources"] == ["header_semantics"]
    # independent evidence reinforces a unit in broad mode
    multi = cands("Frequency", ["1"], cx(section_texts=["Modes above 5 Hz matter."]), {"frequency": {"Hz": 6}})
    assert U.decide(multi, "broad")["confidence"] > 0.6 and set(U.decide(multi, "broad")["sources"]) >= {"section_text", "document", "header_semantics"}
    # conflict: strict gives nothing (and says so), broad picks the best and lists the alternative
    conflict = cands("Pressure", ["1"], cx(caption="values in psi", notes=["Units: kPa"]))
    s = U.decide(conflict, "strict")
    assert s["unit"] is None and s["conflict"] and len(s["evidence"]) == 2
    bb = U.decide(conflict, "broad")
    assert bb["unit"] in ("psi", "kPa") and bb["alternatives"][0]["unit"] in ("psi", "kPa") and bb["alternatives"][0]["unit"] != bb["unit"]
    explicit = cands("Damping (%)", ["4"])
    assert view(explicit, "off") == "%" and U.decide(explicit, "broad")["inferred"] is False
    with pytest.raises(ValueError):
        U.decide(explicit, "nope")


def test_units_from_a_real_document_kg(tmp_path):
    home = tmp_path / "h"
    d = stage_document(home, "c" * 16, DOC_U, "https://www.nrc.gov/docs/limits.html", ["standard:1"])
    g = nx.node_link_graph(json.loads((d / "kg.json").read_text()), edges="edges")
    man = json.loads(next((d / "assets").glob("*/manifest.json")).read_text())
    texts = [str(x.get("text") or x.get("label") or "") for _, x in g.nodes(data=True) if x.get("type") in ("Paragraph", "Heading", "Table")]
    stats = U.doc_unit_stats(texts)
    assert stats["pressure"]["kPa"] == 3 and stats["pressure"]["ksi"] >= 1
    res = {}
    for a in man["assets"]:
        fr = K.frame(K.read_csv(next((d / "assets").glob(f"*/{a['file']}"))))
        cols = K.profile(fr)
        ctx = K.kg_context(g, a["id"], a["caption"])
        U.apply(cols, fr.rows, ctx, stats)
        res[a["caption"][:9]] = {c.header: c for c in cols}
    t1, t2 = res["Table 5-1"], res["Table 6-1"]
    assert "Plates" not in str(t1) and K.kg_context(g, man["assets"][0]["id"], "").section_texts                  # section neighbours are collected
    assert t1["Allowable Stress"].unit == "ksi" and t1["Allowable Stress"].unit_source == "table_declaration"     # caption: (units: ksi)
    assert t1["Temperature (°C)"].unit == "°C" and t1["Temperature (°C)"].unit_source == "header"
    # Thickness: the introduction ('All dimensions ... in mm unless otherwise noted') is a declaration -> even narrow mode takes mm
    th = t1["Thickness"]
    assert th.unit == "mm" and th.unit_source == "table_declaration" and th.quantity == "length"
    assert "ksi" not in {x["unit"] for x in th.unit_candidates}                                                    # the caption's pressure unit does not apply to a length
    # Damping: only the customary unit is known
    assert t1["Damping"].unit == "" and U.decide(t1["Damping"].unit_candidates, "broad")["unit"] == "%"
    assert t1["Count"].unit_candidates == [] and t1["Count"].quantity == ""
    # Pressure in table 6-1: only text mentions kPa (no declaration)
    pr = t2["Pressure"]
    assert U.decide(pr.unit_candidates, "strict")["unit"] is None and U.decide(pr.unit_candidates, "broad")["unit"] == "kPa"
    assert {x["source"] for x in pr.unit_candidates} >= {"section_text"} and stats["pressure"]["kPa"] == 3
    # a unit carried by every numeric cell is explicit
    assert t2["Weight 5 kg"].unit == "kg" and t2["Weight 5 kg"].unit_source == "cells" and t2["Weight 5 kg"].quantity == "mass"
