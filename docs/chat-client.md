# Chat client: a conversational front-end for the cogent MCP server

`src/kg_chat/` (launcher `src/kg_chat_app.py`) is a web UI and a terminal client in which you ask questions about the standards and reports on a cogent MCP server and get answers that are grounded in what the server serves. It is adapted from the company-website Q&A agent in [NearIdentity/0xA6E07](https://github.com/NearIdentity/0xA6E07) and keeps its back-end design: **LangChain** retrieval, **Ollama** chat and embedding models, **Pydantic**-validated structured answers, an in-memory vector index and a **Flask** UI. What changed is the source of the documents: instead of crawled web pages, the knowledge graphs and tables of the MCP server, reached through a **bearer-token authenticated MCP connection**.

```
 cogent MCP server  (https://host/mcp, Authorization: Bearer <token>)
    │  list_entries, export_graph, get_entry_metadata, get_disclaimers      src/kg_chat/mcp_conn.py, source.py
    ▼
 Markdown sections per heading (tables as Markdown tables, citation metadata)
    │  split (MarkdownTextSplitter) → embed (Ollama) → in-memory vector store      src/kg_chat/agent.py, cache.py
    ▼
 question ─► [rewrite with chat history] ─► top-k chunks  +  lookup_values (table rows, if the server has a table database)
                                                 │
                              chat model (Ollama) → AgentAnswer (answer, found_in_context)
                                                 │
        reply = answer + sources (from retrieval metadata) + tables (Markdown, built in code) + notices (the server's caveats)
```

## Quick start

```bash
pip install -r requirements.txt                     # adds flask and langchain-text-splitters to the packages cogent already needs
ollama pull llama3.1 && ollama pull nomic-embed-text   # chat model (must support structured output) and embedding model
python src/kg_mcp.py --home kg_agent_home token create --name chat    # on the server host; prints the token once

export COGENT_MCP_TOKEN=...                         # or: --token-file path   (never a command-line argument)
python src/kg_chat_app.py web --mcp-url https://your-host/mcp          # http://127.0.0.1:5000
python src/kg_chat_app.py chat --mcp-url https://your-host/mcp         # the same agent in the terminal
```

`--mcp-url` (or `$COGENT_MCP_URL`) is the designated URL of the server; a bare `https://host` gets `/mcp` appended. Credentials or a query string in the URL are refused; plain `http` to another host prints a warning because the token would travel in clear text. At start-up the client connects, lists the tools (a wrong URL or token stops here with a clear message), reads every entry's graph, builds the index and then serves. Options: `--model`, `--embed-model`, `--ollama-url` (also `$CHAT_MODEL`, `$EMBED_MODEL`, `$OLLAMA_URL`), `--entries standard:22,report:1`, `--top-k`, `--table-rows`, `--cache-dir` (default `chat_cache/`, `none` disables), `--otlp-endpoint`. Web only: `--host`, `--port`, `--allow-remote`, `--metrics`.

## What it uses from the server

| Need | MCP tools |
|---|---|
| Text to answer from | `list_entries`, `export_graph` (every entry's graph: headings, paragraphs, tables, figure captions) |
| Numbers in tables | `lookup_values` on every question, when the server has a table database; `describe_table` for `/describe` |
| Caveats | `get_entry_metadata` (edition status, capture age, warnings, entry-specific disclaimers), `get_disclaimers`, `get_warnings` |
| Overview commands | `list_entries`, `get_warnings`, `lookup_values`, `describe_table` |

A server without a table database (no `lookup_values` tool) works: answers then rest on the graph text only. A table database that is down degrades the same way for that question.

## Grounding: the same rules as 0xA6E07

| 0xA6E07 (website agent) | This client (MCP-served knowledge) |
|---|---|
| Answer **only** from the retrieved page excerpts | Only from retrieved graph sections and table rows, each labelled `[S1]`, `[S2]`, ... in the prompt |
| `AgentAnswer(answer, found_in_context)` validated by Pydantic; "could not find it" when the context lacks the answer | The same model and rule; the prompt also says: if a table search reported no confident match, say the value was not found |
| Page text is untrusted: data, never instructions | Graph text and table cells are untrusted (they come from extracted documents); the prompt says so |
| Sources come from the retrieved chunks' metadata, shown only for a grounded answer | The same: entry, document, section, page and URL come from metadata; a citation mark `[S9]` that matches no retrieved excerpt is removed from the answer; nothing is shown for an answer the model could not ground |
| Follow-ups are rewritten into standalone questions; the last `history_turns` are kept | The same; per-session history, shared index |

Added for cogent, because its data is regulatory and carries its own caveats:

* **Tables are rendered in code, not by the model.** The rows of `lookup_values` (and of graph `Table` nodes) are turned into Markdown tables from the server's *original cell text* (`src/kg_chat/render.py`); the model is told to refer to a table by its label and never retype it, so the numbers on the screen are the numbers in the document. A table's title, document, page, units (with the server's `unit_notes`) and similarity are shown with it. When the server answers `no_confident_match`, no rows are shown and the model is told the value was not found.
* **The server's provenance is relayed by code.** For every entry an answer rests on, the interface shows what the server says: not the latest edition, stale capture, open warnings, and entry-specific disclaimers (paywalled source, OCR, failed fetch); paywalled table results also carry the copyright notice. The same points are given to the model under `<caveats>`. Provenance is reused for 5 minutes (`notice_ttl`).
* A rejected token is an error, not a degraded answer: the web app says "The MCP server rejected the token".

## Markdown display

The reply is JSON (`answer`, `found_in_context`, `sources`, `tables`, `notices`). The page renders the answer and each table's Markdown with a small renderer that builds DOM nodes with `textContent` only (headings, lists, bold/italic, code, block quotes, links to `http(s)` only, tables): nothing from a document or a table cell can inject HTML or script (tested in a real browser). The terminal client prints the same Markdown. Slash commands show server data without the language model: `/entries [text]`, `/warnings [entry]`, `/lookup <question>`, `/describe <table id>`, `/status`, `/help`.

## Index and caches

The index is built once at start-up (as in 0xA6E07) from every entry that has a graph. Graph sections are cached per entry under `--cache-dir` and reused while the entry's `graph_built` time is unchanged; embeddings are cached in SQLite by model and text hash, so a restart embeds only new text. Vectors live in memory: a very large server (tens of thousands of chunks) needs memory accordingly, and `--entries` limits what is indexed. An entry whose graph the server truncates (`max_nodes`) is reported under `/status`.

## Monitoring: combined LLM usage

The chat client reports its LLM calls with the same `cogent_llm_*` metrics as the daily agent and the MCP server, under `service="chat"`, plus `cogent_chat_*` metrics for questions and MCP calls. With `--metrics` (and `COGENT_CHAT_METRICS_TOKEN`) the web app serves them on `/metrics`; Prometheus scrapes them as a third job and the Grafana dashboard adds all three together. Details: [observability.md](observability.md#combined-llm-usage-agent-mcp-server-and-chat-client).

## Security

* The token is read from `$COGENT_MCP_TOKEN` or `--token-file`, held in a `SecretStr`, sent only in the `Authorization` header and removed from error messages. It is not a command-line option (it would show in the process list). Use a token of its own (`kg_mcp.py token create --name chat`) so it can be revoked separately; `$COGENT_MCP_TOKEN` is also the name of the server's own static-token variable, so on one machine the same value can serve both.
* **The web app has no login** (as in 0xA6E07) and holds the token: anyone who can reach it can read what the token can read. It binds to 127.0.0.1 and refuses another address unless you pass `--allow-remote`; then put authentication in front of it. `/metrics` needs `Authorization: Bearer $COGENT_CHAT_METRICS_TOKEN` (required on a non-loopback address).
* Answers are inserted as DOM text, only `http(s)` links are made clickable, and `/api/chat` accepts only `application/json` (basic CSRF protection). Private paths of purchased local copies (`file://...`) are shown as `local:<file name>`.

## Tests

`tests/test_chat_*.py` (46 tests): rendering of every table shape; grounding with fake models; the connection against a **real cogent MCP server over HTTP with a bearer token** (wrong, empty and missing token; wrong path; unreachable server) and tables through a stub table service; caches; the Flask app; the command line; the page in headless Chromium (Markdown tables rendered, HTML and `javascript:` links inert); LLM-usage and dashboard consistency. No Ollama is needed.

## Limits and what was not verified

* The live Ollama calls (`ChatOllama`, `OllamaEmbeddings`) were **not** run; the first real run checks that your chat model supports structured output (as in 0xA6E07's README). Token counts from Ollama (`usage_metadata`, `prompt_eval_count`) are used when present.
* Not run against the real table database (it needs PostgreSQL + pgvector): the table path is tested against a stub shaped like the server's answers. Not run against a large production server: indexing time and memory there are unmeasured.
* Retrieval is by embedding similarity over graph sections plus the server's table search; there is no keyword search over the graphs, so an exact identifier can be missed by the vectors.
* The page template `src/templates/index.html` that 0xA6E07's README and Dockerfile refer to is not in that repository (not committed), so this UI was written new following its documented behaviour.
* There is no container image or Kubernetes manifest for the chat client yet.
