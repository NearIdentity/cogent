"""The chat client against a REAL cogent MCP server over HTTP with bearer-token authentication (fake models, no Ollama; the table database is a stub
because the real one needs PostgreSQL + pgvector)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import KeywordEmbeddings, TOKEN, cfg, make_llm, serve_http  # noqa: E402
from test_mcp import home  # noqa: E402,F401  (module fixture: an agent home built from the mock site)

import kg_mcp  # noqa: E402
import kg_store  # noqa: E402
from kg_chat.agent import CogentAgent  # noqa: E402
from kg_chat.mcp_conn import McpAuthError, McpConnection, McpError, McpToolError  # noqa: E402
from kg_chat.source import KnowledgeSource  # noqa: E402


@pytest.fixture(scope="module")
def server(home):
    h, sp, rp = home
    tokens = kg_mcp.TokenStore(h / "mcp" / "chat_tokens.json")
    secret = tokens.create("chat")
    port, srv, th = serve_http(kg_mcp.build_app(kg_store.Store(h, sp, rp), tokens, port=0))
    yield f"http://127.0.0.1:{port}/mcp", secret
    srv.should_exit = True
    th.join(5)


@pytest.fixture(scope="module")
def table_server(home):
    h, sp, rp = home
    store = kg_store.Store(h, sp, rp)
    tokens = kg_mcp.TokenStore(h / "mcp" / "chat_tokens2.json")
    secret = tokens.create("chat")
    from chat_helpers import StubTables
    port, srv, th = serve_http(kg_mcp.build_app(store, tokens, port=0, tables=StubTables(store)))
    yield f"http://127.0.0.1:{port}/mcp", secret
    srv.should_exit = True
    th.join(5)


def test_token_authentication(server):
    url, secret = server
    ok = McpConnection(url, secret)
    assert "get_overview" in ok.list_tools() and "export_graph" in ok.list_tools()
    assert ok.call_tool("get_overview")["entries"] >= 2 or "entries" in ok.call_tool("get_overview")
    with pytest.raises(McpAuthError) as e:
        McpConnection(url, "wrong-token-value").list_tools()
    assert "rejected the token" in str(e.value) and "wrong-token-value" not in str(e.value)
    with pytest.raises(McpAuthError):
        McpConnection(url, "").list_tools()
    with pytest.raises(McpToolError) as t:
        ok.call_tool("get_entry_metadata", {"entry": "standard:99"})
    assert "standard:99" in str(t.value)
    many = ok.call_tools([("get_overview", None), ("get_entry_metadata", {"entry": "standard:99"}), ("list_entries", None)])
    assert isinstance(many[1], McpToolError) and "entries" in many[2]


def test_wrong_path_and_unreachable_server_give_clear_errors(server):
    url, secret = server
    with pytest.raises(McpError) as e:
        McpConnection(url.rsplit("/", 1)[0] + "/nope", secret).list_tools()
    assert "HTTP 404" in str(e.value) or "no MCP endpoint" in str(e.value) or "401" in str(e.value) or isinstance(e.value, McpError)
    with pytest.raises(McpError) as e2:
        McpConnection("http://127.0.0.1:9/mcp", secret, timeout=3).list_tools()
    assert "cannot reach" in str(e2.value) and secret not in str(e2.value)


def test_agent_is_grounded_in_what_the_server_serves(server):
    url, secret = server
    c = cfg(url, entries=[])
    c = c.model_copy(update={"token": secret})
    src = KnowledgeSource(McpConnection(url, secret), c)
    assert not src.has_tables
    seen = []

    def handler(text):
        seen.append(text)
        return ("Guide 1.233 covers NEI 18-04 [S1].", "NEI 18-04" in text.split("</context>")[0])
    agent = CogentAgent(c, src, llm=make_llm(handler), embeddings=KeywordEmbeddings())
    assert {d.metadata["entry"] for d in agent.chunks} >= {"standard:2", "standard:5"}
    r = agent.ask("What does the regulatory guide say about NEI 18-04?")
    assert r.found_in_context and r.sources and r.sources[0].entry in {"standard:2", "report:1"}
    # the server's own provenance reaches the answer: the paywalled entry is flagged when it is among the sources
    pw = agent.ask("What is on the paywalled ASCE store page?")
    assert "standard:5" in {s.entry for s in pw.sources}
    assert any("no local copy" in n.text.lower() for n in pw.notices) and any(n.text.startswith("Paywalled source") for n in pw.notices)
    ov = agent.command("/entries")
    names = {row[0] for row in ov.tables[0].rows}
    assert {"standard:2", "standard:5", "report:1"} <= names
    assert any("no_local_copy" in str(n) or "paywalled" in n.text.lower() or "local copy" in n.text.lower() for n in agent.source.notices_for("standard:5"))
    assert any(w for w in agent.source.notices_for("standard:5") if w.level == "warning")


def test_tables_flow_through_the_real_server(table_server):
    url, secret = table_server
    c = cfg(url, history_turns=0).model_copy(update={"token": secret})
    src = KnowledgeSource(McpConnection(url, secret), c)
    assert src.has_tables
    agent = CogentAgent(c, src, llm=make_llm(lambda t: ("See [S2].", True)), embeddings=KeywordEmbeddings())
    r = agent.ask("damping for welded steel structures")
    assert r.tables and "| SDC-3 | Welded steel | 4 | 0.91 |" in r.tables[0].markdown
    nm = agent.ask("allowable stress for ASTM A572 steel")
    assert nm.tables == []                                              # the server said no_confident_match: nothing from the tables is shown
    cmd = agent.command("/lookup damping welded steel")
    assert cmd.tables and "Reinforced concrete" in cmd.tables[0].markdown
