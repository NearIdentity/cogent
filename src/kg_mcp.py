#!/usr/bin/env python3
"""MCP server that serves the staged knowledge graphs (and their meta-data) to AI agents.

    python src/kg_mcp.py token create --name claude          # prints the secret once
    python src/kg_mcp.py serve --home kg_agent_home --port 8765 --public-host kg.example.com

Transport: MCP "streamable HTTP" at ``/mcp`` (works with Claude Code, the Claude API MCP connector,
Claude Desktop through ``mcp-remote``, MCP Inspector, ...). ``/healthz`` is open for probes.

Authentication: OAuth 2.1 (``--oauth-issuer``, for claude.ai connectors) or static bearer tokens (``Authorization: Bearer <token>`` or ``X-API-Key``). Only SHA-256
hashes are stored (``<home>/mcp/tokens.json``); ``COGENT_MCP_TOKEN`` can supply one extra token.
TLS is expected to be terminated by a reverse proxy / tunnel (see docs/mcp.md).
The server is read-only: it never changes the agent home.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kg_store import GENERAL_DISCLAIMERS, NotFound, Store  # noqa: E402

log = logging.getLogger("kg_mcp")

INSTRUCTIONS = """\
Serves knowledge graphs (KGs) built from nuclear-industry standards, regulations and report templates,
with provenance. Workflow: get_overview -> list_entries -> get_entry_metadata(entry) (capture time, whether
the on-board content is the latest available edition, warnings) -> get_dependents (report<->standard impact, from the combined graph) / graph_summary / get_outline /
search_graph -> get_node / get_neighbors / get_table for details.
`ref='combined'` is the derived entry-level graph. `entry` is a key such as 'standard:22' or 'report:3'; a 16-hex document id also works wherever `ref` is asked.
Every graph result carries a `provenance` block: ALWAYS check it and relay stale-capture, 'not the latest
edition', paywall and OCR caveats to the user; never present extracted text as authoritative.
"""


# ---------------------------------------------------------------------------
# Token store (hashes only)
# ---------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """``{"tokens": [{"name", "sha256", "created", "expires", "revoked"}]}`` in a 0600 JSON file."""

    def __init__(self, path: Path, env_token: Optional[str] = None):
        self.path, self.env_hash = Path(path), (_hash(env_token) if env_token else None)
        self._mtime, self._rows = -1.0, []

    def _load(self) -> list[dict[str, Any]]:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._rows = -1.0, []
            return self._rows
        if m != self._mtime:                    # picks up `token create/revoke` without a restart
            self._rows = json.loads(self.path.read_text(encoding="utf-8")).get("tokens", [])
            self._mtime = m
        return self._rows

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"tokens": rows}, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def create(self, name: str, days: Optional[float] = None) -> str:
        rows = [r for r in self._load() if r["name"] != name]
        token = "cgt_" + secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        rows.append({"name": name, "sha256": _hash(token), "created": now.isoformat(timespec="seconds"),
                     "expires": (now + timedelta(days=days)).isoformat(timespec="seconds") if days else None,
                     "revoked": False})
        self._save(rows)
        return token

    def revoke(self, name: str) -> bool:
        rows = self._load()
        hit = False
        for r in rows:
            if r["name"] == name and not r["revoked"]:
                r["revoked"], hit = True, True
        if hit:
            self._save(rows)
        return hit

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "sha256"} for r in self._load()]

    def verify(self, token: str) -> Optional[str]:
        """Name of the matching token, or None. Constant-time comparison against every candidate."""
        h, found, now = _hash(token), None, datetime.now(timezone.utc)
        if self.env_hash and hmac.compare_digest(h, self.env_hash):
            found = "env"
        for r in self._load():
            if hmac.compare_digest(h, r["sha256"]) and not r["revoked"]:
                exp = r.get("expires")
                if exp is None or datetime.fromisoformat(exp) > now:
                    found = r["name"]
        return found

    def active_names(self) -> set[str]:
        now = datetime.now(timezone.utc)
        names = {r["name"] for r in self._load() if not r["revoked"] and (not r.get("expires") or datetime.fromisoformat(r["expires"]) > now)}
        return names | ({"env"} if self.env_hash else set())

    def any_configured(self) -> bool:
        return bool(self.env_hash) or any(not r["revoked"] for r in self._load())


class BearerAuth:
    """Pure-ASGI middleware: 401 unless a valid token is presented (``/healthz`` stays open)."""

    OPEN = {"/healthz"}

    def __init__(self, app, tokens: TokenStore):
        self.app, self.tokens = app, tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.OPEN:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else headers.get("x-api-key", "").strip()
        name = self.tokens.verify(token) if token else None
        if name is None:
            log.warning("rejected %s %s from %s", scope["method"], scope["path"], (scope.get("client") or ["?"])[0])
            body = b'{"error":"unauthorized","detail":"send Authorization: Bearer <token>"}'
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="cogent-kg"')]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["token_name"] = name
        return await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _tool_errors(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None
    return inner


def create_server(store: Store, *, host: str = "127.0.0.1", port: int = 8765, public_hosts: Optional[list[str]] = None,
                  stateless: bool = True, oauth: Any = None, issuer_url: Optional[str] = None) -> FastMCP:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    for h in public_hosts or []:
        hosts += [h, f"{h}:*"]
    auth_kw: dict[str, Any] = {}
    if oauth is not None:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
        auth_kw = dict(auth_server_provider=oauth, auth=AuthSettings(
            issuer_url=issuer_url, resource_server_url=issuer_url.rstrip("/") + "/mcp", validate_token_resource=False,
            required_scopes=["kg:read"], revocation_options=RevocationOptions(enabled=True),
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["kg:read"], default_scopes=["kg:read"])))
    mcp = FastMCP("cogent-kg", instructions=INSTRUCTIONS, host=host, port=port, stateless_http=stateless, json_response=True, **auth_kw,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                               allowed_origins=["*"]))

    def provenance(ref: str) -> dict[str, Any]:
        """Compact meta-data attached to every graph answer."""
        if ref == "combined":
            m = store.combined_meta()
            return {"ref": "combined", **m, "disclaimer": GENERAL_DISCLAIMERS[0] + " (see get_disclaimers)"}
        if ":" not in ref:
            return {"ref": ref, "note": "document-level graph; call get_entry_metadata on an entry for capture/version "
                                        "information", "disclaimers": GENERAL_DISCLAIMERS[:1]}
        m = store.entry_metadata(ref)
        extra = [d for d in m["disclaimers"] if d not in GENERAL_DISCLAIMERS]
        return {"entry": ref, "name": m["name"], "graph_built": m["graph"]["built"],
                "sources_last_checked": m["capture"]["documents_last_checked"], "age_days": m["capture"]["age_days"],
                "stale": m["capture"]["stale"], "version_status": m["version"]["status"],
                "version_summary": m["version"]["summary"],
                "latest_available": [x.get("label") for x in m["version"]["latest_available"]],
                "warnings": [w["message"] for w in m["warnings"]], "caveats": extra,
                "disclaimer": GENERAL_DISCLAIMERS[0] + " (see get_disclaimers)"}

    def wrap(ref: str, data: Any) -> dict[str, Any]:
        return {"provenance": provenance(ref), **(data if isinstance(data, dict) else {"result": data})}

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_overview() -> dict[str, Any]:
        """Start here: how many entries/graphs exist, version-status counts, open warnings, last agent run, disclaimers."""
        return store.overview()

    @mcp.tool(annotations=RO)
    @_tool_errors
    def list_entries(kind: Optional[Literal["standard", "report"]] = None, query: Optional[str] = None) -> dict[str, Any]:
        """List configured standards/reports with graph availability, build time, version status and warning counts.
        `query` filters by name/description."""
        return {"entries": store.catalog(kind, query)}

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_entry_metadata(entry: str) -> dict[str, Any]:
        """Full provenance of one entry ('standard:22'): source links, per-document capture times (first seen, last
        checked, last changed), graph build time, latest-available vs on-board editions, open warnings, disclaimers."""
        return store.entry_metadata(entry)

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_dependents(entry: str) -> dict[str, Any]:
        """Which reports depend on a standard (and which standards a report depends on), from the combined graph.
        Curated relations (from the configuration: complies_with / overlaps_with) are separated from relations inferred from
        text. `if_this_changes_reports_affected` answers 'what is affected if this standard changes edition?'. Each related
        entry carries its version status. The combined graph is also available as ref='combined' in the graph tools."""
        return store.dependents(entry)

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_warnings(entry: Optional[str] = None) -> dict[str, Any]:
        """Open warnings (paywalled without local copy, new edition for purchase, outdated local copy, changed store
        page, ...). Omit `entry` for all."""
        return {"warnings": store.alerts(entry)}

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_disclaimers(entry: Optional[str] = None) -> dict[str, Any]:
        """General disclaimers, plus the ones that apply to `entry` (paywall, stale capture, OCR, not-latest edition)."""
        return {"disclaimers": store.entry_metadata(entry)["disclaimers"] if entry else GENERAL_DISCLAIMERS}

    @mcp.tool(annotations=RO)
    @_tool_errors
    def graph_summary(ref: str) -> dict[str, Any]:
        """Node/edge counts by type and relation for an entry key or a document id."""
        return wrap(ref, store.graph_summary(ref))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_outline(ref: str, max_items: int = 200) -> dict[str, Any]:
        """Headings of the document(s) in reading order (ids usable with get_node / get_neighbors)."""
        return wrap(ref, {"outline": store.outline(ref, max_items)})

    @mcp.tool(annotations=RO)
    @_tool_errors
    def search_graph(ref: str, query: str, node_types: Optional[list[str]] = None, limit: int = 20) -> dict[str, Any]:
        """Keyword search over node labels/text (all terms must match). node_types e.g. ['Paragraph','Heading','Table',
        'Figure','Entity','Identifier']."""
        return wrap(ref, {"matches": store.search(ref, query, node_types, limit)})

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_node(ref: str, node_id: str, full_text: bool = False) -> dict[str, Any]:
        """One node with its attributes (long text is clipped unless full_text=true)."""
        return wrap(ref, {"node": store.node(ref, node_id, full_text)})

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_neighbors(ref: str, node_id: str, relation: Optional[str] = None,
                      direction: Literal["in", "out", "both"] = "both", limit: int = 50) -> dict[str, Any]:
        """Adjacent nodes; filter by relation (CONTAINS, NEXT, MENTIONS, CITES, references, HAS_DOCUMENT, complies_with,
        overlaps_with, ...)."""
        return wrap(ref, store.neighbors(ref, node_id, relation, direction, limit))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_subgraph(ref: str, center: str, depth: int = 1, max_nodes: int = 100) -> dict[str, Any]:
        """Nodes and edges within `depth` hops (<=4) of `center`, capped at max_nodes."""
        return wrap(ref, store.subgraph(ref, center, depth, max_nodes))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_table(ref: str, node_id: str) -> dict[str, Any]:
        """Rows of a Table node."""
        return wrap(ref, store.table(ref, node_id))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def export_graph(ref: str, max_nodes: int = 2000) -> dict[str, Any]:
        """The graph as node-link JSON (networkx format), truncated to max_nodes."""
        return wrap(ref, {"graph": store.export(ref, max_nodes)})

    # ---- resources (for clients that browse rather than call tools)
    @mcp.resource("kg://catalog", mime_type="application/json")
    def r_catalog() -> str:
        return json.dumps({"entries": store.catalog(), "disclaimers": GENERAL_DISCLAIMERS}, indent=1)

    @mcp.resource("kg://combined/graph", mime_type="application/json")
    def r_combined() -> str:
        try:
            return json.dumps({"provenance": provenance("combined"), "graph": store.export("combined")})
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    @mcp.resource("kg://disclaimers", mime_type="text/plain")
    def r_disclaimers() -> str:
        return "\n".join(f"- {d}" for d in GENERAL_DISCLAIMERS)

    @mcp.resource("kg://{kind}/{ident}/metadata", mime_type="application/json")
    def r_meta(kind: str, ident: str) -> str:
        try:
            return json.dumps(store.entry_metadata(f"{kind}:{ident}"), indent=1)
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    @mcp.resource("kg://{kind}/{ident}/graph", mime_type="application/json")
    def r_graph(kind: str, ident: str) -> str:
        try:
            ref = f"{kind}:{ident}"
            return json.dumps({"provenance": provenance(ref), "graph": store.export(ref)})
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):                    # noqa: ANN001
        from starlette.responses import JSONResponse
        run = store.last_run()
        return JSONResponse({"status": "ok", "agent_last_run": (run or {}).get("finished")})

    if oauth is not None:
        @mcp.custom_route("/consent", methods=["GET", "POST"])
        async def consent(request):                # noqa: ANN001
            return await oauth.consent(request)

    return mcp


def build_app(store: Store, tokens: Optional[TokenStore], *, issuer_url: Optional[str] = None, oauth_path: Optional[Path] = None, **kw):
    """ASGI app = MCP streamable-HTTP app. With ``issuer_url`` it is an OAuth 2.1 resource+authorization server (static
    bearer tokens still accepted); otherwise wrapped in the plain bearer-token check when ``tokens`` is given."""
    if issuer_url:
        from kg_oauth import CogentOAuthProvider
        assert tokens is not None, "OAuth needs the token store (tokens approve clients)"
        prov = CogentOAuthProvider(oauth_path or Path("oauth.json"), tokens, issuer_url)
        return create_server(store, oauth=prov, issuer_url=issuer_url, **kw).streamable_http_app()
    app = create_server(store, **kw).streamable_http_app()
    return BearerAuth(app, tokens) if tokens else app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path(_env("COGENT_HOME", "kg_agent_home")), help="agent home directory")
    ap.add_argument("--tokens", type=Path, default=None, help="token file (default <home>/mcp/tokens.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--standards", type=Path, default=Path(_env("COGENT_STANDARDS", "config/standards.json")))
    s.add_argument("--reports", type=Path, default=Path(_env("COGENT_REPORTS", "config/reports.json")))
    s.add_argument("--host", default="127.0.0.1", help="bind address (keep 127.0.0.1 behind a proxy/tunnel)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--public-host", action="append", default=[], metavar="HOST",
                   help="public host name(s) clients use (Host-header allow-list); repeatable")
    s.add_argument("--stale-after", type=float, default=3.0, help="days before captures are flagged stale")
    s.add_argument("--oauth-issuer", metavar="URL", help="enable OAuth 2.1 (claude.ai custom connectors); the public https "
                   "base URL, e.g. https://kg.example.com")
    s.add_argument("--no-auth", action="store_true", help="disable authentication (loopback binds only)")
    s.add_argument("--stateful", action="store_true", help="keep per-client sessions (default: stateless)")

    t = sub.add_parser("token", help="manage access tokens")
    ts = t.add_subparsers(dest="tcmd", required=True)
    c = ts.add_parser("create", help="create a token (printed once)")
    c.add_argument("--name", required=True)
    c.add_argument("--days", type=float, default=None, help="expire after N days")
    ts.add_parser("list", help="list tokens (no secrets)")
    r = ts.add_parser("revoke", help="revoke a token by name")
    r.add_argument("--name", required=True)

    a = ap.parse_args(argv)
    tokens = TokenStore(a.tokens or a.home / "mcp" / "tokens.json", os.environ.get("COGENT_MCP_TOKEN"))

    if a.cmd == "token":
        if a.tcmd == "create":
            print(tokens.create(a.name, a.days))
            print(f"(shown once; only its hash is stored in {tokens.path})", file=sys.stderr)
        elif a.tcmd == "list":
            for row in tokens.list():
                print(json.dumps(row))
        else:
            ok = tokens.revoke(a.name)
            print("revoked" if ok else "no such active token")
            return 0 if ok else 1
        return 0

    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.no_auth and a.host not in ("127.0.0.1", "localhost", "::1"):
        ap.error("--no-auth is only allowed on a loopback bind")
    if not a.no_auth and not tokens.any_configured():
        ap.error("no access token configured: run 'kg_mcp.py token create --name <client>' or set COGENT_MCP_TOKEN")
    store = Store(a.home, a.standards, a.reports, a.stale_after)
    app = build_app(store, None if a.no_auth else tokens, issuer_url=None if a.no_auth else a.oauth_issuer,
                    oauth_path=a.home / "mcp" / "oauth.json", host=a.host, port=a.port, public_hosts=a.public_host,
                    stateless=not a.stateful)
    mode = "OFF" if a.no_auth else f"OAuth 2.1 + bearer token (issuer {a.oauth_issuer})" if a.oauth_issuer else "bearer token"
    log.info("serving %s on http://%s:%s/mcp (auth %s)", a.home, a.host, a.port, mode)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
