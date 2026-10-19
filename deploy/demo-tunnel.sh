#!/usr/bin/env bash
# Demo helper: run the MCP server on loopback and expose it through a Cloudflare quick tunnel.
#   deploy/demo-tunnel.sh [--oauth] [agent-home] [port]
# --oauth also enables the OAuth 2.1 login that claude.ai custom connectors need (the tunnel is started first so the
# server can be told its own public URL).
# Creates a throw-away token, prints the public URL and a ready-to-paste `claude mcp add` command, and on exit
# (Ctrl+C) stops the server and revokes the token. Needs: cloudflared, the project's Python environment.
set -euo pipefail
OAUTH=0
if [ "${1:-}" = "--oauth" ]; then OAUTH=1; shift; fi
HOME_DIR="${1:-kg_agent_home}"
PORT="${2:-8765}"
cd "$(dirname "$0")/.."
command -v cloudflared >/dev/null || { echo "cloudflared not found; see docs/mcp.md, section 'Demo with a Cloudflare quick tunnel'" >&2; exit 1; }
NAME="demo-$(date +%Y%m%d-%H%M%S)"
TOKEN="$(python src/kg_mcp.py --home "$HOME_DIR" token create --name "$NAME" --days 1 2>/dev/null)"
LOG="$(mktemp)"
cleanup() {
  kill "${SERVER_PID:-}" "${TUNNEL_PID:-}" 2>/dev/null || true
  python src/kg_mcp.py --home "$HOME_DIR" token revoke --name "$NAME" >/dev/null 2>&1 || true
  rm -f "$LOG"
  echo "stopped; token $NAME revoked"
}
trap cleanup EXIT
trap "exit 130" INT TERM
cloudflared tunnel --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 1 60); do
  URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || { echo "cloudflared exited:" >&2; cat "$LOG" >&2; exit 1; }
  sleep 1
done
[ -n "$URL" ] || { echo "no tunnel URL after 60 s:" >&2; cat "$LOG" >&2; exit 1; }
EXTRA=()
[ "$OAUTH" = 1 ] && EXTRA=(--oauth-issuer "$URL")
python src/kg_mcp.py --home "$HOME_DIR" serve --port "$PORT" --public-host '*' "${EXTRA[@]}" &
SERVER_PID=$!
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
cat <<EOF

MCP endpoint : $URL/mcp
Token        : $TOKEN      (expires in 1 day, revoked when this script exits)
Claude Code  : claude mcp add --transport http cogent-kg $URL/mcp --header "Authorization: Bearer $TOKEN"
Health check : curl $URL/healthz
Ctrl+C to stop.
EOF
if [ "$OAUTH" = 1 ]; then cat <<EOF
claude.ai    : Settings > Connectors > Add custom connector > $URL/mcp ; on the sign-in page paste the token above.
EOF
fi
wait "$TUNNEL_PID"
