The MCP server serves the knowledge graphs that the update agent has staged, plus the table database built from the tables in those documents, plus the metadata an agent needs to judge them. It never changes the agent home or the graphs. The only state it keeps is each session's saved preferences, in memory. Line numbers below refer to the current checkout.

## What it serves
- **Transport:** streamable HTTP at `/mcp` and an open `/healthz` (`src/kg_mcp.py`, `create_server` at `:184`, `build_app` at `:618`).
  - It is stateless by default (`--stateful` switches that off).
  - It runs behind your own HTTPS proxy or tunnel (`docs/mcp.md`, section 3).
- **Instructions to the client:** the server's built-in guidance tells an agent how to use the tools and to relay warnings (`INSTRUCTIONS`, `kg_mcp.py:42`). When a table database is configured, it adds `TABLE_INSTRUCTIONS` (`:52`): ask for numbers and row-type facts through `lookup_values`, then `query_table` or `describe_table`, and relay warnings, paywall flags, inferred-unit notes and the disclaimer.
- **Two back ends:**
  - the graph store (`src/kg_store.py`), which reads the staged files;
  - the table service (`src/kg_tableservice.py`), which queries PostgreSQL + pgvector. It is enabled with `--tabledb-dsn` (or `COGENT_TABLEDB_DSN`).

## Tools
24 tools are always registered (17 graph, 3 dictionary and map, 4 tailoring). A table database adds 7 more, and two optional flags add 2 further tools, for 33 at most. Most are read-only. The two that change a session's saved preferences are marked as non-destructive and idempotent.

### Graph tools (17, from the graph store)
| Group | Tools | Notes |
|---|---|---|
| **Orientation** | `get_overview`, `list_entries` | counts, version-status histogram, open warnings, last agent run |
| **Metadata** | `get_entry_metadata`, `get_warnings`, `get_disclaimers` | capture times, latest vs on-board edition, alerts, disclaimers |
| **Graph navigation** | `graph_summary`, `get_outline`, `search_graph`, `get_node`, `get_neighbors`, `get_subgraph`, `get_table`, `export_graph` | `ref` is an entry key such as `standard:22`, a 16-hex document id, or `combined` |
| **Cross-entry impact** | `get_dependents` | which reports depend on a standard and the reverse; curated vs inferred relations (data from `src/kg_combine.py`) |
| **Figures and tables** | `list_assets`, `get_figure`, `get_table_csv` | extracted figures (PNG) and tables (CSV); paywall watermark and policy apply |

