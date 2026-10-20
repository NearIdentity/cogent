"""Table model, KG context, join inference and the embedders (no database needed)."""
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import networkx as nx
import pytest

import kg_embed as E
import kg_relmap as R
import kg_tables as K
from conftest import DOC_A, DOC_B, stage_document


# ---------------------------------------------------------------------------------------------- numbers / CSV
@pytest.mark.parametrize("cell,expect", [
    ("1,200", (1200.0, "")), ("<0.5", (0.5, "")), ("5 %", (5.0, "%")), ("1.0E-4", (1e-4, "")), ("1.0 x 10^-4 g", (1e-4, "g")),
    ("-12", (-12.0, "")), ("≥ 2.5 psi", (2.5, "psi")), ("1.2 kPa/m", (1.2, "kPa/m")),
    ("Seismic Category 1", None), ("10 CFR 50", None), ("n/a", None), ("3/4", None), ("", None), ("Table 3", None)])
def test_parse_number(cell, expect):
    assert K.parse_number(cell) == expect


def test_frame_header_detection_and_cleanup():
    fr = K.frame([["Name", "", "Value"], ["a", "", "1"], ["b", "", "2"], ["", "", ""]])
    assert fr.header == ["Name", "Value"] and fr.rows == [["a", "1"], ["b", "2"]] and fr.header_detected       # empty column / row dropped
    nofr = K.frame([["1", "2.5"], ["3", "4"]])
    assert nofr.header == ["column 1", "column 2"] and not nofr.header_detected and len(nofr.rows) == 2         # first row is data
    assert K.frame([["only", "header"]]) is None and K.frame([["a"], ["b"]]) is None
    assert K.frame([["h1", "h2"], ["x"]]).rows == [["x", ""]]                                                    # ragged rows are padded


def test_profile_types_roles_units_and_names():
    fr = K.frame([["Seismic Category", "Damping (%)", "Limit", "Count", "Group", "Group"],
                  ["SDC-3", "4", "<0.5", "10", "x", "p"], ["SDC-4", "5", "0.7", "11", "x", "q"], ["SDC-5", "7", "n/a", "12", "y", "r"]])
    c = {x.name: x for x in K.profile(fr)}
    assert c["seismic_category"].role == "key" and c["seismic_category"].key_candidate
    assert c["damping"].pg_type == "bigint" and c["damping"].unit == "%" and (c["damping"].vmin, c["damping"].vmax) == (4, 7)
    assert c["c_limit"].pg_type == "text" and c["count"].pg_type == "bigint"            # 'n/a' makes <90 % of the column numeric
    assert c["c_group"].pg_type == "text" and "c_group_2" in c                            # duplicate header -> unique name
    used = set()
    assert K.pg_identifier("User", used) == "c_user" and K.pg_identifier("1st value", used) == "c_1st_value"
    assert K.pg_identifier("Ünits (kPa)", used) == "units"
    assert K.typed_value("1,200", c["count"]) == 1200 and K.typed_value("x", c["damping"]) is None and K.typed_value("", c["c_group"]) is None


# -------------------------------------------------------------------------------------------- embedding text
def _stage_graph(tmp_path, html=DOC_A, docid="a" * 16):
    home = tmp_path / "h"
    ddir = stage_document(home, docid, html, "https://www.nrc.gov/docs/x.html", ["standard:1"])
    g = nx.node_link_graph(json.loads((ddir / "kg.json").read_text()), edges="edges")
    man = json.loads(next((ddir / "assets").glob("*/manifest.json")).read_text())
    csvs = {a["id"]: next((ddir / "assets").glob(f"*/{a['file']}")) for a in man["assets"]}
    return home, g, man["assets"], csvs


