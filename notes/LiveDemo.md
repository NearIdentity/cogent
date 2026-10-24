# Demo deployment: the MCP server on the Internet, for the Claude web app

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
  --embedder ollama --embed-model nomic-embed-text
```
With a **named tunnel** use `--public-host mcp.example.com --oauth-issuer https://mcp.example.com` instead of the first two options.

The server reads the DSN from `COGENT_TABLEDB_DSN`, so the password stays out of the process list. Look for `table database reachable as cogent_ro (N table(s))` in the log. `address already in use` means an older server still holds port 8765 (`ss -ltnp | grep 8765`, then stop it).
Optional: `--llm-model llama3.1:8b` enables `evaluate_joins`; `--paywalled-assets block` hides purchased standards from the audience; `--workers 2 --session-store sqlite:kg_agent_home/mcp/sessions.db --rate-limit 600 --metrics` for a sturdier demo.

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

## 8. Tear down
- Quick tunnel: Ctrl+C the tunnel (the address dies at once). Named tunnel: stop `named-tunnel.sh run` (or `systemctl stop cogent-tunnel`); the hostname stays yours.
- Stop the server, then `python3 src/kg_mcp.py --home kg_agent_home token revoke --name web`.
- Remove the connector in claude.ai. Optionally `docker compose -f deploy/docker-compose.tabledb.yml stop`.

## Caveats
- Custom connectors depend on your Claude plan and on what the web app currently offers; I can't check either.
- The OAuth flow was tested only against the server's own test client, not claude.ai. If the web app rejects the sign-in, note the error.
- The named-tunnel commands could not be run against Cloudflare when they were written; if `deploy/named-tunnel.sh setup` fails, its output shows which `cloudflared` command did.
- `deploy/demo-tunnel.sh --oauth` automates the quick-tunnel steps 3–5 but does not pass the table-database flags, so use the manual steps for the table tools.
- Quick tunnels have no uptime guarantee. Anyone with the address and a token can read everything staged in `kg_agent_home`.
- `--public-host '*'` turns off the Host-header check because the quick-tunnel name is random. The token check still applies.
- A run limited with `--only` no longer erases other entries' warnings, but run the agent without `--only` before a demo so `get_warnings` is complete.
