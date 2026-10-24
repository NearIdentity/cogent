# Scaling the MCP server

Nothing here was measured under real load. The corpus is small (tens of entries, a few hundred tables), so one modest machine serves many users; scale for
availability and a stable URL first, for throughput second. Prices and free-tier limits of third parties change: check them before relying on them.

## What scales, and how

| Piece | Behaviour | Scale-out |
|---|---|---|
| MCP server | read-only, stateless HTTP | `--workers N` (one port, N processes) or several instances behind a proxy |
| Graphs in memory | every process loads its own copy | `--graph-cache N` keeps the N most recently used graphs (default 64; 0 = no limit) |
| Session preferences | in memory, lost on restart | `--session-store sqlite:<file>` (workers of one host) or `redis://…` (several hosts) |
| Question embeddings (Ollama) | one call per `lookup_values` / `search_tables` | `--embed-cache` (recent vectors), `--embed-concurrency`, `--embed-wait`; when the embedder is saturated the search falls back to lexical matching and says so |
| Clients | one bad client can starve the rest | `--rate-limit R` requests/minute per credential (token bucket, `--rate-burst`), HTTP 429 with `Retry-After` |
| Observability | `/healthz` (open; pid, uptime, agent's last run) | `--metrics` adds `/metrics` (Prometheus text; needs a valid token) |
| Tokens / OAuth state | files in `<home>/mcp` (already safe for several processes on one host) | `--state-dir` moves them to shared storage |
| Agent | single writer, batch job | keep exactly one; publish its result as a **snapshot** for other hosts |

## A free, stable URL: a named Cloudflare tunnel

The quick tunnel (`demo-tunnel.sh`) gets a new random address every time. A **named tunnel** keeps one hostname, needs no open inbound port, and terminates TLS
at Cloudflare. It needs a free Cloudflare account and a domain whose DNS is on Cloudflare (the domain itself costs a few dollars a year).

1. Install `cloudflared` (see `docs/mcp.md`, "Demo with a Cloudflare quick tunnel", step 1).
2. One-time setup (opens a browser login, creates the tunnel, adds the DNS record, writes a config):
   ```bash
   deploy/named-tunnel.sh setup mcp.example.com            # tunnel name defaults to "cogent", port to 8765
   ```
3. Start the server with the **exact** public name (this keeps the Host-header check on, unlike the quick tunnel's `--public-host '*'`):
   ```bash
   python3 src/kg_mcp.py --home kg_agent_home serve --host 127.0.0.1 --port 8765 \
     --public-host mcp.example.com --oauth-issuer https://mcp.example.com \
     --workers 2 --session-store sqlite:kg_agent_home/mcp/sessions.db --rate-limit 600 --metrics
   ```
4. Run the tunnel: `deploy/named-tunnel.sh run`.
5. Check it from outside: `deploy/named-tunnel.sh check mcp.example.com <token>` (healthz 200, no token 401, MCP initialize 200).
6. Connect: `claude mcp add --transport http cogent-kg https://mcp.example.com/mcp --header "Authorization: Bearer <token>"`; in the Claude web app add
   `https://mcp.example.com/mcp` as a custom connector (the OAuth issuer above is the same URL).

### As services (survive reboots)
```bash
sudo mkdir -p /etc/cogent && sudo cp deploy/mcp.env.example /etc/cogent/mcp.env && sudo chmod 600 /etc/cogent/mcp.env   # edit: host name, read-only DSN
sudo cp ~/.cloudflared/cogent.yml /etc/cogent/cloudflared.yml
sudo cp ~/.cloudflared/<tunnel-uuid>.json /etc/cogent/cloudflared-credentials.json     # then set credentials-file in the copy to that path
sudo chown cogent: /etc/cogent/cloudflared* && sudo chmod 600 /etc/cogent/cloudflared*
sudo cp deploy/cogent-mcp.service deploy/cogent-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cogent-mcp cogent-tunnel
```
Restarting the server does not touch the tunnel or the URL. Cloudflare Access can add a login in front of the hostname, but browser-based logins do not suit
MCP clients (Claude Code, the web connector); keep the server's own tokens / OAuth for `/mcp` and consider Access only for `/metrics`.
The tunnel steps were written from Cloudflare's documented commands and could not be run against Cloudflare from the build environment.

## Several workers on one host
`--workers N` binds the port once and forks N processes that each serve on it; a worker that dies is restarted (up to 5 times a minute, then the supervisor
stops). Each worker holds its own graphs, so memory grows roughly N-fold: lower `--graph-cache` if needed. Use `--session-store sqlite:<file>` so a client
that reaches different workers keeps its preferences; the rate limit and metrics are per process. Windows has no `fork`: run instances on separate ports instead.

## Several hosts: snapshots
The agent writes; servers read. After a run the agent publishes a bundle, and each server pulls the newest one and switches to it atomically.
```bash
# on the agent host (also: kg_agent.py run --publish-snapshot DIR, so the daemon does it after every run)
python3 src/kg_snapshot.py publish --home kg_agent_home --dest /srv/cogent-snapshots --standards config/standards.json --reports config/reports.json
# on every server host
python3 src/kg_snapshot.py watch --src /srv/cogent-snapshots --link /srv/cogent/current --interval 300     # or an https:// base URL serving the same folder
python3 src/kg_mcp.py --home /srv/cogent/current serve --standards /srv/cogent/current/config/standards.json --reports /srv/cogent/current/config/reports.json ...
```
A bundle holds `staging/`, `tabledb/`, `state/alerts.json`, `logs/runs.jsonl` and the two configuration files; never `local/` (purchased copies), `archive/` or `mcp/`
(tokens). The pull verifies the SHA-256, refuses unsafe archive members, extracts beside the link and repoints the symlink with one rename; old snapshots are
pruned. The staged text of paywalled documents is inside the bundle (and the servers serve it): publish only to places where the server may run.
Tokens are separate per host unless `--state-dir` points to shared storage, and the table database must be reachable from every server (managed Postgres with
pgvector, or a replica).

## Sizing, in practice
1. Measure first: `--metrics` and a load generator (`hey`, `k6`) against `tools/call` on `lookup_values`, `search_graph`, `get_table`.
2. Watch memory per worker (graph count × size) and embedder latency; those are the first limits, not CPU on the database.
3. If Ollama is the bottleneck: raise `--embed-cache`, keep `--embed-concurrency` at the machine's real parallelism, or move Ollama to another host
   (`--ollama-host`). Changing the embedding **model** needs a rebuild of the table database.

## Hardening checklist
- Server on loopback only; only the tunnel (or Caddy) is public. `--no-auth` never on a public bind (the server refuses it).
- One token per client with an expiry; revoke on leave (`kg_mcp.py token revoke`). Read-only database login only; `--allow-sql` off.
- Do not expose PostgreSQL or Ollama. Keep `/etc/cogent/*` at mode 600.
- `--paywalled-assets block` when the audience is not covered by your licence for the purchased standards.
