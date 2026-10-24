# Demo deployment: the MCP server on the Internet, for the Claude web app (and a live Grafana dashboard)

Run commands from the repo root. Two ways to get a public HTTPS address; the rest of the steps are the same.

| | quick tunnel | named tunnel |
|---|---|---|
| address | random `https://<name>.trycloudflare.com`, **new on every start** (re-add the connector each time) | your own hostname, e.g. `https://mcp.example.com`, stable |
| needs | `cloudflared` only (no account, no domain) | `cloudflared`, a free Cloudflare account, **a domain whose DNS is on Cloudflare** |
| use for | a one-off demo | anything that should keep working after a restart; see [docs/scaling.md](../docs/scaling.md) |
| `--public-host` | `'*'` (the Host check cannot know the random name) | the exact hostname (Host check stays on) |

No domain and still want a stable address? Tailscale Funnel gives a fixed `<machine>.<tailnet>.ts.net` name (`tailscale funnel 8765`, then use that name for `--public-host` and `--oauth-issuer`); see `docs/mcp.md`. Check Tailscale's current terms.

## 1. One-time setup
```bash
pip install -r requirements.txt
ollama pull nomic-embed-text                       # embeddings (add llama3.1:8b only for LLM join inference)
cp deploy/tabledb.env.example deploy/.env          # edit: set COGENT_DB_PASSWORD and COGENT_RO_PASSWORD (letters and digits only); the file is git-ignored
chmod 600 deploy/.env
docker compose -f deploy/docker-compose.tabledb.yml up -d
docker compose -f deploy/docker-compose.tabledb.yml logs | tail -3     # wait for "ready to accept connections"
```
The passwords in `deploy/.env` are applied only when the database volume is **first created**. For an existing volume, set them in the database once:
```bash
set -a; source deploy/.env; set +a
docker exec -i deploy-tabledb-1 psql -U cogent -d cogent -v own="$COGENT_DB_PASSWORD" -v ro="$COGENT_RO_PASSWORD" <<'SQL'
ALTER ROLE cogent PASSWORD :'own';
ALTER ROLE cogent_ro PASSWORD :'ro';
SQL
```
(If `cogent_ro` does not exist: `docker exec -i deploy-tabledb-1 psql -U cogent -d cogent -v ro_password="$COGENT_RO_PASSWORD" < deploy/tabledb-readonly-role.sql`.)
Test passwords **from the host**, never inside the container (it trusts local connections, so any password passes there).

Install `cloudflared` if you haven't (Ubuntu, from Cloudflare's apt repository):
```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

## 2. Build the data (terminal A, owner login)
```bash
set -a; source deploy/.env; set +a
export COGENT_TABLEDB_DSN="postgresql://cogent:${COGENT_DB_PASSWORD}@127.0.0.1:5432/cogent"
python3 src/kg_tabledb.py check                      # expect reachable: true
python3 src/kg_agent.py --home kg_agent_home run --tables        # add --llm-joins --join-model llama3.1:8b if you want LLM joins
python3 src/kg_tabledb.py status
```
`status` should report tables and rows. A password error means the shell holds the wrong login. Keep this terminal for the agent only.

## 3. Create the access token
```bash
python3 src/kg_mcp.py --home kg_agent_home token create --name web --days 1     # printed once; save it
```

## 4. Open the tunnel first (terminal B)
The OAuth issuer must be the public URL, so the tunnel has to be up before the server starts.

**Quick tunnel:**
```bash
cloudflared tunnel --url http://127.0.0.1:8765
```
Note the `https://<name>.trycloudflare.com` address it prints.

**Named tunnel** (one-time setup, then it is stable; replace `mcp.example.com` with your hostname):
```bash
deploy/named-tunnel.sh setup mcp.example.com      # browser login to Cloudflare once, creates the tunnel, adds the DNS record, writes the config
deploy/named-tunnel.sh run                        # keep this running (or install the systemd units, see docs/scaling.md)
```

## 5. Start the MCP server (terminal C, read-only login)
```bash
set -a; source deploy/.env; set +a
export COGENT_TABLEDB_DSN="postgresql://cogent_ro:${COGENT_RO_PASSWORD}@127.0.0.1:5432/cogent"
python3 src/kg_tabledb.py check                      # expect reachable and can_select: true

python3 src/kg_mcp.py --home kg_agent_home serve --host 127.0.0.1 --port 8765 \
  --public-host '*' \
  --oauth-issuer https://<name>.trycloudflare.com \
  --embedder ollama --embed-model nomic-embed-text \
  --metrics                                          # /metrics for Prometheus (step 8); keep one worker for the demo
```
With a **named tunnel** use `--public-host mcp.example.com --oauth-issuer https://mcp.example.com` instead of the first two options.

