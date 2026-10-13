# MCP server for the knowledge graphs

`src/kg_mcp.py` serves the graphs the update agent stages (`kg_agent_home/staging/...`) to AI agents over the
[Model Context Protocol](https://modelcontextprotocol.io), together with meta-data: capture times, **latest available vs.
on board** editions, open warnings and disclaimers. It is read-only and can run beside the daemon (the agent replaces
files atomically; graphs are re-read when their mtime changes).

Open-source parts: the official [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP, streamable
HTTP), `uvicorn`/Starlette, `networkx`; for the public URL Caddy, `cloudflared` or Tailscale.

## 1. Steps (what was built and why)

1. **`src/kg_store.py`** – read-only backend. Loads node-link graphs with networkx (cached by mtime), joins them with
   `meta.json`, per-document `state.json`, `state/alerts.json`, `logs/runs.jsonl` and the `version_tracking` blocks in
   `config/*.json`, and builds the meta-data and disclaimers (paywall, stale capture, OCR pages, not-latest edition…).
2. **`src/kg_mcp.py`** – FastMCP server (stateless streamable HTTP at `/mcp`, `/healthz` open) with 13 read-only tools
   and `kg://` resources; a bearer-token ASGI middleware; Host-header allow-list (DNS-rebinding protection); token CLI.
3. **Provenance on every answer.** Graph tools return `{"provenance": {...}, ...}`: `graph_built`, `sources_last_checked`,
   `age_days`, `stale`, `version_status` (`current | update_available | not_staged | partially_staged | no_local_copy`),
   `version_summary`, `latest_available`, `warnings`, `caveats`, `disclaimer`. The server `instructions` tell the agent
   to relay these to the user.
4. **Remote URL** – the server binds to loopback; a proxy/tunnel adds TLS and the public name (section 3).
5. **Auth** – section 4: bearer tokens, and optional OAuth 2.1 (`src/kg_oauth.py`).

### Tools

| Tool | Purpose |
|---|---|
| `get_overview` | counts, version-status histogram, open warnings, last agent run, disclaimers |
| `list_entries(kind?, query?)` | standards/reports with graph availability, build time, version status |
| `get_entry_metadata(entry)` | per-document capture times, latest vs on-board editions, warnings, disclaimers |
| `get_dependents(entry)` | reports that depend on a standard / standards a report depends on (curated vs inferred), and what is affected if it changes |
| `get_warnings(entry?)`, `get_disclaimers(entry?)` | alerts / applicable disclaimers |
| `graph_summary`, `get_outline`, `search_graph`, `get_node`, `get_neighbors`, `get_subgraph`, `get_table`, `export_graph` | navigate a graph (`ref` = `standard:22`, `report:3` or a document id) |

Graph tools also accept `ref="combined"` (the derived entry-level graph). Resources: `kg://combined/graph`, `kg://catalog`, `kg://disclaimers`, `kg://{standard|report}/{id}/metadata`, `kg://{standard|report}/{id}/graph`.

## 2. Run it locally

```bash
pip install -r requirements.txt            # includes mcp<2, uvicorn
python src/kg_mcp.py token create --name claude      # prints the secret ONCE
python src/kg_mcp.py --home kg_agent_home serve --port 8765
curl localhost:8765/healthz
```
Env equivalents: `COGENT_HOME`, `COGENT_STANDARDS`, `COGENT_REPORTS`, `COGENT_MCP_TOKEN` (one extra token).
Inspect interactively: `npx @modelcontextprotocol/inspector` → Streamable HTTP, URL `http://localhost:8765/mcp`,
header `Authorization: Bearer <token>`.

## 3. Make it reachable at a remote URL

Always keep `--host 127.0.0.1` and put TLS in front. Pass the public name with `--public-host` (otherwise the Host check
returns 421).

| Option | How | Notes |
|---|---|---|
| **Caddy** (own VM/domain) | `deploy/Caddyfile`; `kg.example.com { reverse_proxy 127.0.0.1:8765 }` | automatic Let's Encrypt |
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://127.0.0.1:8765` (quick, random URL) or a named tunnel | no open inbound port |
| **Tailscale Funnel** | `tailscale funnel 8765` | stable `*.ts.net` URL; or use plain tailnet for private-only access |
| **Docker** | `deploy/docker-compose.mcp.yml` (server + Caddy) | |
| ngrok | `ngrok http 8765` | handy for tests |

Run as a service: `deploy/cogent-mcp.service` (systemd; token via `EnvironmentFile`).
Example: `python src/kg_mcp.py --home kg_agent_home serve --public-host kg.example.com` → URL `https://kg.example.com/mcp`.

## 4. Authentication — is there a simple way?

Yes: **static bearer tokens**, implemented here in ~60 lines (`BearerAuth`, `TokenStore`):

```bash
python src/kg_mcp.py token create --name alice --days 90   # one per client; expiry optional
python src/kg_mcp.py token list
python src/kg_mcp.py token revoke --name alice             # effective immediately, no restart
```
Tokens are 256-bit random (`cgt_…`); only SHA-256 hashes are stored (`<home>/mcp/tokens.json`, mode 0600); comparison is
constant-time; `Authorization: Bearer …` and `X-API-Key` are accepted; failures return `401` + `WWW-Authenticate`. Always
use HTTPS, one token per client, rotate by create → switch → revoke.

### OAuth 2.1 (for claude.ai web/mobile custom connectors and other OAuth clients)

Anthropic's connector docs describe OAuth (dynamic client registration) as the supported auth for claude.ai custom
connectors, so the server can also act as its own small OAuth authorization server (`src/kg_oauth.py`):

```bash
python src/kg_mcp.py --home kg_agent_home serve --public-host kg.example.com --oauth-issuer https://kg.example.com
```
* Endpoints (from the MCP SDK): `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`,
  `/register` (dynamic client registration), `/authorize`, `/token`, `/revoke`; authorization code + **PKCE (S256)**.
* The one human step is the **consent page** (`/consent`): the operator approves the client by entering a token made with
  `token create`. After 5 wrong entries from one IP the page locks for 5 minutes.
* Access tokens last 1 h, refresh tokens 30 d (rotated on use); only hashes are stored in `<home>/mcp/oauth.json` (0600).
  Revoking the operator token that approved a client immediately invalidates that client's OAuth tokens.
* Static bearer tokens keep working on `/mcp` (Claude Code, API connector, `mcp-remote`); `X-API-Key` is not accepted
  in OAuth mode. The issuer must be the public **https** URL (http only for localhost).
* In claude.ai: Settings → Connectors → Add custom connector → `https://kg.example.com/mcp` → sign in on the consent page.
  Connector behaviour changes; check <https://claude.com/docs/connectors/building/authentication>.
* Limits: single operator identity (tokens, not user accounts); registration is open by design but harmless without a
  token. For per-user identity/SSO put an OIDC gateway (Cloudflare Access, Authentik, Keycloak…) in front instead.

## 5. Connecting agents

Replace `https://kg.example.com/mcp` and `$TOKEN`.

**Claude Code**
```bash
claude mcp add --transport http cogent-kg https://kg.example.com/mcp --header "Authorization: Bearer $TOKEN"
claude mcp list        # then in a session: /mcp
```

**Claude API (MCP connector, beta `mcp-client-2025-11-20`)**
```python
import anthropic
client = anthropic.Anthropic()
resp = client.beta.messages.create(
    model="claude-opus-5-5", max_tokens=2048, betas=["mcp-client-2025-11-20"],
    mcp_servers=[{"type": "url", "url": "https://kg.example.com/mcp", "name": "cogent-kg", "authorization_token": TOKEN}],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "cogent-kg"}],
    messages=[{"role": "user", "content": "Is the staged copy of NRC RG 1.233 the latest edition? Cite capture times."}])
```
The URL must be public HTTPS (Anthropic's servers connect to it).

**Claude Desktop** (`claude_desktop_config.json`, via the `mcp-remote` bridge)
```json
{"mcpServers": {"cogent-kg": {"command": "npx",
  "args": ["-y", "mcp-remote", "https://kg.example.com/mcp", "--header", "Authorization:${AUTH}"],
  "env": {"AUTH": "Bearer <token>"}}}}
```
**Claude.ai web/mobile** – Settings → Connectors → Add custom connector → URL; start the server with `--oauth-issuer` (section 4).

**Other agents** – any MCP client with streamable HTTP (Cursor, VS Code, OpenAI Agents SDK, LangChain
`langchain-mcp-adapters`, …): URL + `Authorization: Bearer` header. Python example:
```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async with streamablehttp_client(URL, headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize(); print(await s.call_tool("get_overview", {}))
```

## 6. Security notes
Read-only tools; refs are validated (no path traversal); body size limited by the SDK; Host-header allow-list; rate
limiting and IP allow-lists belong in the proxy (Caddy `rate_limit` plugin, Cloudflare). Content is third-party text:
clients should treat it as data, not instructions. Paywalled standards are served only from copies you bought — check
your licence before exposing them remotely.
