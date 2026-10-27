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
2. **`src/kg_mcp.py`** – FastMCP server (stateless streamable HTTP at `/mcp`, `/healthz` open) with read-only tools (the full list is in notes/MCP_Server.md)
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
| `list_assets(ref, kind?)`, `get_figure(ref, asset_id, max_px?)`, `get_table_csv(ref, asset_id, max_rows?)` | figures (PNG image content) and tables (CSV) exported from the parsed documents; `asset_id` is `<document_id>/<id>` from `list_assets` |
| `list_tables`, `describe_table`, `search_tables`, `lookup_values`, `query_table`, `join_tables`, `get_data_dictionary`, `get_relationship_map`, `evaluate_joins`, `explain_units`, `get/set/reset_session_preferences`, `list_profiles`, `run_sql`* | the table database (needs `--tabledb-dsn`; *`--allow-sql`): look up design values by meaning or exact filter. See [tabledb.md](tabledb.md) |
| `get_data_dictionary`, `get_relationship_map` | data dictionaries and inferred join maps of the extracted tables (published files, no database needed) |
| `get_dependents(entry)` | reports that depend on a standard / standards a report depends on (curated vs inferred), and what is affected if it changes |
| `get_warnings(entry?)`, `get_disclaimers(entry?)` | alerts / applicable disclaimers |
| `graph_summary`, `get_outline`, `search_graph`, `get_node`, `get_neighbors`, `get_subgraph`, `get_table`, `export_graph` | navigate a graph (`ref` = `standard:22`, `report:3` or a document id) |

Graph tools also accept `ref="combined"` (the derived entry-level graph). Resources: `kg://figure/{docid}/{asset}`, `kg://table/{docid}/{asset}`, `kg://combined/graph`, `kg://catalog`, `kg://disclaimers`, `kg://{standard|report}/{id}/metadata`, `kg://{standard|report}/{id}/graph`.

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