def test_kg_context_and_embedding_text(tmp_path):
    home, g, assets, csvs = _stage_graph(tmp_path)
    t1, t2 = assets[0], assets[1]
    ctx = K.kg_context(g, t1["id"], t1["caption"])
    assert ctx.section_path == ["Seismic Design Criteria for Nuclear Facilities", "3 Seismic Categories"] or ctx.section_path[-1] == "3 Seismic Categories"
    assert ctx.table_label == "Table 3-1" and "seismic design categories and their limit state" in ctx.intro
    assert ctx.notes and ctx.notes[0].startswith("Note: probabilities")
    assert ctx.referenced_by, "paragraphs that mention Table 3-1 are found"
    assert K.kg_context(g, t1["id"], t1["caption"], use_kg=False).section_path == []            # KG context can be switched off
    fr = K.frame(K.read_csv(csvs[t1["id"]]))
    cols = K.profile(fr)
    full = K.row_text(ctx, cols, fr.rows[0])                                                      # default: the entire row
    assert "Seismic Category: SDC-3" in full and "Limit State: Limited permanent distortion" in full and "Annual Probability (per year): 4.0E-4" in full
    assert "Values given for" not in full
    text = K.row_text(ctx, cols, fr.rows[0], mode="text")
    assert "Seismic Category: SDC-3" in text and "Limit State: Limited permanent distortion" in text
    assert "Annual Probability (per year)" in text                                                # numeric column NAME is embedded
    assert "4.0E-4" not in text and "4.0e-4" not in text.lower()                                  # numeric VALUES are not (text mode)
    assert "Values given for: Annual Probability (per year)" in text and "Notes: Note: probabilities" in text
    assert K.recipe("full") == "v2-full" and K.recipe("text") == "v2-text"
    with pytest.raises(ValueError):
        K.recipe("nope")
    assert "3 Seismic Categories" in text and "Table 3-1: Seismic design categories" in text
    tt = K.table_text(ctx, cols, fr.rows)
    assert "Rows: SDC-3; SDC-4; SDC-5" in tt
    assert ctx.sha() == K.kg_context(g, t1["id"], t1["caption"]).sha() != K.kg_context(g, t2["id"], t2["caption"]).sha()


# ---------------------------------------------------------------------------------------------- relationships
def _tables(tmp_path):
    home, g, assets, csvs = _stage_graph(tmp_path)
    out = []
    for a in assets:
        fr = K.frame(K.read_csv(csvs[a["id"]]))
        out.append(R.T(f"{'a' * 16}/{a['id']}", a["id"], K.profile(fr), fr.rows, K.kg_context(g, a["id"], a["caption"])))
    return out


def test_join_inference_uses_names_values_and_kg(tmp_path):
    tabs = _tables(tmp_path)
    rels = R.infer(tabs)
    joins = [r for r in rels if r["kind"] == "join"]
    assert len(joins) == 1
    j = joins[0]
    assert (j["left_column"], j["right_column"]) == ("seismic_category", "seismic_category")
    assert j["cardinality"] == "1:N" and j["left_table"].endswith(tabs[0].node_id)             # Table 3-1 is the 'one' side
    assert j["name_similarity"] == 1.0 and j["containment"] == 1.0 and j["score"] > 0.9
    ev = " ".join(j["evidence"])
    assert "refer to each other" in ev and "3 of 3 distinct values in common" in ev
    assert sorted(j["shared_values"]) == ["sdc-3", "sdc-4", "sdc-5"]
    # decimal columns and unrelated columns are never joined
    assert not any(r.get("left_column") in ("damping", "annual_probability") for r in joins)


def test_no_join_without_evidence_and_same_schema():
    def mk(tid, header, rows):
        fr = K.frame([header] + rows)
        return R.T(tid, tid, K.profile(fr), fr.rows, K.Context())
    a = mk("d/t1", ["Material", "Grade"], [["steel", "A"], ["copper", "B"], ["zinc", "C"], ["tin", "D"]])
    b = mk("d/t2", ["Colour", "Shade"], [["red", "x"], ["blue", "y"], ["green", "z"], ["pink", "w"]])
    assert R.infer([a, b]) == []
    c = mk("d/t3", ["Material", "Grade", "Cost"], [["gold", "A"], ["lead", "B"], ["iron", "C"], ["tin", "D"]])
    d = mk("d/t4", ["Material", "Grade", "Cost"], [["oak", "A"], ["ash", "B"], ["elm", "C"]])
    rels = R.infer([c, d])
    assert [r["kind"] for r in rels if r["kind"] != "join"] == ["same_schema"]
    # near-identical names: units in brackets, plural, 'no.' = 'number'
    assert R.name_similarity("Category No.", "category number") == 1.0 and R.name_similarity("Seismic Categories", "Seismic Category") == 1.0
    assert R.name_similarity("Damping (%)", "Frequency (Hz)") < 0.5


