#!/usr/bin/env bash
# Set up and check a free NAMED Cloudflare tunnel for the cogent MCP server (stable URL, unlike the quick tunnel in demo-tunnel.sh).
#
#   deploy/named-tunnel.sh setup  mcp.example.com [tunnel-name] [port]   one-time: log in, create the tunnel, route DNS, write the config
#   deploy/named-tunnel.sh run    [config]                               run the tunnel in the foreground
#   deploy/named-tunnel.sh check  mcp.example.com [token]               test the public URL from outside (health, auth gate, MCP endpoint)
#
# Needs: cloudflared (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), a free Cloudflare account and a domain
# whose DNS is on Cloudflare. `setup` opens a browser login once. Nothing here is specific to this project except the port (default 8765).
# Set COGENT_CHECK_SCHEME=http to test against a local server (host may then be 127.0.0.1:8765).
# The config is written to ${COGENT_TUNNEL_CONFIG:-$HOME/.cloudflared/cogent.yml}; copy it to /etc/cogent/cloudflared.yml for the systemd unit.
set -euo pipefail

CONFIG="${COGENT_TUNNEL_CONFIG:-$HOME/.cloudflared/cogent.yml}"
need() { command -v "$1" >/dev/null || { echo "error: $1 not found" >&2; exit 1; }; }

cmd="${1:-}"; shift || true
case "$cmd" in
  setup)
    host="${1:?usage: setup <hostname> [tunnel-name] [port]}"; name="${2:-cogent}"; port="${3:-8765}"
    need cloudflared
    if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
      echo "== log in to Cloudflare (a browser window opens; pick the zone of $host)"
      cloudflared tunnel login
    fi
    if cloudflared tunnel info "$name" >/dev/null 2>&1; then
      echo "== tunnel '$name' already exists"
    else
      echo "== creating tunnel '$name'"
      cloudflared tunnel create "$name"
    fi
    uuid="$(cloudflared tunnel list 2>/dev/null | awk -v n="$name" '$2 == n {print $1; exit}')"
    [ -n "$uuid" ] || { echo "error: could not find the id of tunnel '$name' (cloudflared tunnel list)" >&2; exit 1; }
    cred="$HOME/.cloudflared/$uuid.json"
    [ -f "$cred" ] || echo "warning: credentials file $cred not found; copy it from the machine that created the tunnel" >&2
    echo "== routing $host to the tunnel (creates a CNAME in your DNS zone)"
    cloudflared tunnel route dns "$name" "$host" || echo "note: route dns failed (the record may already exist)"
    mkdir -p "$(dirname "$CONFIG")"
    cat > "$CONFIG" <<EOF
tunnel: $uuid
credentials-file: $cred
loglevel: info
ingress:
  - hostname: $host
    service: http://127.0.0.1:$port
  - service: http_status:404
EOF
    chmod 600 "$CONFIG"
    echo "== wrote $CONFIG"
    cat <<EOF

Next:
  1. Start the MCP server bound to loopback with the public name, e.g.
       python3 src/kg_mcp.py --home kg_agent_home serve --port $port --public-host $host --oauth-issuer https://$host ...
  2. Run the tunnel:   deploy/named-tunnel.sh run
     or as a service:  sudo cp $CONFIG /etc/cogent/cloudflared.yml && sudo cp $cred /etc/cogent/cloudflared-credentials.json
                       (edit credentials-file in the copy) && sudo systemctl enable --now cogent-tunnel
  3. Check it from outside:   deploy/named-tunnel.sh check $host <token>
  4. Claude Code:  claude mcp add --transport http cogent-kg https://$host/mcp --header "Authorization: Bearer <token>"
EOF
    ;;
  run)
    need cloudflared
    exec cloudflared --no-autoupdate tunnel --config "${1:-$CONFIG}" run
    ;;
  check)
    host="${1:?usage: check <hostname> [token]}"; token="${2:-}"
    need curl
    fail=0
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${COGENT_CHECK_SCHEME:-https}://$host/healthz" || true)"
    echo "healthz           -> $code   (expect 200)"; [ "$code" = 200 ] || fail=1
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "${COGENT_CHECK_SCHEME:-https}://$host/mcp" || true)"
    echo "mcp, no token     -> $code   (expect 401)"; [ "$code" = 401 ] || fail=1
    if [ -n "$token" ]; then
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "${COGENT_CHECK_SCHEME:-https}://$host/mcp" \
        -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}' || true)"
      echo "mcp, with token   -> $code   (expect 200)"; [ "$code" = 200 ] || fail=1
    else
      echo "mcp, with token   -> skipped (pass the token as 2nd argument)"
    fi
    [ "$fail" = 0 ] && echo "OK" || { echo "FAILED"; exit 1; }
    ;;
  *)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