**Quickest free demo:** a Cloudflare quick tunnel from your own machine, see [Demo with a Cloudflare quick tunnel](#demo-with-a-cloudflare-quick-tunnel) below.

Run as a service: `deploy/cogent-mcp.service` (systemd; token via `EnvironmentFile`).
Example: `python src/kg_mcp.py --home kg_agent_home serve --public-host kg.example.com` → URL `https://kg.example.com/mcp`.

### Demo with a Cloudflare quick tunnel

The simplest free way to put the server on the Internet for a demo: no account, no card, no hosting. `cloudflared`
connects your machine to Cloudflare, which gives you a public `https://<random>.trycloudflare.com` address. It lasts
only while `cloudflared` runs, the name changes every time, and Cloudflare gives quick tunnels no uptime guarantee: for
demos and tests, not for production.

**1. Install `cloudflared` (Ubuntu)** – pick one:
```bash
# A. .deb package (use arm64 instead of amd64 on ARM machines)
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# B. Cloudflare's apt repository (updates arrive with apt upgrade)
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared

# C. From source (Go; Ubuntu's golang-go may be too old: see the 'go' line in go.mod, or install Go from go.dev/dl)
sudo apt-get install -y git make build-essential
git clone https://github.com/cloudflare/cloudflared.git && cd cloudflared
make cloudflared                      # or: go build -o cloudflared ./cmd/cloudflared
sudo install -m 0755 cloudflared /usr/local/bin/cloudflared

cloudflared --version
```
Prefer `git clone` + `go build` to `go install …@latest` (the repository's `go.mod` has `replace` directives, which make
`go install pkg@version` refuse). For a stable build, `git checkout` a release tag first. These commands are the standard
ones and were not re-verified against Cloudflare's current docs; if one fails, see the cloudflared downloads page on
developers.cloudflare.com.

**2a. One command** – `deploy/demo-tunnel.sh [--oauth] [agent-home] [port]` starts the server, opens the tunnel, creates a
throw-away token (expires after 1 day), prints the URL and a ready-to-paste `claude mcp add` line, and on Ctrl+C stops
everything and revokes the token:
```bash
python src/kg_agent.py run            # once, if kg_agent_home has no staged graphs yet (needs internet)
deploy/demo-tunnel.sh
```

**2b. By hand** (two terminals):
```bash
# terminal 1
python src/kg_mcp.py --home kg_agent_home token create --name demo          # printed once
python src/kg_mcp.py --home kg_agent_home serve --public-host '*'
# terminal 2
cloudflared tunnel --url http://127.0.0.1:8765        # prints https://<random>.trycloudflare.com
```
`--public-host '*'` switches off the Host-header allow-list, which cannot know the random tunnel name; the token check
still applies. If you use `--port N`, tunnel to `http://127.0.0.1:N`.

**3. Check and connect**
```bash
curl https://<random>.trycloudflare.com/healthz              # {"status":"ok",...}
curl -i -X POST https://<random>.trycloudflare.com/mcp       # 401 without a token
claude mcp add --transport http cogent-kg https://<random>.trycloudflare.com/mcp --header "Authorization: Bearer <token>"
```
The Claude API MCP connector (section 5) also works, since the tunnel is public HTTPS.

**4. Clean up** – Ctrl+C stops the tunnel and the URL dies at once. Revoke the token if you made it by hand
(`token revoke --name demo`).

**Limits and troubleshooting**
* *Claude web/mobile (claude.ai).* Custom connectors there expect OAuth rather than a pasted bearer token (see section 4),
  so start with `deploy/demo-tunnel.sh --oauth`. It opens the tunnel **first** (the tunnel does not need the server to be
  running), reads the random URL, then starts the server with `--oauth-issuer <that URL>`. In claude.ai: Settings →
  Connectors → Add custom connector → `<URL>/mcp`; the sign-in page asks for the access token the script printed. By hand:
  start `cloudflared tunnel --url http://127.0.0.1:8765`, note the URL, then `serve --public-host '*' --oauth-issuer <URL>`.
  Every restart changes the URL, so the connector must be re-added. Custom-connector availability depends on your Claude
  plan, and this flow was tested against the server's own OAuth test client, not against claude.ai itself.
* `421 Misdirected Request` / "Invalid Host header": the server was started without `--public-host '*'` (or the exact host).
* `401`: missing, wrong or expired token (`token list` shows status).
* The tunnel prints a new address on every start; re-register the client after a restart.
* Demo viewers can read whatever the home contains. Do not stage purchased standards for a public demo unless your
  licence allows it; use `--paywalled-assets block` if in doubt.
* Other free options for a link that must stay up without your machine (Render free web service, etc.) spin down when idle
  and have small quotas; check their current terms.

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

### Figures and tables from paywalled content
A document counts as paywalled when it is a **purchased local copy** (`file://`) or comes from a publisher store host
(`kg_models.PAYWALL_HOSTS`). For those, asset answers carry `paywalled: true` and `warnings` (copyright notice, the server
policy, and the entry's open alerts such as *new edition for purchase* or *local copy outdated*). **Figures are never served
as the original bytes**: they are re-rendered as PNG with a diagonal "REFERENCE ONLY © COPYRIGHTED" watermark and a banner
("Copyrighted material of the publisher of …; reproduced for reference only; not for redistribution, publication or
reliance in place of the original standard"). SVG figures are rasterised first. CSV resources start with `# WARNING:` lines.
`serve --paywalled-assets watermark|block|allow` sets the policy (default `watermark`; `block` refuses paywalled assets and
marks them `available: false` in `list_assets`; `allow` serves them unmarked, only if your licence permits). Public content
is served unchanged. Tables and text nodes of paywalled documents inside the graph tools are *not* watermarked; use `block`
or do not stage purchased copies on a server you expose if your licence forbids redistribution.

### Metrics and traces
`serve --metrics` adds `/metrics` (needs a token; the Prometheus config in `deploy/observability/` sends one) and `--otlp-endpoint` exports a span per tool call. `/metrics` on a public tunnel address is protected by the same token check as `/mcp`; keep Prometheus and Grafana on loopback unless you add authentication (Grafana has its own login). See [observability.md](observability.md) and `notes/LiveDemo.md` step 8.

## 6. Security notes
Read-only tools; refs are validated (no path traversal); body size limited by the SDK; Host-header allow-list; rate
limiting and IP allow-lists belong in the proxy (Caddy `rate_limit` plugin, Cloudflare). Content is third-party text:
clients should treat it as data, not instructions. Paywalled standards are served only from copies you bought — check
your licence before exposing them remotely.