def test_dictionary_and_relationship_map_formats(tmp_path):
    tabs = _tables(tmp_path)
    rels = R.infer(tabs)
    entries = [{"table_id": t.table_id, "pg_table": "kgt_x", "node_id": t.node_id, "caption": t.ctx.caption, "page": None, "section_path": t.ctx.section_path,
                "n_rows": len(t.rows), "columns": [{"name": c.name, "header": c.header, "pg_type": c.pg_type, "role": c.role, "unit": c.unit, "n_nonnull": c.n_nonnull,
                                                    "n_distinct": c.n_distinct, "key_candidate": c.key_candidate, "samples": c.samples} for c in t.cols]} for t in tabs]
    d = R.data_dictionary("a" * 16, "Seismic", "https://x/y.html", entries, rels, paywalled=True)
    col = next(c for t in d["tables"] for c in t["columns"] if c["name"] == "seismic_category" and t["table_id"].endswith(tabs[1].node_id))
    assert col["joins"] and col["joins"][0]["cardinality"] == "N:1"                              # seen from the many side
    md = R.dictionary_markdown(d, rels)
    assert "# Data dictionary: Seismic" in md and "Copyrighted material" in md and "## Relationship map" in md and "`seismic_category`" in md
    mm = R.relationships_mermaid(d, rels)
    assert mm.startswith("erDiagram") and "||--o{" in mm and 'seismic_category = seismic_category' in mm


# ------------------------------------------------------------------------------------------------- embedders
def test_hash_embedder_is_deterministic_normalised_and_similar_for_shared_words():
    h = E.HashEmbedder(64)
    a, b, c = h.embed_docs(["damping welded steel structures", "welded steel damping value", "audit interval of commercial grade items"])
    assert h.embed_docs(["damping welded steel structures"])[0] == a and len(a) == 64 and h.spec == "hash/64"
    assert abs(math.sqrt(sum(x * x for x in a)) - 1) < 1e-9
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert dot(a, b) > dot(a, c) + 0.2


class _Ollama(BaseHTTPRequestHandler):
    calls: list = []
    fail_first = 0
    old_api = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, body))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self.send_response(500)
            self.end_headers()
            return
        if self.path == "/api/embed" and not type(self).old_api:
            out = {"embeddings": [[float(len(t)), 1.0, 0.5] for t in body["input"]]}
        elif self.path == "/api/embeddings":
            out = {"embedding": [float(len(body["prompt"])), 1.0, 0.5]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def ollama():
    _Ollama.calls, _Ollama.fail_first, _Ollama.old_api = [], 0, False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ollama_embedder_batches_prefixes_and_probes_dimension(ollama):
    e = E.OllamaEmbedder("nomic-embed-text", ollama, batch=2)
    docs = e.embed_docs(["a", "bb", "ccc"])
    assert len(docs) == 3 and e.dim == 3 and e.spec == "ollama:nomic-embed-text/3"
    inputs = [b["input"] for p, b in _Ollama.calls if p == "/api/embed"]
    assert inputs == [["search_document: a", "search_document: bb"], ["search_document: ccc"]]          # batches of 2, document prefix
    assert abs(math.sqrt(sum(x * x for x in docs[0])) - 1) < 1e-9
    e.embed_query("what")
    assert _Ollama.calls[-1][1]["input"] == ["search_query: what"]
    plain = E.OllamaEmbedder("some-other-model", ollama)
    plain.embed_docs(["x"])
    assert _Ollama.calls[-1][1]["input"] == ["x"]                                                      # no prefix for unknown models


def test_ollama_embedder_retries_falls_back_and_explains_failures(ollama, monkeypatch):
    monkeypatch.setattr(E.time, "sleep", lambda s: None)
    _Ollama.fail_first = 2
    assert len(E.OllamaEmbedder("m", ollama).embed_docs(["x"])) == 1                                   # transient 500s are retried
    _Ollama.old_api, _Ollama.calls = True, []
    assert len(E.OllamaEmbedder("m", ollama).embed_docs(["x", "y"])) == 2                              # older Ollama: /api/embeddings
    assert any(p == "/api/embeddings" for p, _ in _Ollama.calls)
    with pytest.raises(E.EmbedError, match="ollama pull"):
        E.OllamaEmbedder("m", "http://127.0.0.1:9", retries=1).embed_docs(["x"])
    assert isinstance(E.get_embedder("hash", dim=8), E.HashEmbedder)
    with pytest.raises(ValueError):
        E.get_embedder("nope")
