Instructions for a one-time demo of the MCP server through the Claude web UI. They assume Ubuntu and the unzipped `cogent-dev.zip`. I haven't tested the claude.ai steps (step 5), and Anthropic's connector screens may differ from what I describe.

## 0. Before you start
- **Plan:** custom connectors in claude.ai depend on your Claude plan. Check that Settings → Connectors shows "Add custom connector". If it doesn't, the demo can't run in the web UI. You can still use Claude Code with the bearer-token command that step 4 prints.
- **Content:** a public URL means anyone with the token can read what is staged. Don't stage purchased standards (entries 20, 21, 25, 30) for a public demo unless your licence allows it. To be safe, start the server with `--paywalled-assets block`.
- **Machine:** it must stay on and online during the demo.

## 1. One-time setup
```bash
unzip cogent-dev.zip && cd cogent
bash venv-setup.sh                      # creates the ./cogent virtual environment (uv)
source ./cogent/bin/activate
pip install -r requirements.txt
```
`venv-setup.sh` uses `uv`. If it's missing, use `python3 -m venv cogent && source cogent/bin/activate` instead.

## 2. Install `cloudflared`
```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
cloudflared --version
```
On an ARM machine use `cloudflared-linux-arm64.deb`. The apt and from-source options are in `docs/mcp.md`. No Cloudflare account is needed.

## 3. Prepare the demo data
The server only serves what the agent has already staged. Run the agent once, which needs internet access and may take several minutes:
```bash
python src/kg_agent.py run
python src/kg_agent.py status           # check graphs were built
```
To keep the demo small, restrict it to a few entries, for example `python src/kg_agent.py run --only standard:1,standard:23,report:1`.

## 4. Start the server and the tunnel
One command does both:
```bash
deploy/demo-tunnel.sh --oauth
```
It opens the Cloudflare tunnel first, then starts the server with OAuth enabled for the tunnel's address, and creates a one-day access token. After a few seconds it prints:
```
MCP endpoint : https://<random-words>.trycloudflare.com/mcp
Token        : cgt_...
```
Copy both and leave the terminal open. Ctrl+C stops everything and revokes the token.

To also block paywalled assets, add the flag by editing the `serve` line in `deploy/demo-tunnel.sh`, or start the two pieces by hand as described in `docs/mcp.md`.

Quick check from another terminal:
```bash
curl https://<random-words>.trycloudflare.com/healthz         # {"status":"ok",...}
curl -i -X POST https://<random-words>.trycloudflare.com/mcp   # 401 without a token
```

## 5. Add the connector in the Claude web UI
1. In claude.ai, open **Settings → Connectors → Add custom connector**.
2. Enter a name (for example "cogent-kg") and the URL `https://<random-words>.trycloudflare.com/mcp`.
3. Connect. A browser tab should open on the server's sign-in page ("Authorize MCP client"). Paste the token from step 4 and press **Allow**.
4. Back in claude.ai, enable the connector for your conversation.

## 6. Prompts for the demo
- "Use cogent-kg to give me an overview of what's available, then list the standards."
- "Is the staged copy of NRC RG 1.233 the latest edition? Cite capture times and warnings."
- "Which reports are affected if standard 1 (10 CFR Part 50) changes?"
- "Search the 10 CFR 50.69 graph for 'categorization' and show the neighbors of the best match."
- "List the figures and tables for standard 23 and show one."

If answers look empty, the agent run in step 3 hasn't staged those entries.

## 7. When the demo ends
Press Ctrl+C in the script's terminal. That closes the tunnel, so the URL stops working, and revokes the token. Then remove the connector in claude.ai (Settings → Connectors).

## If something goes wrong
- **The sign-in page doesn't open or claude.ai says it can't connect:** the connector flow is untested against claude.ai from my side. Check that you started with `--oauth` (the script prints the issuer in its log line), and that the URL you entered ends with `/mcp`. Each restart gives a new URL, so remove the old connector and add it again.
- **"Token is not valid":** you pasted a token from an earlier run. Each run of the script makes a new one.
- **`421` or "Invalid Host header":** the server wasn't started through the script or without `--public-host '*'`.
- **Tools return nothing:** run `python src/kg_agent.py status` to see which entries have graphs.
- **Fallback with no OAuth:** `deploy/demo-tunnel.sh` without `--oauth`, then in Claude Code run the `claude mcp add --transport http …` line that the script prints.

The same material is in `docs/mcp.md`, in the section "Demo with a Cloudflare quick tunnel".
