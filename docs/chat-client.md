# Chat client: a conversational front-end for the cogent MCP server

`src/kg_chat/` (launcher `src/kg_chat_app.py`) is a web UI and a terminal client in which you ask questions about the standards and reports on a cogent MCP server and get answers that are grounded in what the server serves. It is adapted from the company-website Q&A agent in [NearIdentity/0xA6E07](https://github.com/NearIdentity/0xA6E07) and keeps its back-end design: **LangChain** retrieval, **Ollama** chat and embedding models, **Pydantic**-validated structured answers, an in-memory vector index and a **Flask** UI. What changed is the source of the documents: instead of crawled web pages, the knowledge graphs and tables of the MCP server, reached through a **bearer-token authenticated MCP connection**.

```
 cogent MCP server  (https://host/mcp, Authorization: Bearer <token>)
    │  list_entries, export_graph, get_entry_metadata, get_disclaimers      src/kg_chat/mcp_conn.py, source.py
    ▼
 Markdown sections per heading (tables as Markdown tables, citation metadata)
    │  split (MarkdownTextSplitter) → embed (Ollama) → in-memory vector store      src/kg_chat/agent.py, cache.py
    ▼
 question ─► [rewrite with chat history] ─► top-k chunks: vector search + BM25 keyword search, fused  +  lookup_values (table rows, if the server has a table database)
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
cp .env.example .env && chmod 600 .env              # then edit CHAT_USERNAME and CHAT_PASSWORD (the login of the web app; .env is git-ignored)
python src/kg_chat_app.py web --mcp-url https://your-host/mcp          # http://127.0.0.1:5000, asks for the login
python src/kg_chat_app.py chat --mcp-url https://your-host/mcp         # the same agent in the terminal
```

`--mcp-url` (or `$COGENT_MCP_URL`) is the designated URL of the server; a bare `https://host` gets `/mcp` appended. Credentials or a query string in the URL are refused; plain `http` to another host prints a warning because the token would travel in clear text. At start-up the client connects, lists the tools (a wrong URL or token stops here with a clear message), reads every entry's graph, builds the index and then serves. Options: `--model`, `--embed-model`, `--ollama-url` (also `$CHAT_MODEL`, `$EMBED_MODEL`, `$OLLAMA_URL`), `--entries standard:22,report:1`, `--retrieval hybrid|semantic|keyword`, `--num-ctx` (default 8192, also `$CHAT_NUM_CTX`), `--top-k`, `--table-rows`, `--cache-dir` (default `chat_cache/`, `none` disables), `--otlp-endpoint`. Web only: `--host`, `--port`, `--allow-remote`, `--env-file`, `--no-login`, `--secure-cookie`, `--session-hours`, `--metrics`.

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

The reply is JSON (`answer`, `found_in_context`, `sources`, `tables`, `notices`). The page renders the answer and each table's Markdown with a small renderer that builds DOM nodes with `textContent` only (headings, lists, bold/italic, code, block quotes, links to `http(s)` only, tables): nothing from a document or a table cell can inject HTML or script (tested in a real browser). The terminal client prints the same Markdown. Slash commands show server data without the language model: `/explain <question>` (what retrieval does), `/entries [text]`, `/warnings [entry]`, `/lookup <question>`, `/describe <table id>`, `/status`, `/help`.

## Why an answer says "not found": what the agent can and cannot see

A "not found" has four possible causes, and the client now tells them apart instead of leaving you to guess:

| Cause | How the client shows it | What to do |
|---|---|---|
| The server does not hold it | the note "No indexed text mentions A572; the table search found no row mentioning A572. The server does not appear to hold it" (the codes of the question that occur in no indexed chunk, and the server's own `missing_terms`) | stage the document, or accept the answer; `/entries` lists what is staged |
| The question is about the server, not a document ("What is staged, and is the database read-only?") | answered from the server's own state: `get_overview` (entries, version status, last agent run, table database reachable / login / tables), `list_entries` and `list_tools` with their read-only annotations, labelled "MCP server" in the sources; used when the question mentions staged / database / read-only / tools / entries / warnings / last run ... | none; before this, such questions could never be answered because that state is not in any graph |
| The text is there but retrieval missed it, or the prompt was cut | `/explain <question>` (no model call): the chunks found by vector and by keyword, their scores and final rank, the codes found in no text, the table lookup result, and how much of the context window the excerpts use | `--retrieval`, `--top-k`, `--num-ctx` |
| The model was too cautious (or broke the schema) | a note that the code does appear in the knowledge graphs and where; a schema failure is retried once and then reported as an error, not as "not found" | a larger model; see below |

Two facts about the data that the retrieval cannot fix: text from scanned pages is only as good as the OCR (on the test server the purchased ASCE 43-05 copy contains run-together words such as `allowablestressesshallbescaledtostrength-based`; the keyword search now also matches inside such text, but vectors and the server's own `search_graph` do not), and a document the agent could not fetch (403) is not in any graph.

## What the tests cannot tell you: the language model

The unit tests use fake models, and tests that read the server's data (as I did with `lookup_values` and `search_graph` while diagnosing the examples above) show the data side only. **How `llama3.1:8b` or any other model behaves inside this agent can only be measured with the real model**, and some limits matter a lot for this design:

* **Context window.** The prompt is instructions + caveats + history + question + up to 6 text chunks + table rows (+ the server's state for questions about it). Ollama's default window is small (2048 to 4096 tokens depending on version) and Ollama cuts the START of a longer prompt, which is where the instructions and the best excerpts are, so the model answers "not found". The client therefore sends `num_ctx` explicitly (`--num-ctx`, default 8192; set `OLLAMA_CONTEXT_LENGTH` on the server to match), **trims the context to fit** (priority: the server's state, table rows, then chunks in rank order; the last item that partly fits is cut), reports what was left out, and checks Ollama's own `prompt_eval_count`: a count at the window means the prompt was probably cut, which is shown as a warning, counted in `cogent_chat_context_overflow_total` and fails the evaluation run. The estimate (3 characters per token) is cautious for prose; a model whose tokenizer is denser can still overflow, which is what the check is for.
* **Structured output.** The answer must be valid JSON for `AgentAnswer`. A small model can break it; the client retries once and then raises an error (HTTP 502 with a message), it does not turn a broken answer into "not found".
* **Calibration of `found_in_context`.** Small models tend to say "not found" even when the excerpt is there, and sometimes the reverse. The client cannot know which; it adds the note where the question's codes do appear in the data, and `/explain` shows the evidence.
* **Citation habits, latency, long tables, units.** Whether the model cites `[S1]` sensibly, how long an answer takes with six chunks, and whether it copies numbers faithfully are model properties (the interface never relies on the model for the numbers: tables are rendered in code).

**Measure it on your stack:** `python src/kg_chat_app.py eval --mcp-url https://host/mcp --questions chat_eval/questions.json --report chat_eval/report.md` asks each question of the real MCP server and the real Ollama models and reports per question: pass / FAIL against the expectations written beside it, `found_in_context`, the prompt tokens against the window, excerpts left out, sources, tables and seconds; it exits 1 if anything fails. `chat_eval/questions.json` ships with the two prompts reported as failing ("What is staged, and is the database read-only?", "What is the allowable stress for ASTM A572? Quote the source.") and three more; **edit it** to facts that are certainly in your staged documents. Re-run it after changing the model, `--num-ctx`, `--top-k` or `--retrieval`. A model that fails *found* on questions whose evidence `/explain` shows retrieved is too timid for this task: try a larger one (candidates to measure, not recommendations I have verified: `qwen2.5:14b` or `qwen2.5:32b` instruct models, `llama3.1:70b` if the GPU allows; each needs structured-output support and a larger `--num-ctx` than its default). This run was **not** executed here: there is no Ollama in the build environment.

## Retrieval: vectors and keywords

Embeddings find passages that mean the same, but they blur exact codes: `NEI 18-04` and `NEI 21-07`, `A572` and `A516`, `50.69` and `50.59` look alike to a vector. So every question is answered from **two rankings over the same chunks, fused**:

* **vector search**: the embedding similarity of the (rewritten) question, as in 0xA6E07;
* **keyword search**: Okapi **BM25** over the chunk texts and their section headings, with an identifier-aware tokenizer that works like the server's own keyword search: case-insensitive, `-`, `/` and `_` count as spaces (`DOE-STD-1104` finds `DOE STD 1104`), a dotted number stays one token (`1.233`, `50.69`), and stop words are dropped from the question (not from the index);
* **fusion**: reciprocal rank fusion (constant 60). A question that contains a code (a token with a digit) gives the keyword ranking double weight, so a passage that is the only one to mention `A572` is not outvoted by passages that are merely close in meaning to the rest of the question.

`--retrieval hybrid` (default) | `semantic` | `keyword` chooses the method for text. The table search is asked for hybrid retrieval too (`lookup_values` with `retrieval="hybrid"`; config `table_retrieval`), so exact codes in table rows are matched as well. A code that no chunk contains finds nothing by keyword: the answer then rests on the vectors alone, and if they have nothing the model says it could not find it. `cogent_chat_retrieval_hits_total{source}` shows how many chunks came from each method.

## Index and caches

The index is built once at start-up (as in 0xA6E07) from every entry that has a graph. Graph sections are cached per entry under `--cache-dir` and reused while the entry's `graph_built` time is unchanged; embeddings are cached in SQLite by model and text hash, so a restart embeds only new text. Vectors live in memory: a very large server (tens of thousands of chunks) needs memory accordingly, and `--entries` limits what is indexed. An entry whose graph the server truncates (`max_nodes`) is reported under `/status`.

## Monitoring: combined LLM usage

The chat client reports its LLM calls with the same `cogent_llm_*` metrics as the daily agent and the MCP server, under `service="chat"`, plus `cogent_chat_*` metrics for questions and MCP calls. With `--metrics` (and `COGENT_CHAT_METRICS_TOKEN`) the web app serves them on `/metrics`; Prometheus scrapes them as a third job and the Grafana dashboard adds all three together. Details: [observability.md](observability.md#combined-llm-usage-agent-mcp-server-and-chat-client).

## Security

* The token is read from `$COGENT_MCP_TOKEN` or `--token-file`, held in a `SecretStr`, sent only in the `Authorization` header and removed from error messages. It is not a command-line option (it would show in the process list). Use a token of its own (`kg_mcp.py token create --name chat`) so it can be revoked separately; `$COGENT_MCP_TOKEN` is also the name of the server's own static-token variable, so on one machine the same value can serve both.
* **The web app asks for a login** (0xA6E07's app had none) because it holds the MCP token: anyone who can reach it could read what the token can read. One user, whose credentials are in a **`.env` file on the server** (`CHAT_USERNAME` and `CHAT_PASSWORD`, or `CHAT_PASSWORD_HASH` made with `python src/kg_chat_app.py hash-password`, which is preferable to a plaintext password; optional `FLASK_SECRET_KEY` keeps sessions valid across restarts). `.env` is in `.gitignore`; `.env.example` is the template. The app refuses to start without credentials (or `--no-login`, which is allowed on 127.0.0.1 only), warns when the file is readable by other users or the password is shorter than 10 characters, and never logs or echoes the password.
* **How the login works:** a signed session cookie (`HttpOnly`, `SameSite=Lax`, `Secure` with `--secure-cookie`, valid `--session-hours`, default 12); the form carries a CSRF token and the session is renewed on login; the user name and password are compared in constant time and a failure never says which was wrong; after 4 failed attempts an address is locked out (30 s, doubling to 15 min), counted in `cogent_chat_logins_total`; logout (a CSRF-checked POST) ends the session and clears the conversation. The page and every `/api/*` call need the login (`/healthz` stays open); `/metrics` has its own bearer token and is not behind the login. Pages are sent with `Cache-Control: no-store`, `X-Frame-Options: DENY` and no referrer.
* **Serve it over HTTPS when other machines can reach it:** over plain http the password travels in clear text. The app binds to 127.0.0.1 and refuses another address unless you pass `--allow-remote` (it then warns unless `--secure-cookie` is set); put a TLS reverse proxy in front. Behind a proxy every client has the proxy's address, so the lock-out counts them together; give the proxy its own rate limit too. The lock-out is in memory and per process. `/metrics` needs `Authorization: Bearer $COGENT_CHAT_METRICS_TOKEN` (required on a non-loopback address).
* Answers are inserted as DOM text, only `http(s)` links are made clickable, and `/api/chat` accepts only `application/json` (basic CSRF protection). Private paths of purchased local copies (`file://...`) are shown as `local:<file name>`.

## Tests

`tests/test_chat_*.py` (92 tests): rendering of every table shape; grounding with fake models; the connection against a **real cogent MCP server over HTTP with a bearer token** (wrong, empty and missing token; wrong path; unreachable server) and tables through a stub table service; caches; the Flask app; the login (`.env` parsing, plain and hashed passwords, CSRF, lock-out, session cookie flags, logout, headers, no password in logs; in a real browser); the command line; the page in headless Chromium (Markdown tables rendered, HTML and `javascript:` links inert); LLM-usage and dashboard consistency. No Ollama is needed.

## Limits and what was not verified

* The live Ollama calls (`ChatOllama`, `OllamaEmbeddings`) were **not** run: see "What the tests cannot tell you" above for what that leaves open and the `eval` command that measures it on your stack; the first real run checks that your chat model supports structured output (as in 0xA6E07's README). Token counts from Ollama (`usage_metadata`, `prompt_eval_count`) are used when present.
* Not run against the real table database (it needs PostgreSQL + pgvector): the table path is tested against a stub shaped like the server's answers. Not run against a large production server: indexing time and memory there are unmeasured.
* Keyword search is BM25 over the chunks the client itself indexed (the same text the vectors see), not the server's `search_graph`: it needs no extra calls, but it only knows chunks of the entries indexed at start-up, and it has no stemming or synonyms (`seismic` does not find `earthquake`; that is the vector search's job). The fusion constants (reciprocal rank fusion 60, keyword weight 2 for questions that contain a code) are untuned guesses. Tested with fake embeddings that cannot find the code; not measured on real questions.
* The page template `src/templates/index.html` that 0xA6E07's README and Dockerfile refer to is not in that repository (not committed), so this UI was written new following its documented behaviour.
* There is no container image or Kubernetes manifest for the chat client yet.
