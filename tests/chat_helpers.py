"""Shared helpers of the chat-client tests: fake models (no Ollama), a fake MCP connection, and a real cogent MCP server with a fake table service."""
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from langchain_core.embeddings import Embeddings  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402

from kg_chat.models import AgentAnswer, ClientConfig  # noqa: E402

TOKEN = "t0k3n-for-tests-only"


class KeywordEmbeddings(Embeddings):
    """Bag-of-words vectors over a fixed vocabulary: deterministic similarity without Ollama."""
    WORDS = ["founded", "seismic", "damping", "welded", "steel", "category", "edition", "regulatory", "guide", "paywalled", "asce", "nei", "1999",
             "anvils", "pricing", "store", "limit", "state", "probability"]

    def __init__(self):
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += len(texts)
        return [[float(w in t.lower()) + 0.01 for w in self.WORDS] for t in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


class FakeLLM(GenericFakeChatModel):
    """Structured answers from handler(prompt_text) -> (answer, found) or None (= the model broke the schema); records every prompt.
    ``ctx`` > 0 makes it behave like Ollama: it reports the prompt tokens it was given, capped at the window."""
    handler: object = None
    prompts: list = []
    ctx: int = 0
    cpt: float = 3.0               # characters per token the fake model's tokenizer produces (a denser tokenizer than the client's estimate overflows the window)

    def with_structured_output(self, schema, include_raw=False, **kw):
        def respond(prompt_value):
            text = prompt_value.to_string()
            self.prompts.append(text)
            out = self.handler(text)
            parsed = AgentAnswer(answer=out[0], found_in_context=out[1]) if out is not None else None
            if not include_raw:
                return parsed
            tokens = min(int(len(text) / self.cpt), self.ctx) if self.ctx else 0
            raw = AIMessage(content="", usage_metadata={"input_tokens": tokens, "output_tokens": 5, "total_tokens": tokens + 5})
            return {"raw": raw, "parsed": parsed, "parsing_error": None if parsed else ValueError("invalid JSON from the model")}
        return RunnableLambda(respond)


def make_llm(handler, rewrite="standalone question about seismic damping", ctx=0, cpt=3.0):
    return FakeLLM(messages=iter([AIMessage(content=rewrite)] * 200), handler=handler, prompts=[], ctx=ctx, cpt=cpt)


def cfg(url="http://127.0.0.1:1/mcp", **kw):
    return ClientConfig(mcp_url=url, token=TOKEN, **kw)


class FakeConn:
    """Stands in for McpConnection: tool name -> callable(args) -> dict. Counts the calls."""

    def __init__(self, tools):
        self.tools, self.calls = tools, []

    def with_timeout(self, t):
        return self

    def list_tools(self):
        return set(self.tools)

    def list_tool_details(self):
        return [{"name": n, "description": "", "read_only": n not in ("set_session_preferences", "reset_session_preferences")} for n in sorted(self.tools)]

    def call_tool(self, name, args=None):
        self.calls.append((name, args or {}))
        out = self.tools[name](args or {})
        if isinstance(out, Exception):
            raise out
        return out


def node(id, type, label="", **kw):
    return {"id": id, "type": type, "label": label, "text": kw.pop("text", label), **kw}


def sample_graph():
    """An entry graph shaped like the server's export_graph: per-document prefixed ids, headings, paragraphs, a table, entities."""
    t = "ab12cd34"
    nodes = [node("entry:standard:9", "Standard", "Seismic Design Criteria"), node(f"{t}:doc:a.html", "Document", "Seismic Design Criteria"),
             node(f"{t}:h0001", "Heading", "Seismic Design Criteria", level="1"),
             node(f"{t}:h0002", "Heading", "3 Seismic Categories", level="2"),
             node(f"{t}:p0003", "Paragraph", "Table 3-1 lists the seismic design categories and their limit state probability."),
             node(f"{t}:tbl0004", "Table", "Table 3-1: Seismic design categories",
                  rows='[["Seismic Category", "Limit State", "Annual Probability (per year)"], ["SDC-3", "Limited permanent distortion", "4.0E-4"], ["SDC-4", "A|B", "1.0E-4"]]', page="12"),
             node(f"{t}:h0006", "Heading", "4 Damping", level="2"),
             node(f"{t}:p0007", "Paragraph", "Damping values for welded steel structures are given in Table 4-1."),
             node("ent:sdc3", "TableRow", "SDC-3")]
    return {"directed": True, "multigraph": True, "graph": {"entry": "standard:9", "sources": [{"tag": t, "source": "https://www.example.org/docs/a.html"}]},
            "nodes": nodes, "edges": [], "truncated": False, "total_nodes": len(nodes)}


ENTRY = {"entry": "standard:9", "kind": "standard", "id": 9, "name": "Seismic Design Criteria", "has_graph": True, "graph_built": "2026-11-01T00:00:00Z",
         "paywalled": False, "version_status": "current", "open_warnings": 0}

META = {"entry": "standard:9", "name": "Seismic Design Criteria", "paywalled": False,
        "version": {"status": "update_available", "summary": "A newer edition is available than the one on board (latest: Rev. 2)."},
        "capture": {"stale": True, "age_days": 41}, "warnings": [{"message": "standard:9: a new edition is on sale", "severity": "warning"}],
        "disclaimers": ["general one", "Stale capture: sources were last checked 41 days ago (threshold 30).", "On-board content is NOT the latest available: x"]}


# shaped like the get_overview answer of a real cogent server (trimmed)
OVERVIEW = {"entries": 45, "with_graph": 45, "version_status_counts": {"current": 33, "partially_staged": 1, "no_local_copy": 2, "update_available": 1, "no_documents": 8},
            "open_warnings": 19, "agent_last_run": "2026-11-12T06:24:17Z", "entries_behind_extraction_recipe": ["standard:20"],
            "agent_last_run_summary": {"documents": 54, "docs_created": 0, "docs_updated": 0, "docs_unchanged": 40, "docs_error": 10, "docs_deferred": 4},
            "document_errors": [{"url": "https://www.ecfr.gov/current/title-10/chapter-I/part-50", "error": "HTTP 406 Not Acceptable"},
                                {"url": "https://www.nrc.gov/docs/ML1801/ML18019A922.pdf", "error": "HTTP 403 Forbidden"}],
            "table_database": {"configured": True, "reachable": True, "user": "cogent_ro", "built": True, "can_select": True, "tables": 623}}


def lookup_rows(paywalled=False):
    tbl = {"caption": "Table 4-1: Damping values by seismic category", "paywalled": paywalled,
           "source": {"document_id": "ab12cd34ab12cd34", "document": "Seismic Design Criteria", "url": "https://www.example.org/docs/a.html", "page": 12,
                      "caption": "Table 4-1: Damping values by seismic category", "section": "4 Damping"}}
    res = {"question": "q", "mode": "semantic", "match_quality": "semantic", "tables_searched": [],
           "rows": [{"table_id": "ab12cd34ab12cd34/t0008", "row": 2, "similarity": 0.91, "table": tbl, "units": {"Damping (%)": "%"},
                     "values": {"Seismic Category": "SDC-3", "Structure Type": "Welded steel", "Damping (%)": "4"}},
                    {"table_id": "ab12cd34ab12cd34/t0008", "row": 3, "similarity": 0.55, "table": tbl, "units": {"Damping (%)": "%"},
                     "values": {"Seismic Category": "SDC-4", "Structure Type": "Reinforced concrete", "Damping (%)": "7"}}],
           "provenance": {"disclaimer": "d"}}
    if paywalled:
        res.update(paywalled=True, copyright_notice="Copyrighted material of ACME: reference only.", warnings=["standard:9: no local copy on board"])
    return res


def base_tools(table_mode=None):
    """Tool functions of a fake server; table_mode: None (no table tools) | 'rows' | 'no_match' | 'paywalled' | 'down'."""
    t = {"list_entries": lambda a: {"entries": [ENTRY]}, "get_overview": lambda a: OVERVIEW,
         "get_disclaimers": lambda a: {"disclaimers": ["general one"]}, "get_entry_metadata": lambda a: META,
         "export_graph": lambda a: {"provenance": {}, "graph": sample_graph()}, "get_warnings": lambda a: {"warnings": [{"entry": "standard:9", "severity": "warning", "message": "a new edition is on sale"}]}}
    if table_mode:
        from kg_chat.mcp_conn import McpToolError

        def lookup(a):
            if table_mode == "down":
                return McpToolError("table database not reachable: connection refused")
            if table_mode == "no_match":
                return {"no_confident_match": True, "message": "no table row mentions 'A572'", "rows": [], "match_quality": "none", "missing_terms": ["A572"]}
            return lookup_rows(paywalled=table_mode == "paywalled")
        t["lookup_values"] = lookup
        t["describe_table"] = lambda a: {"table": {"table_id": a["table_id"], "caption": "Table 4-1", "source": {"document": "Seismic Design Criteria", "page": 12},
                                                   "columns": [{"name": "damping", "header": "Damping (%)", "pg_type": "numeric", "unit": "%", "n_distinct": 5, "min": 2, "max": 7}],
                                                   "sample_rows": [{"Seismic Category": "SDC-3", "Damping (%)": "4"}]}}
    return t


class StubTables:
    """A table service for kg_mcp.create_server: canned answers shaped like the real ones (the real one needs PostgreSQL + pgvector)."""

    def __new__(cls, store, mode="rows"):
        import kg_tableservice as TS

        class Impl(TS.TableService):
            has_db = property(lambda self: True)

            def health(self):
                return {"configured": True, "reachable": True}

            def lookup_values(self, question, ref=None, table_ids=None, k=10, candidate_tables=5, prefs=None):
                if "A572" in question:
                    return {"no_confident_match": True, "message": "no table row mentions A572", "rows": [], "match_quality": "none"}
                return lookup_rows(paywalled=mode == "paywalled")

        return Impl(store, None)


def serve_http(app):
    import uvicorn
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return port, srv, th