The server reads the DSN from `COGENT_TABLEDB_DSN`, so the password stays out of the process list. Look for `table database reachable as cogent_ro (N table(s))` in the log. `address already in use` means an older server still holds port 8765 (`ss -ltnp | grep 8765`, then stop it).
Optional: `--llm-model llama3.1:8b` enables `evaluate_joins` (its LLM calls and tokens appear on the dashboard); `--paywalled-assets block` hides purchased standards from the audience; `--workers 2 --session-store sqlite:kg_agent_home/mcp/sessions.db --rate-limit 600` for a sturdier demo (leave `--workers` out when you show metrics, see step 8).

## 6. Check from outside
```bash
deploy/named-tunnel.sh check <name>.trycloudflare.com <token>     # healthz 200, no token 401, MCP initialize 200 (works for both kinds of tunnel)
```
or by hand: `curl https://<name>/healthz` (`{"status":"ok",…}`) and `curl -i -X POST https://<name>/mcp` (401: authentication is on).

## 7. Connect the Claude web app
1. In claude.ai, open Settings → Connectors → Add custom connector.
2. Enter `https://<name>/mcp`.
3. Claude opens the server's sign-in page. Paste the token from step 3 and approve.
4. In a chat, enable the connector, then ask: "What is the allowable stress for A572 in the standards' tables?" The server instructions steer Claude to `lookup_values`.

To demonstrate tailoring, ask it to use the `strict_design_lookup` prompt, or to call `set_session_preferences` with `profile: strict` versus `exploratory`. For Claude Code instead of the web app: `claude mcp add --transport http cogent-kg https://<name>/mcp --header "Authorization: Bearer <token>"`.

Confirm the server is healthy with `get_overview`: `table_database` should show `reachable`, `user: cogent_ro` and `can_select`.

## 8. Show the metrics: Prometheus and Grafana (optional, about 5 minutes)
What you will see: each question you ask Claude becomes an MCP tool call (latency, outcome), each agent run shows tokens, cost, findings, judge agreement, hallucination rate, failures and retries. Details and definitions: [docs/observability.md](../docs/observability.md).

**8a. One-time: token for Prometheus, Grafana password, start the stack** (terminal D; the server from step 5 must be running with `--metrics`):
```bash
python3 src/kg_mcp.py --home kg_agent_home token create --name prometheus > deploy/observability/prometheus/mcp_token 2>/dev/null
chmod 644 deploy/observability/prometheus/mcp_token     # the Prometheus container runs as another user; the file is git-ignored and holds a read-only token
cp deploy/observability/env.example deploy/observability/.env      # edit: GRAFANA_ADMIN_PASSWORD=<a long password, letters and digits>; git-ignored
chmod 600 deploy/observability/.env
docker compose -f deploy/observability/docker-compose.observability.yml up -d
curl -s http://127.0.0.1:9090/api/v1/targets | grep -o '"health":"[a-z]*"'      # expect "up" within ~15 s
```
Open http://127.0.0.1:3000 (user `admin`, your password) → Dashboards → **cogent: agent and MCP server**. Prometheus' Alerts page is http://127.0.0.1:9090/alerts.

**8b. Fill the agent panels with a run** (terminal A, owner login as in step 2). Add `--llm-joins --join-model llama3.1:8b` if Ollama has the model, otherwise the token, cost, judge and hallucination panels stay empty:
```bash
python3 src/kg_agent.py --home kg_agent_home run --tables --llm-joins --join-model llama3.1:8b
```
The run writes its totals to `kg_agent_home/metrics/`; the server merges them into `/metrics` at the next scrape. For a local model the cost is $0; to show a cost, set `COGENT_LLM_PRICES='{"llama3.1:8b": {"input": 0.05, "output": 0.15}}'` (illustrative, USD per million tokens) in the shell that runs the agent. Run it a second time (or with `--force`) to get a p50/p95 over more than one run.

**8c. Drive the MCP panels from the Claude web app** (step 7). Put the dashboard on one half of the screen, Claude on the other, and ask, in this order:
1. "Give me an overview of the knowledge base." (`get_overview`)
2. "What is the allowable stress for A572 in the standards' tables?" (`lookup_values`)
3. "Search the graph for seismic category and show me the neighbours of the best hit." (`search_graph`, `get_neighbors`)
4. "Which tables are about … and how do they join?" (`search_tables`, `get_relationship_map`, `evaluate_joins` if `--llm-model` is set)
5. Ask something the server cannot answer, e.g. "Get node 'nonexistent'." It shows up as `not_found` / error in **Tool calls per second, by outcome**.
The dashboard refreshes every 10 s; with the default 15 s scrape allow up to half a minute. Panels to point at: **MCP tool latency (p50 / p95)**, **Tool calls per second**, **HTTP requests per second**, the **last run** row, **Findings: auto-applied vs human-reviewed**, **Judge agreement**, **Hallucination rate**.

