"""Flask app of the chat client (adapted from 0xA6E07's tests), the command-line helpers, and the page itself in a real browser."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import FakeConn, KeywordEmbeddings, TOKEN, base_tools, cfg, make_llm  # noqa: E402

from kg_chat import cli  # noqa: E402
from kg_chat.agent import CogentAgent  # noqa: E402
from kg_chat.models import ChatReply, Notice, SourceRef, TableView  # noqa: E402
from kg_chat.source import KnowledgeSource  # noqa: E402
from kg_chat.web import MAX_MESSAGE_CHARS, create_app  # noqa: E402


def make_agent(handler=None, table_mode="rows"):
    c = cfg(history_turns=2)
    src = KnowledgeSource(FakeConn(base_tools(table_mode)), c)
    handler = handler or (lambda t: ("Welded steel damping is in [S1].", "damping" in t.rsplit("Question:", 1)[-1].lower()))
    return CogentAgent(c, src, llm=make_llm(handler), embeddings=KeywordEmbeddings())


@pytest.fixture()
def client():
    app = create_app(make_agent(), title="host.test", disclaimer="Extraction can contain errors.", metrics=True, metrics_token="mtok")
    app.config["TESTING"] = True
    return app.test_client()


def test_index_status_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and b"host.test" in r.data and b"Extraction can contain errors." in r.data and b"table search on" in r.data
    assert client.get("/healthz").get_json()["status"] == "ok"
    st = client.get("/api/status").get_json()
    assert st["table_tools"] is True and st["chunks"] > 0 and TOKEN not in json.dumps(st)


def test_chat_returns_answer_sources_tables_and_notices(client):
    body = client.post("/api/chat", json={"message": "damping for welded steel?"}).get_json()
    assert body["found_in_context"] is True and "[S1]" in body["answer"]
    assert body["sources"][0]["entry"] == "standard:9" and body["sources"][0]["url"] == "https://www.example.org/docs/a.html"
    assert body["tables"] and "| SDC-3 | Welded steel | 4 | 0.91 |" in body["tables"][0]["markdown"]
    assert any(n["level"] == "warning" for n in body["notices"])
    nf = client.post("/api/chat", json={"message": "who won the match"}).get_json()
    assert nf["found_in_context"] is False and nf["sources"] == [] and nf["tables"] == []


def test_commands_go_through_the_same_endpoint(client):
    body = client.post("/api/chat", json={"message": "/entries"}).get_json()
    assert "| standard:9 |" in body["tables"][0]["markdown"]


def test_rejects_bad_input(client):
    assert client.post("/api/chat", json={}).status_code == 400
    assert client.post("/api/chat", json={"message": 5}).status_code == 400
    assert client.post("/api/chat", json={"message": "   "}).status_code == 400
    assert client.post("/api/chat", json={"message": "x" * (MAX_MESSAGE_CHARS + 1)}).status_code == 413
    assert client.post("/api/chat", data="message=hi", content_type="application/x-www-form-urlencoded").status_code == 400      # CSRF guard


def test_history_is_per_session_and_resettable():
    agent = make_agent()
    app = create_app(agent)
    app.config["TESTING"] = True
    a, b = app.test_client(), app.test_client()
    a.post("/api/chat", json={"message": "damping welded steel"})
    a.post("/api/chat", json={"message": "and for concrete?"})
    assert agent.history == []                                                   # the shared base agent is never mutated
    assert b.post("/api/chat", json={"message": "damping"}).status_code == 200
    assert a.post("/api/reset").get_json() == {"ok": True}


def test_failures_return_502_without_details():
    def boom(t):
        raise ConnectionError("model server down: secret-detail")
    client = create_app(make_agent(boom)).test_client()
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 502 and "secret-detail" not in r.get_data(as_text=True)
    from kg_chat.mcp_conn import McpAuthError
    tools = base_tools()
    tools["get_entry_metadata"] = lambda a: McpAuthError("rejected")
    c = cfg()
    agent = CogentAgent(c, KnowledgeSource(FakeConn(tools), c), llm=make_llm(lambda t: ("ok", True)), embeddings=KeywordEmbeddings())
    agent.source.conn.tools["get_entry_metadata"] = lambda a: McpAuthError("rejected")
    r = create_app(agent).test_client().post("/api/chat", json={"message": "damping"})
    assert r.status_code == 502 and "rejected the token" in r.get_json()["error"]


def test_metrics_endpoint_is_opt_in_and_token_protected(client):
    client.post("/api/chat", json={"message": "damping welded steel"})
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer mtok"})
    assert ok.status_code == 200 and b"cogent_chat_questions_total" in ok.data and b"cogent_chat_conversations" in ok.data
    off = create_app(make_agent()).test_client()
    assert off.get("/metrics").status_code == 404


# ---------------------------------------------------------------------------- configuration and the command line

def test_the_token_never_appears_in_a_repr_or_dump_and_is_not_a_command_line_option():
    c = cfg()
    assert TOKEN not in repr(c) and TOKEN not in c.model_dump_json() and TOKEN not in str(c.model_dump())
    help_text = __import__("subprocess").run([sys.executable, str(Path(cli.__file__).resolve().parents[1] / "kg_chat_app.py"), "web", "--help"], capture_output=True, text=True).stdout
    assert "--token-file" in help_text and "--token " not in help_text and "--mcp-url" in help_text


def test_url_validation_and_token_sources(tmp_path, monkeypatch):
    from pydantic import ValidationError
    assert cfg("https://host.example").mcp_url == "https://host.example/mcp" and cfg("host.example/mcp/").mcp_url == "https://host.example/mcp"
    for bad in ("ftp://x/mcp", "https://user:pw@host/mcp", "https://host/mcp?token=abc", ""):
        with pytest.raises(ValidationError):
            cfg(bad)
    assert cfg("http://10.1.2.3:8765/mcp").insecure_remote and not cfg("http://127.0.0.1:8765/mcp").insecure_remote and not cfg("https://x/mcp").insecure_remote
    with pytest.raises(ValidationError):
        cfg().model_copy(update={}).__class__(mcp_url="https://x/mcp", token="  ")
    f = tmp_path / "tok"
    f.write_text("  from-file \n")
    monkeypatch.setenv("COGENT_MCP_TOKEN", "from-env")
    assert cli.read_token(f) == "from-file" and cli.read_token(None) == "from-env"
    monkeypatch.delenv("COGENT_MCP_TOKEN")
    with pytest.raises(SystemExit) as e:
        cli.read_token(None)
    assert "COGENT_MCP_TOKEN" in str(e.value)
    with pytest.raises(SystemExit):
        cli.read_token(tmp_path / "missing")


def test_web_refuses_a_remote_bind_without_the_flag(monkeypatch):
    monkeypatch.setenv("COGENT_MCP_TOKEN", "x")
    with pytest.raises(SystemExit) as e:
        cli.main(["web", "--mcp-url", "https://h/mcp", "--host", "0.0.0.0"])
    assert "--allow-remote" in str(e.value)
    with pytest.raises(SystemExit) as e2:
        cli.main(["web", "--mcp-url", "https://h/mcp", "--host", "0.0.0.0", "--allow-remote", "--metrics"])
    assert "COGENT_CHAT_METRICS_TOKEN" in str(e2.value)


def test_terminal_format_keeps_tables_as_markdown():
    r = ChatReply(answer="See the table.", found_in_context=True, tables=[TableView(title="T", markdown="**T**\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")],
                  notices=[Notice(level="warning", text="not the latest edition")], sources=[SourceRef(label="S1", entry_name="N", section="Sec", url="https://x.test/d")])
    out = cli.format_reply(r)
    assert "| a | b |" in out and "WARNING: not the latest edition" in out and "[S1] N — Sec  https://x.test/d" in out


# ---------------------------------------------------------------------------- the page, in a real browser

class PageAgent:
    """A stand-in agent whose reply tries to inject HTML and script through every field."""
    class _S:
        has_tables = True
        problems: list = []
    source = _S()
    chunks = [1, 2, 3]

    class cfg:
        chat_model, embed_model = "m", "e"
    history: list = []

    def command(self, text):
        return None

    def ask(self, text):
        evil = "<img src=x onerror=\"window.__pwned=1\"><script>window.__pwned=1</script>"
        md = "| Seismic Category | Damping (%) |\n| --- | --- |\n| SDC-3 | 4 |\n| A\\|B | " + evil + " |"
        return ChatReply(answer="**Bold** and *italic* `code` [ok](https://example.org/x) [bad](javascript:window.__pwned=1) " + evil + "\n\n- one\n- two",
                         found_in_context=True,
                         tables=[TableView(title="Table 4-1", markdown="**Table 4\\-1**\n*Doc, p. 12*\n\n" + md + "\n\n> Units: Damping (%) → %")],
                         notices=[Notice(level="warning", text="Not the latest edition " + evil)],
                         sources=[SourceRef(label="S1", entry_name="Entry", section="Sec", url="https://example.org/doc"), SourceRef(label="S2", entry_name="Bad", url="javascript:window.__pwned=1")])


def test_page_renders_markdown_tables_and_cannot_be_injected():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    app = create_app(PageAgent(), title="host.test")
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            import os
            exe = os.environ.get("KG_BROWSER_PATH") or ("/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None)
            try:
                browser = pw.chromium.launch(args=["--no-sandbox"], executable_path=exe)
            except Exception as exc:                                              # no browser in this environment
                pytest.skip(f"no browser: {str(exc)[:80]}")
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{srv.server_port}/")
            page.fill("#q", "damping?")
            page.click("#send")
            page.wait_for_selector(".msg.bot table")
            rows = page.eval_on_selector_all(".msg.bot table tr", "trs => trs.map(tr => Array.from(tr.children).map(c => c.textContent))")
            assert rows[0] == ["Seismic Category", "Damping (%)"] and rows[1] == ["SDC-3", "4"] and rows[2][0] == "A|B" and "<script>" in rows[2][1]    # shown as text
            assert page.evaluate("window.__pwned") is None                           # nothing from the reply executed
            assert page.eval_on_selector_all(".msg.bot img, .msg.bot script", "e => e.length") == 0
            hrefs = page.eval_on_selector_all(".msg.bot a", "a => a.map(x => x.href)")
            assert "https://example.org/x" in hrefs and "https://example.org/doc" in hrefs and not any(h.startswith("javascript:") for h in hrefs)
            assert page.inner_text(".msg.bot strong") == "Bold" and page.inner_text(".msg.bot em") == "italic" and page.eval_on_selector(".msg.bot li", "e => e.textContent") == "one"
            assert page.inner_text(".msg.bot blockquote").startswith("Units: Damping (%)")
            assert "Not the latest edition" in page.inner_text(".note.warning") and "[S1]" in page.inner_text(".sources")
            browser.close()
    finally:
        srv.shutdown()
