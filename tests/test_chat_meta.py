"""Questions about the server itself, "not found" diagnostics, glued OCR text and /explain: the gaps found with a real cogent server."""
import sys
from pathlib import Path

import pytest
from langchain_core.documents import Document

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import ENTRY, OVERVIEW, FakeConn, KeywordEmbeddings, base_tools, cfg, make_llm  # noqa: E402

from kg_chat.agent import CogentAgent  # noqa: E402
from kg_chat.keyword import Bm25Index, is_glued  # noqa: E402
from kg_chat.source import KnowledgeSource, facts_markdown, looks_meta  # noqa: E402

TOOLS = [{"name": "get_overview", "read_only": True}, {"name": "lookup_values", "read_only": True}, {"name": "set_session_preferences", "read_only": False}]


def make_agent(handler, table_mode=None):
    c = cfg()
    conn = FakeConn(base_tools(table_mode))
    return CogentAgent(c, KnowledgeSource(conn, c), llm=make_llm(handler), embeddings=KeywordEmbeddings()), conn


def test_which_questions_are_about_the_server():
    for q in ("What is staged, and is the database read-only?", "How many standards are on board?", "Which tools does the server offer?",
              "Are there open warnings?", "When was the last agent run?"):
        assert looks_meta(q), q
    for q in ("What damping applies to welded steel structures?", "Quote the requirement for a quality plan."):
        assert not looks_meta(q), q


def test_facts_state_what_is_staged_the_database_login_and_the_tools():
    md = facts_markdown(OVERVIEW, [dict(ENTRY, entry="standard:1", name="10 CFR Part 50", nodes=5277)], TOOLS)
    assert "entries: 45 (with a knowledge graph: 45)" in md and "current 33" in md and "partially_staged 1" in md and "open warnings: 19" in md
    assert "54 documents checked, 40 unchanged" in md and "10 failed, 4 deferred" in md and "older extraction recipe: standard:20" in md
    assert "tables: 623" in md and "database login used by the server: `cogent_ro`" in md and "can SELECT: yes" in md
    assert "3 tools; annotated read-only: 2; not annotated read-only: set_session_preferences" in md
    assert "`run_sql` (a single read-only SELECT, enabled by the operator): not offered" in md
    assert "- standard:1 10 CFR Part 50: current, graph of 5277 nodes" in md and "could not be fetched in the last run: 2" in md
    no_db = facts_markdown({"entries": 1, "table_database": {"configured": False}}, [], [])
    assert "not configured: the server has no table database" in no_db and "0 tools" in no_db


def test_a_question_about_the_server_is_answered_from_its_overview():
    seen = []

    def handler(text):
        seen.append(text)
        ctx = text.split("</context>")[0]
        return ("45 entries are staged; the server's database login is cogent_ro [S1].", "cogent_ro" in ctx)
    agent, conn = make_agent(handler, table_mode="rows")
    r = agent.ask("What is staged, and is the database read-only?")
    assert r.found_in_context and "cogent_ro" in seen[0] and "MCP server" in seen[0] and "[S1] MCP server" in seen[0]
    assert r.sources[0].entry_name == "MCP server" and r.sources[0].section.startswith("staged content") and r.sources[0].url == ""
    assert r.tables and r.tables[0].title == "Entries on the server"                                        # "what is staged" also shows the entries as a table
    assert "Excerpts from \"MCP server\" describe the server itself" in seen[0]
    names = [n for n, _ in conn.calls]
    assert "get_overview" in names and "list_entries" in names
    n_before = names.count("get_overview")
    agent.ask("Is the database read-only?")
    assert [n for n, _ in conn.calls].count("get_overview") == n_before                                     # reused within notice_ttl
    other, conn2 = make_agent(lambda t: ("ok", True))
    other.ask("What damping applies to welded steel structures?")
    assert "get_overview" not in [n for n, _ in conn2.calls]                                                  # ordinary questions do not pay for it


def test_not_found_says_what_was_searched():
    agent, _ = make_agent(lambda t: ("I could not find that in the knowledge graphs.", False), table_mode="no_match")
    r = agent.ask("What is the allowable stress for ASTM A572? Quote the source.")
    assert not r.found_in_context and r.sources == [] and r.tables == []
    assert len(r.notices) == 1
    t = r.notices[0].text
    assert "No indexed text mentions A572" in t and "the table search found no row mentioning A572" in t and "/entries" in t and "/explain" in t
    plain, _ = make_agent(lambda t: ("not found", False))
    assert plain.ask("Who won the match?").notices == []                                                   # nothing specific to say: no noise


def test_glued_ocr_text_is_still_found():
    glued = Document(page_content="Intheallowable(working)stressapproach,thedesigncapacityisbasedontheelasticsectionpropertiesandallowablestresses.",
                     metadata={"entry": "standard:20", "node": "p1446", "section": "4.3"})
    normal = Document(page_content="Seismic loads are combined with dead loads as described elsewhere.", metadata={"entry": "standard:20", "node": "p9"})
    assert is_glued(glued.page_content) and not is_glued(normal.page_content)
    idx = Bm25Index([normal, glued])
    assert idx.search("allowable stress approach", 1)[0][0] is glued                                         # tokens cannot match; the run-together text can
    assert idx.search("allowable stress", 1)[0][0] is glued and idx.search("seismic loads", 1)[0][0] is normal
    assert idx.has_token("allowable") and idx.has_token("elasticsection") and not idx.has_token("a572")


def test_explain_shows_what_retrieval_does_without_calling_the_model():
    agent, _ = make_agent(lambda t: pytest.fail("the model must not be called"), table_mode="no_match")
    r = agent.command("/explain What is the allowable stress for ASTM A572?")
    titles = [t.title for t in r.tables]
    assert titles == ["Retrieved chunks", "Checks"]
    chunks, checks = r.tables
    assert {row[0] for row in chunks.rows} <= {"vector", "keyword"} and chunks.columns[:4] == ["method", "rank", "score", "final rank"]
    c = {row[0]: row[1] for row in checks.rows}
    assert c["codes found in no indexed text"] == "A572" and "no confident match" in c["table lookup"] and c["terms found in no table row"] == "A572"
    assert c["question is about the server itself"] == "no" and "hybrid" in c["retrieval"] and "allowable" in c["words searched by keyword"]
    assert "Usage" in agent.command("/explain").answer
    assert "/explain" in agent.command("/help").answer