**8d. Show the alert.** Alert `CogentAgentFailureRateHigh` fires when more than 25 % of the agent's operations (fetches, LLM calls, table loads, daemon runs) failed over 24 h, for 5 minutes. To trigger it on purpose, make the sources unreachable and run the agent a few times, e.g. `KG_FETCH=http HTTPS_PROXY=http://127.0.0.1:9 python3 src/kg_agent.py --home kg_agent_home run` (every download fails). Watch **Failure rate, 24 h** and **Failures by stage**; the **Alert** tile goes PENDING, then FIRING five minutes after the ratio passes 25 %. The same scenario (a site that went away) is what fired the alert in the screenshot. Because the ratio spans 24 h, one failed run among many good ones will not fire it, and it takes several failed runs to pass 25 % if you have many good ones.

**8e. Let others watch (optional): a second tunnel for Grafana.** Only if the audience is remote. Grafana then faces the Internet, so give it a strong password and share a read-only account, not `admin`:
```bash
set -a; source deploy/observability/.env; set +a
curl -s -X POST http://127.0.0.1:3000/api/admin/users -H 'Content-Type: application/json' -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
  -d '{"name":"demo viewer","login":"viewer","password":"<another long password>"}'     # created with the Viewer role
cloudflared tunnel --url http://127.0.0.1:3000         # terminal E; prints https://<other-name>.trycloudflare.com
```
Share that address, the user `viewer` and the password (open `/d/cogent-observability`). Do **not** expose Prometheus (9090). The MCP server's `/metrics` on its own tunnel address needs a token, so it is not open either. (The curl line puts the admin password in the process list for a moment: fine on a single-user machine.)

**8f. No real data yet, or no Ollama?** `python3 deploy/observability/demo/generate_traffic.py --work /tmp/cogent-demo/w --minutes 12` fills the dashboard with synthetic runs (mock site, mock model). Use it only for a rehearsal or a screenshot, never in front of an audience as if it were your data; it starts its own MCP server on 8765, so stop the real one first.

## 9. Tear down
- Observability: `docker compose -f deploy/observability/docker-compose.observability.yml down` (add `-v` to delete the collected metrics and Grafana's database). Stop the Grafana tunnel (Ctrl+C) and delete the `viewer` user if you created one.
- Quick tunnel: Ctrl+C the tunnel (the address dies at once). Named tunnel: stop `named-tunnel.sh run` (or `systemctl stop cogent-tunnel`); the hostname stays yours.
- Stop the server, then `python3 src/kg_mcp.py --home kg_agent_home token revoke --name web`.
- Remove the connector in claude.ai. Optionally `docker compose -f deploy/docker-compose.tabledb.yml stop`.

## Caveats
- The dashboard and the alert were exercised here with Prometheus and Grafana against a synthetic demo (mock site, mock model); not against your sources, Ollama model, or the Claude web app. The proxy trick in 8d and the viewer account and Grafana tunnel in 8e were not run when this was written. Hallucination rate and judge agreement are heuristics (docs/observability.md).
- With `--workers 2` (as in the optional line of step 5) `/metrics` comes from whichever worker takes the scrape, so the MCP panels jump around; use one worker for a metrics demo. The agent's own panels come from a file and are exact.
- A new quick-tunnel address does not affect Prometheus (it scrapes `127.0.0.1:8765` directly); only the Claude connector and a Grafana tunnel need the new address.
- Custom connectors depend on your Claude plan and on what the web app currently offers; I can't check either.
- The OAuth flow was tested only against the server's own test client, not claude.ai. If the web app rejects the sign-in, note the error.
- The named-tunnel commands could not be run against Cloudflare when they were written; if `deploy/named-tunnel.sh setup` fails, its output shows which `cloudflared` command did.
- `deploy/demo-tunnel.sh --oauth` automates the quick-tunnel steps 3–5 but does not pass the table-database flags, so use the manual steps for the table tools.
- Quick tunnels have no uptime guarantee. Anyone with the address and a token can read everything staged in `kg_agent_home`.
- `--public-host '*'` turns off the Host-header check because the quick-tunnel name is random. The token check still applies.
- A run limited with `--only` no longer erases other entries' warnings, but run the agent without `--only` before a demo so `get_warnings` is complete.
