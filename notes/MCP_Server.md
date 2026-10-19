The MCP server is read-only: it serves the knowledge graphs that the update agent has staged, plus the metadata an agent needs to judge them. It never changes the agent home. Line numbers below refer to the current checkout.

## What it serves
- **Transport:** streamable HTTP at `/mcp` and an open `/healthz` (`src/kg_mcp.py:375`).
  - It is stateless by default (`--stateful` switches that off; `kg_mcp.py:186`).
  - It runs behind your own HTTPS proxy or tunnel (`docs/mcp.md`, section 3).
- **Instructions to the client:** the server's built-in guidance tells an agent how to use the tools and to relay warnings (`INSTRUCTIONS`, `kg_mcp.py:41`).

## Tools (17, all read-only)
The tool definitions are in `src/kg_mcp.py:211-326`, and the logic behind them is in `src/kg_store.py`.

| Group | Tools | Notes |
|---|---|---|
| **Orientation** | `get_overview`, `list_entries` | `Store.overview` (`kg_store.py:257`), `Store.catalog` (`:150`) |
| **Metadata** | `get_entry_metadata`, `get_warnings`, `get_disclaimers` | `Store.entry_metadata` (`:173`), `Store.alerts` (`:137`), `Store.disclaimers` (`:232`) |
| **Graph navigation** | `graph_summary`, `get_outline`, `search_graph`, `get_node`, `get_neighbors`, `get_subgraph`, `get_table`, `export_graph` | `kg_store.py:269-372` |
| **Cross-entry impact** | `get_dependents` | `Store.dependents` (`:394`), data built by `src/kg_combine.py` |
| **Figures and tables** | `list_assets`, `get_figure`, `get_table_csv` | `Store.list_assets` (`:474`), `Store.figure` (`:510`), `Store.table_csv` (`:523`) |

`ref` can be an entry key such as `standard:22`, a 16-hex document id, or `combined` for the derived entry-level graph (`Store._graph_path`, `kg_store.py:127`).

## Resources
Resources are defined in `kg_mcp.py:329-374`. They include `kg://catalog`, `kg://disclaimers`, `kg://{standard|report}/{id}/metadata` and `/graph`, `kg://combined/graph`, `kg://figure/{docid}/{asset}` and `kg://table/{docid}/{asset}`.

## Metadata and warnings
- **Provenance:** every graph answer carries a `provenance` block (`kg_mcp.py:190`) with:
  - graph build time and when the sources were last checked;
  - age and a `stale` flag (threshold set by `--stale-after`);
  - `version_status` (current, update available, not staged and so on), the latest available edition versus what is on board, and a plain-language summary;
  - open warnings and extra caveats.
- **Disclaimers:** the general ones are `GENERAL_DISCLAIMERS` (`kg_store.py:26`). Situational ones are added for paywalled content, stale captures, OCR pages, failed fetches and not-the-latest editions. Alert-specific notes come from `ALERT_NOTES` (`:35`).
- **Combined graph:** it is derived by the agent (`src/kg_combine.py`). Curated relations from your config are kept apart from relations inferred from text.

## Paywalled content
- **Detection:** a document counts as paywalled if it is a purchased local copy or comes from a publisher store host (`Store.doc_paywall`, `kg_store.py:443`).
- **Watermark:** figures from such documents are re-rendered with a "REFERENCE ONLY © COPYRIGHTED" watermark and a copyright banner (`src/kg_assets.py:37`). They are never served as the original bytes.
- **Warnings and policy:** responses carry warnings, and `--paywalled-assets watermark|block|allow` sets the policy (`kg_mcp.py:427`, enforced at `kg_store.py:484` and `:506`).
- **Limit:** the older graph tools (`get_table`, `search_graph`, `export_graph`) do not watermark or block paywalled text.

## Authentication
- **Bearer tokens:** `TokenStore` (`kg_mcp.py:61`) keeps only hashes in a `0600` file. It supports expiry and revocation, plus one extra token from `COGENT_MCP_TOKEN`. `BearerAuth` (`:130`) checks them on every request and also accepts `X-API-Key`. The `token create`, `list` and `revoke` commands are in `main` (`:410`).
- **OAuth 2.1 (optional, for claude.ai connectors):** `CogentOAuthProvider` (`src/kg_oauth.py:37`) is turned on with `--oauth-issuer`. It supports dynamic client registration (`:67`), PKCE (S256), a consent page where you approve a client with one of your tokens (`consent`, `:154`), and refresh-token rotation (`:112`). Static tokens still work, and revoking an approving token cuts off its OAuth tokens (`load_access_token`, `:121`).
- **Hardening:** there is a Host-header allow-list (`--public-host`, `kg_mcp.py:176-188`), lockout after 5 wrong consent attempts (`kg_oauth.py:30`), and `--no-auth` only on a loopback bind (`kg_mcp.py:460`).

## Deployment helpers
- `deploy/Caddyfile`, `Caddyfile.docker`, `cogent-mcp.service` and `docker-compose.mcp.yml`.
- `deploy/demo-tunnel.sh` for a free Cloudflare quick tunnel, with `--oauth` for claude.ai.
- `docs/mcp.md` has the remote-URL options and client setup for Claude Code, the Claude API and Claude Desktop.

## Tests
`tests/test_mcp.py` has 10 tests. They cover store metadata from a real agent run, tokens, the 401/200 gate and Host check, a client round trip, the OAuth flow, and figure and table assets with the watermark and policies.

## Not covered
- **Writing:** it never triggers agent runs or accepts uploads.
- **Staleness:** how fresh the data is depends on how often the daemon runs.
- **Untested against real services:** it hasn't been tried against claude.ai or the real Cloudflare service.
- **Unverified on real PDFs:** the figure and table assets are tested with hand-built files, not ones extracted from real PDFs.
