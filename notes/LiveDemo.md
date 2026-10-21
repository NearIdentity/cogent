# Demo deployment: Cloudflare quick tunnel and the Claude web app

This uses the latest committed code. Run commands from the repo root. The quick tunnel gives a random `https://<name>.trycloudflare.com` address that changes on every start, so re-add the connector each time.

## 1. One-time setup
```bash
pip install -r requirements.txt
ollama pull nomic-embed-text                       # embeddings (add llama3.1:8b only for LLM join inference)
cp deploy/tabledb.env.example deploy/.env          # edit: set COGENT_DB_PASSWORD and COGENT_RO_PASSWORD
docker compose -f deploy/docker-compose.tabledb.yml up -d
docker compose -f deploy/docker-compose.tabledb.yml logs | tail -3     # wait for "ready to accept connections"
```
Install `cloudflared` if you haven't (Ubuntu, from Cloudflare's apt repository):
```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```
If you reused an old database volume, the passwords in `deploy/.env` won't take effect; use `ALTER USER` or `down -v`, as discussed earlier.

## 2. Build the data (agents)
```bash
export COGENT_TABLEDB_DSN="postgresql://cogent:<COGENT_DB_PASSWORD>@127.0.0.1:5432/cogent"
python src/kg_agent.py --home kg_agent_home run --tables        # add --llm-joins --join-model llama3.1:8b if you want LLM joins
python src/kg_tabledb.py status
```
Check that `status` reports tables and rows. A password error here means the DSN doesn't match the database.

## 3. Create the access token
```bash
python src/kg_mcp.py --home kg_agent_home token create --name web --days 1     # printed once; save it
```

## 4. Open the tunnel first (terminal 1)
The OAuth issuer must be the public URL, so the tunnel has to be up first:
```bash
cloudflared tunnel --url http://127.0.0.1:8765
```
Note the `https://<name>.trycloudflare.com` address.

## 5. Start the MCP server with OAuth and the table tools (terminal 2)
```bash
python src/kg_mcp.py --home kg_agent_home serve --host 127.0.0.1 --port 8765 \
  --public-host '*' \
  --oauth-issuer https://<name>.trycloudflare.com \
  --tabledb-dsn "postgresql://cogent_ro:<COGENT_RO_PASSWORD>@127.0.0.1:5432/cogent" \
  --embedder ollama --embed-model nomic-embed-text
```
Use the read-only `cogent_ro` login here, not the owner. Add `--llm-model llama3.1:8b` if you want `evaluate_joins`. Add `--paywalled-assets block` if purchased standards are staged and the demo audience shouldn't see them.

## 6. Check from outside
```bash
curl https://<name>.trycloudflare.com/healthz           # {"status":"ok",...}
curl -i -X POST https://<name>.trycloudflare.com/mcp    # 401: authentication is on
```

## 7. Connect the Claude web app
1. In claude.ai, open Settings → Connectors → Add custom connector.
2. Enter `https://<name>.trycloudflare.com/mcp`.
3. Claude opens the server's sign-in page. Paste the token from step 3 and approve.
4. In a chat, enable the connector, then ask: "What is the allowable stress for A572 in the standards' tables?" The server instructions steer Claude to `lookup_values`.

To demonstrate tailoring, ask it to use the `strict_design_lookup` prompt, or to call `set_session_preferences` with `profile: strict` versus `exploratory`.

## 8. Tear down
- Ctrl+C the tunnel (the URL dies immediately) and stop the server.
- `python src/kg_mcp.py --home kg_agent_home token revoke --name web`
- Remove the connector in claude.ai. Optionally `docker compose -f deploy/docker-compose.tabledb.yml stop`.

## Caveats
- Custom connectors depend on your Claude plan and on what the web app currently offers; I can't check either.
- The OAuth flow was tested only against the server's own test client, not claude.ai. If the web app rejects the sign-in, note the error.
- `deploy/demo-tunnel.sh --oauth` automates steps 3–5 but does not pass the table-database flags, so use the manual steps for the table tools.
- Quick tunnels have no uptime guarantee. Anyone with the URL and token can read everything staged in `kg_agent_home`.
- `--public-host '*'` turns off the Host-header check because the tunnel name is random. The token check still applies.
