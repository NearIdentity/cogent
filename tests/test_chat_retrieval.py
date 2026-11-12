"""Keyword search over the graphs: exact codes and identifiers are found even when the embeddings cannot tell the passages apart."""
import sys
from pathlib import Path

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import ENTRY, FakeConn, base_tools, cfg, make_llm, node  # noqa: E402

import kg_telemetry as telemetry  # noqa: E402
from kg_chat.agent import CogentAgent  # noqa: E402
from kg_chat.keyword import Bm25Index, fuse, tokenize  # noqa: E402
from kg_chat.source import KnowledgeSource  # noqa: E402


class FlatEmbeddings(Embeddings):
    """Embeddings that cannot find the code: the passage that mentions it is the LEAST similar to every query (the worst case for vectors)."""

    def embed_documents(self, texts):
        return [[0.0, 1.0] if "nei 18-04" in t.lower() else [1.0, 0.0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


def doc(text, entry="standard:1", node_id="n", section=""):
    return Document(page_content=text, metadata={"entry": entry, "node": node_id, "section": section})


def big_graph():
    """Twelve filler sections, then the one that mentions the codes (last, so a tie-breaking vector search never reaches it)."""
    t = "ab12cd34"
    nodes = [node(f"{t}:doc:a.html", "Document", "Seismic Design Criteria")]
    n = 0
    for i in range(12):
        n += 1
        nodes.append(node(f"{t}:h{n:04d}", "Heading", f"{i + 1} General topic {i}", level="2"))
        n += 1
        nodes.append(node(f"{t}:p{n:04d}", "Paragraph", f"General discussion {i} of facilities, safety and licensing with no particular code in it."))
    n += 1
    nodes.append(node(f"{t}:h{n:04d}", "Heading", "13 Referenced documents", level="2"))
    n += 1
    nodes.append(node(f"{t}:p{n:04d}", "Paragraph", "The guide endorses NEI 18-04 and refers to ASTM A572 steel and 10 CFR 50.69 for categorisation."))
    return {"graph": {"sources": [{"tag": t, "source": "https://www.example.org/d.html"}]}, "nodes": nodes, "edges": [], "truncated": False}


def make_agent(retrieval="hybrid", top_k=3, table_mode=None):
    tools = base_tools(table_mode)
    tools["export_graph"] = lambda a: {"graph": big_graph()}
    conn = FakeConn(tools)
    c = cfg(retrieval=retrieval, top_k=top_k)
    return CogentAgent(c, KnowledgeSource(conn, c), llm=make_llm(lambda t: ("ok", True)), embeddings=FlatEmbeddings()), conn


# ---------------------------------------------------------------------------- the index

def test_tokens_keep_identifiers_and_treat_separators_as_spaces():
    assert tokenize("NEI 18-04 and A572, 10 CFR 50.69, DOE-STD-1104_2016") == ["nei", "18", "04", "and", "a572", "10", "cfr", "50.69", "doe", "std", "1104", "2016"]
    assert tokenize("RG 1.233 Rev. 1") == ["rg", "1.233", "rev", "1"] and tokenize("") == []
    from kg_chat.keyword import has_identifier, query_tokens
    assert query_tokens("What does the guide say about NEI 18-04?") == ["guide", "nei", "18", "04"] and query_tokens("what is it") == []
    assert has_identifier("allowable stress for A572") and has_identifier("see 50.69") and not has_identifier("general discussion of facilities")


def test_exact_code_beats_a_near_miss_and_separators_do_not_matter():
    docs = [doc("The guide endorses NEI 21-07 for design basis events."), doc("The guide endorses NEI 18-04 for licensing basis development."),
            doc("General text about nuclear safety."), doc("Requirements per DOE-STD-1104 apply.")]
    idx = Bm25Index(docs)
    assert idx.search("NEI 18-04", 2)[0][0] is docs[1]
    assert idx.search("DOE STD 1104", 1)[0][0] is docs[3] and idx.search("doe-std-1104", 1)[0][0] is docs[3]
    assert idx.search("A572", 3) == [] and idx.search("", 3) == []                       # nothing contains it: nothing is invented
    assert [d for d, _ in idx.search("nuclear", 5)] == [docs[2]]
    assert len(idx.search("guide NEI", 1)) == 1


def test_section_headings_are_searchable():
    d = Document(page_content="Values are listed below.", metadata={"section": "4 Damping", "node": "x"})
    assert Bm25Index([d, doc("other")]).search("damping", 1)[0][0] is d


def test_reciprocal_rank_fusion():
    a, b, c, d = (doc(x, node_id=x) for x in "abcd")
    out = fuse([[a, b, c], [d, b]], 3)
    keys = [x.page_content for x, _ in out]
    assert keys[0] == "b"                                                                   # ranked by both lists
    assert [x.page_content for x, _ in fuse([[a, b], [d]], 2, weights=[1.0, 3.0])][0] == "d"        # a heavier list outweighs a better rank elsewhere
    assert dict((x.page_content, by) for x, by in out)["b"] == [0, 1] and set(keys) <= {"a", "b", "c", "d"} and len(keys) == 3


# ---------------------------------------------------------------------------- in the agent

def codes(agent, mode_docs):
    return any("NEI 18-04" in d.page_content for d in mode_docs)


def test_keyword_leg_finds_what_the_embeddings_cannot():
    sem, _ = make_agent("semantic")
    kw, _ = make_agent("keyword")
    hy, _ = make_agent("hybrid")
    q = "What does it say about NEI 18-04?"
    assert not codes(sem, sem.retrieve(q))                                                  # flat vectors: ties, the filler sections come first
    assert codes(kw, kw.retrieve(q)) and codes(hy, hy.retrieve(q))
    assert len(hy.retrieve(q)) == 3


def test_hybrid_keeps_the_vector_results_when_the_query_has_no_code():
    hy, _ = make_agent("hybrid")
    assert len(hy.retrieve("general discussion of facilities")) == 3
    kw, _ = make_agent("keyword")
    assert kw.retrieve("zzzz qqqq") == []


def test_answer_cites_the_chunk_found_by_keyword():
    seen = []
    tools = base_tools()
    tools["export_graph"] = lambda a: {"graph": big_graph()}
    c = cfg(retrieval="hybrid", top_k=3)
    agent = CogentAgent(c, KnowledgeSource(FakeConn(tools), c), llm=make_llm(lambda t: (seen.append(t) or "ok [S1]", True)), embeddings=FlatEmbeddings())
    r = agent.ask("Which guide endorses NEI 18-04?")
    assert "NEI 18-04" in seen[0].split("</context>")[0] and r.found_in_context
    assert any("Referenced documents" in s.section for s in r.sources) and r.sources[0].entry == "standard:9"


def test_retrieval_sources_are_counted():
    m = telemetry.metrics()
    before = {s: m.value("cogent_chat_retrieval_hits_total", source=s) for s in ("vector", "keyword", "both")}
    hy, _ = make_agent("hybrid")
    hy.retrieve("What does it say about NEI 18-04?")
    after = {s: m.value("cogent_chat_retrieval_hits_total", source=s) for s in ("vector", "keyword", "both")}
    assert sum(after.values()) - sum(before.values()) == 3 and after["keyword"] + after["both"] > before["keyword"] + before["both"]


def test_the_table_search_is_asked_for_hybrid_retrieval_too():
    agent, conn = make_agent(table_mode="rows")
    agent.ask("allowable stress for A572")
    call = [a for n, a in conn.calls if n == "lookup_values"][0]
    assert call["retrieval"] == "hybrid"
    c2 = cfg(table_retrieval="lexical")
    conn2 = FakeConn(base_tools("rows"))
    KnowledgeSource(conn2, c2).lookup("x")
    assert conn2.calls[-1][1]["retrieval"] == "lexical"


def test_retrieval_option_is_validated_and_exposed():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cfg(retrieval="fuzzy")
    import subprocess
    out = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "src" / "kg_chat_app.py"), "chat", "--help"], capture_output=True, text=True).stdout
    assert "--retrieval {hybrid,semantic,keyword}" in out