### Table tools (need `--tabledb-dsn`: 7, plus 2 optional)
| Tool | Use |
|---|---|
| `list_tables`, `describe_table` | which tables exist; columns, types, units (as the session's unit mode sees them), sample rows |
| `search_tables` | which tables are about a question (table-level embeddings) |
| `lookup_values` | two-stage semantic lookup: best tables, then best rows (rows are embedded whole, numbers included). Returns the original cell text, document, page, caption, units and warnings |
| `query_table` | exact filters (`= != < <= > >= like in between is_null not_null`), numeric columns compared numerically |
| `join_tables` | join two tables along an inferred relationship, for the selected join approach |
| `explain_units` | every unit hypothesis for a column, its evidence, and what strict, broad and off would answer |
| `evaluate_joins` | only with `--llm-model`: runs the LLM join suggester and judge on request for one document and returns the evaluation |
| `run_sql` | only with `--allow-sql`: one read-only SELECT (keyword filter, read-only transaction, timeout; the database role is the real boundary) |

### Data dictionaries and relationship maps (3, from published files)
`get_data_dictionary`, `get_relationship_map` and `get_join_evaluation` serve what the table agent published under `<home>/tabledb/<docid>/`, in JSON, Markdown or Mermaid. They are registered even when no database is attached, and they honour the session's join approach and unit mode.

### Tailoring tools (4)
`list_profiles`, `get_session_preferences`, `set_session_preferences`, `reset_session_preferences`. See "Tailoring" below.

## Tailoring per session and per task
The database stores every join candidate and every unit hypothesis with its evidence, so a call chooses how to read them and nothing is recomputed.

| Setting | Values | Meaning |
|---|---|---|
| `approach` | `deterministic`, `llm`, `hybrid`, `consensus` | which join candidates count: heuristics only, LLM-suggested (not rejected by the judge), any candidate the judge did not reject (default), or proposed by both and accepted by the judge |
| `min_score` | 0 to 1 | score floor (default 0.55, which hides weakly supported LLM-only joins) |
| `include_rejected`, `min_agreement` | flag, 0 to 1 | show judge-rejected candidates, marked; demand agreement across LLM runs |
| `units` | `strict`, `broad`, `off` | narrow (explicit header, cell and declared units only; a conflict gives no unit), diverse (adds section text, KG entities, the document's dominant unit and the customary unit, flagged as inferred, with alternatives), or only what the header and cells say |
| `retrieval` | `vector`, `lexical`, `hybrid` | how `lookup_values` and `search_tables` rank (hybrid fuses the two) |

- **Profiles:** `fast`, `balanced` (default), `strict`, `exploratory`, `llm`; the server default is set with `--default-profile`.
- **Resolution order, last wins:** server default profile, then the session's saved preferences, then a `profile` in the call, then explicit arguments in the call (`approach`, `min_score`, `units` and so on).
- **Sessions:** keyed by the token or OAuth subject plus the client's `session_id` (or the MCP session id when stateful), held in memory for 8 hours.
- **Prompts:** `find_design_value`, `strict_design_lookup`, `explore_table_relationships` and `audit_units` set a posture for a task.

## Resources
- **Graph and catalog:** `kg://catalog`, `kg://disclaimers`, `kg://{standard|report}/{id}/metadata`, `kg://{standard|report}/{id}/graph`, `kg://combined/graph`, `kg://figure/{docid}/{asset}`, `kg://table/{docid}/{asset}`.
- **Tables:** `kg://tables/{docid}/dictionary`, `kg://tables/{docid}/relationships`, `kg://tables/{docid}/join-evaluation`.

## Metadata, provenance and warnings
- **Provenance:** every graph answer carries a `provenance` block with graph build time, when sources were last checked, age and a `stale` flag (`--stale-after`), `version_status` (current, update available, not staged and so on), latest vs on-board edition, open warnings and caveats.
- **Table answers** carry `warnings`, `paywalled`, the preferences used (approach, units, retrieval), inferred-unit notes, and a disclaimer that values are machine-extracted and must be checked against the source.
- **Disclaimers:** general ones plus situational ones for paywalled content, stale captures, OCR pages, failed fetches and not-the-latest editions (`src/kg_store.py`).
- **Combined graph:** derived by the agent (`src/kg_combine.py`); curated relations from your config are kept apart from relations inferred from text.

## How the table data is built
- **Agent:** `src/kg_tabledb.py` runs after the KG agent (`kg_agent.py run --tables`) or on its own. It loads each extracted table CSV into a typed PostgreSQL table with a whole-row `_embedding` column (same Ollama model and dimension everywhere) and a table-level embedding.
- **Joins:** deterministic scoring (names, value containment, types, KG relations) always runs. With `--llm-joins`, an Ollama suggester and an LLM judge add candidates and verdicts, with agreement and accept-rate metrics.
- **Units:** deduced from headers, cell suffixes, table captions and notes, section text and KG nodes, each with provenance. Nothing is converted.
- Setup and design trade-offs are in `docs/tabledb.md`.

## Scaling out
`--workers N` (one port, supervised processes), `--graph-cache N` (graphs per process, LRU), `--session-store memory|sqlite:<file>|redis://…` (session preferences shared by workers or hosts), `--embed-cache` / `--embed-concurrency` / `--embed-wait` (a busy embedder falls back to lexical search), `--rate-limit R` (requests per minute per credential, HTTP 429), `--metrics` (`/metrics`, needs a token), `--state-dir`. `kg_snapshot.py` ships the agent's results to other servers; `deploy/named-tunnel.sh` sets up a free named Cloudflare tunnel. See `docs/scaling.md`.

## Paywalled content
- **Detection:** a document counts as paywalled if it is a purchased local copy or comes from a publisher store host.
- **Figures:** re-rendered with a "REFERENCE ONLY © COPYRIGHTED" watermark and a copyright banner (`src/kg_assets.py`). They are never served as the original bytes.
- **Tables from the database:** carry `paywalled: true`, warnings and a copyright notice. `--paywalled-assets watermark|block|allow` sets the policy; `block` removes them from every result and disables `run_sql`.
- **Limit:** the older graph tools (`get_table`, `search_graph`, `export_graph`) do not watermark or block paywalled text.

## Authentication
- **Bearer tokens:** `TokenStore` (`kg_mcp.py:71`) keeps only hashes in a `0600` file, with expiry and revocation, plus one extra token from `COGENT_MCP_TOKEN`. `BearerAuth` (`:140`) checks them on every request and also accepts `X-API-Key`. The `token create`, `list` and `revoke` commands are in `main`.
- **OAuth 2.1 (optional, for claude.ai connectors):** turned on with `--oauth-issuer` (`src/kg_oauth.py`). It supports dynamic client registration, PKCE (S256), a consent page where you approve a client with one of your tokens, and refresh-token rotation. Static tokens still work, and revoking an approving token cuts off its OAuth tokens.
- **Hardening:** a Host-header allow-list (`--public-host`; `'*'` turns it off for random tunnel names), lockout after 5 wrong consent attempts, and `--no-auth` only on a loopback bind.
- **Database access:** connect the server with the SELECT-only role (`deploy/tabledb-readonly-role.sql`). Passwords come from the environment, not from the repo.

## Deployment helpers
- `deploy/Caddyfile`, `Caddyfile.docker`, `cogent-mcp.service` and `docker-compose.mcp.yml`.
- `deploy/docker-compose.tabledb.yml` with `deploy/tabledb.env.example` and `deploy/tabledb-init.sh` for PostgreSQL + pgvector.
- `deploy/demo-tunnel.sh` for a free Cloudflare quick tunnel, with `--oauth` for claude.ai. It does not pass the table-database flags; start the server by hand for those.
- `docs/mcp.md` has the remote-URL options and client setup for Claude Code, the Claude API and Claude Desktop; `docs/tabledb.md` covers the table database.

## Tests
`tests/test_mcp.py` (11 tests) covers store metadata from a real agent run, tokens, the 401/200 gate and Host check, a client round trip, the OAuth flow, and figure and table assets with the watermark and policies. The table features add `tests/test_mcp_tables.py` (9), `tests/test_mcp_tailoring.py` (12), `tests/test_units.py` (5) and `tests/test_llmjoin.py` (7). The table tests need PostgreSQL with pgvector and are skipped otherwise. The full suite is 153 tests.

## Not covered
- **Writing:** it never triggers agent runs or accepts uploads. Session preferences are the only thing a client can change, and they live in memory.
- **Staleness:** how fresh the data is depends on how often the daemon runs.
- **Untested against real services:** it hasn't been tried against claude.ai, the real Cloudflare service, or a real Ollama model. The LLM join stages were tested with scripted models and a mock Ollama server.
- **Judge is not ground truth:** the LLM judge's verdicts are evidence only.
- **Units:** labels with provenance, never converted; broad-mode units are guesses to be checked.
- **Unverified on real PDFs:** the figure and table assets are tested with hand-built files, not ones extracted from real PDFs.
