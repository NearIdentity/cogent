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
import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kg_store import GENERAL_DISCLAIMERS, NotFound, Store  # noqa: E402
from kg_tableservice import TableService  # noqa: E402

log = logging.getLogger("kg_mcp")

INSTRUCTIONS = """\
Serves knowledge graphs (KGs) built from nuclear-industry standards, regulations and report templates,
with provenance. Workflow: get_overview -> list_entries -> get_entry_metadata(entry) (capture time, whether
the on-board content is the latest available edition, warnings) -> get_dependents (report<->standard impact, from the combined graph) / graph_summary / get_outline /
search_graph -> get_node / get_neighbors / get_table for details; list_assets / get_figure / get_table_csv for extracted\nfigures and tables (paywalled content is watermarked and carries warnings: always relay them).
`ref='combined'` is the derived entry-level graph. `entry` is a key such as 'standard:22' or 'report:3'; a 16-hex document id also works wherever `ref` is asked.
Every graph result carries a `provenance` block: ALWAYS check it and relay stale-capture, 'not the latest
edition', paywall and OCR caveats to the user; never present extracted text as authoritative.
"""

TABLE_INSTRUCTIONS = """\
Table database (design values, parameters, limits, coefficients that live in tables of the standards): when the user asks for a NUMBER or a
row-type fact ("allowable stress for A572", "damping for welded steel"), do not search the graph text: call lookup_values(question) (by meaning),
then query_table / describe_table for exact filters, columns and units, and join_tables when the answer spans two tables of one document.
get_data_dictionary / get_relationship_map explain a document's tables. Always relay `warnings`, `paywalled`, inferred-unit notes and the disclaimer;
quote the source document, page and caption. Tailor deduction per task: set_session_preferences (or `profile`/`approach`/`units` on a call);
use strict for compliance-grade answers, exploratory/broad to brainstorm. Prompts: find_design_value, strict_design_lookup.
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
# Operations: per-credential rate limit and Prometheus-style metrics
# ---------------------------------------------------------------------------


class Ops:
    """Outermost pure-ASGI layer.

    * rate limit: a token bucket per credential (the hash of the Authorization / X-API-Key header, or the client address when there is none), so one
      client cannot starve the others. ``rate_per_min`` requests per minute with a burst of ``burst``; 0 turns it off. ``/healthz`` is never limited.
      The bucket lives in this process: with W workers a client can get up to W times the rate.
    * ``/metrics`` (when ``metrics`` is on): Prometheus text for **this process**. It needs a valid token (open only on a ``--no-auth`` loopback server).
      With several workers behind one port each scrape reaches one of them; give every worker its own port, or aggregate, if you need exact totals.
    """

    def __init__(self, app, tokens: Optional["TokenStore"] = None, *, rate_per_min: float = 0.0, burst: Optional[float] = None, metrics: bool = False,
                 store: Optional[Store] = None, tables: Any = None, clock: Callable[[], float] = time.monotonic):
        self.app, self.tokens, self.metrics, self.store, self.tables, self.clock = app, tokens, metrics, store, tables, clock
        self.rate = max(0.0, float(rate_per_min)) / 60.0
        self.burst = float(burst) if burst else max(5.0, float(rate_per_min) / 6.0)
        self._buckets: dict[str, tuple[float, float]] = {}            # key -> (tokens, last refill)
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests: dict[str, int] = {}
        self.seconds_sum = 0.0
        self.seconds_count = 0
        self.rate_limited = 0
        self.in_flight = 0

    @staticmethod
    def _key(scope, headers: dict[str, str]) -> str:
        cred = headers.get("authorization") or headers.get("x-api-key") or ""
        if cred:
            return "c:" + hashlib.sha256(cred.encode()).hexdigest()[:16]
        return "ip:" + str((scope.get("client") or ["?"])[0])

    def allow(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds until a request would be allowed)."""
        if not self.rate:
            return True, 0.0
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                if len(self._buckets) > 10000:                        # forget idle keys
                    for k in [k for k, (t, ts) in self._buckets.items() if now - ts > 600]:
                        self._buckets.pop(k, None)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1.0 - tokens) / self.rate

    def render(self) -> str:
        pid = os.getpid()
        lab = f'pid="{pid}"'
        out = ["# HELP cogent_http_requests_total HTTP responses by status code (this process).", "# TYPE cogent_http_requests_total counter"]
        out += [f'cogent_http_requests_total{{{lab},status="{st}"}} {n}' for st, n in sorted(self.requests.items())]
        out += ["# TYPE cogent_http_request_seconds_sum counter", f"cogent_http_request_seconds_sum{{{lab}}} {self.seconds_sum:.6f}",
                "# TYPE cogent_http_request_seconds_count counter", f"cogent_http_request_seconds_count{{{lab}}} {self.seconds_count}",
                "# TYPE cogent_rate_limited_total counter", f"cogent_rate_limited_total{{{lab}}} {self.rate_limited}",
                "# TYPE cogent_http_in_flight gauge", f"cogent_http_in_flight{{{lab}}} {self.in_flight}",
                "# TYPE cogent_uptime_seconds gauge", f"cogent_uptime_seconds{{{lab}}} {time.time() - self.started:.0f}"]
        if self.store is not None:
            out += ["# TYPE cogent_graph_cache_events_total counter"]
            out += [f'cogent_graph_cache_events_total{{{lab},event="{k}"}} {v}' for k, v in self.store.cache_stats.items()]
            out += ["# TYPE cogent_graph_cache_graphs gauge", f"cogent_graph_cache_graphs{{{lab}}} {len(self.store._cache)}"]
        if self.tables is not None:
            out += ["# TYPE cogent_embed_events_total counter"]
            out += [f'cogent_embed_events_total{{{lab},event="{k}"}} {v}' for k, v in self.tables.embed_stats.items()]
            try:
                out += ["# TYPE cogent_sessions gauge", f"cogent_sessions{{{lab},store=\"{self.tables.sessions.kind}\"}} {self.tables.sessions.count()}"]
            except Exception:                                                  # noqa: BLE001 - a broken session store must not break the scrape
                out += ["# cogent_sessions unavailable"]
        return "\n".join(out) + "\n"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if path == "/metrics" and self.metrics:
            cred = headers.get("authorization", "")
            token = cred[7:].strip() if cred[:7].lower() == "bearer " else headers.get("x-api-key", "").strip()
            if self.tokens is not None and not (token and self.tokens.verify(token)):
                return await _plain(send, 401, "unauthorized: send a valid token to read /metrics")
            return await _plain(send, 200, self.render(), "text/plain; version=0.0.4")
        if path != "/healthz":
            ok, wait = self.allow(self._key(scope, headers))
            if not ok:
                self.rate_limited += 1
                self.requests["429"] = self.requests.get("429", 0) + 1
                return await _plain(send, 429, "rate limit exceeded; slow down", extra=[(b"retry-after", str(max(1, int(wait + 0.999))).encode())])
        status = {"code": 0}
        t0 = time.monotonic()

        async def tap(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)
        self.in_flight += 1
        try:
            return await self.app(scope, receive, tap)
        finally:
            self.in_flight -= 1
            self.seconds_sum += time.monotonic() - t0
            self.seconds_count += 1
            k = str(status["code"] or 500)
            self.requests[k] = self.requests.get(k, 0) + 1


async def _plain(send, status: int, text: str, ctype: str = "text/plain; charset=utf-8", extra: Optional[list] = None) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())] + (extra or [])})
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


_STARTED = time.time()
_PRIVATE_ROOTS: list[tuple[str, str]] = []          # (absolute path, public replacement), longest first; set by create_server


def set_private_roots(home: Path) -> None:
    """Local file-system locations that must not appear in answers: the agent home (relative ``local:`` / ``<agent-home>``) and the user's home."""
    h = Path(home).resolve()
    roots = [(f"file://{h / 'local'}/", "local:"), (f"file://{h}/", "<agent-home>/"), (str(h / "local") + "/", "local:"),
             (str(h), "<agent-home>"), (str(Path.home().resolve()), "~")]
    _PRIVATE_ROOTS[:] = sorted({r for r in roots if r[0] not in ("", "/", "file:///")}, key=lambda r: -len(r[0]))


_FILE_URL = re.compile(r"file://(?:/[^\s\"')\]]*)?/([^/\s\"')\]]+)")


def scrub(obj: Any) -> Any:
    """Replace absolute local paths in every string of a result: no user name or directory layout leaves the server (``file:///a/b/x.pdf`` -> ``local:x.pdf``)."""
    if isinstance(obj, str):
        for src, dst in _PRIVATE_ROOTS:
            if src in obj:
                obj = obj.replace(src, dst)
        return _FILE_URL.sub(lambda m: "local:" + m.group(1), obj) if "file://" in obj else obj
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def _tool_errors(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def inner(*a, **kw):
        try:
            return scrub(fn(*a, **kw))
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None
    return inner


def create_server(store: Store, *, host: str = "127.0.0.1", port: int = 8765, public_hosts: Optional[list[str]] = None,
                  stateless: bool = True, oauth: Any = None, issuer_url: Optional[str] = None, tables: Any = None) -> FastMCP:
    set_private_roots(store.home)
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
    mcp = FastMCP("cogent-kg", instructions=INSTRUCTIONS + ("\n" + TABLE_INSTRUCTIONS if tables is not None and tables.has_db else ""), host=host, port=port, stateless_http=stateless, json_response=True, **auth_kw,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection="*" not in (public_hosts or []), allowed_hosts=hosts,
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
        """Start here: how many entries/graphs exist, version-status counts, open warnings, the last agent run with its itemised errors (`run_errors`,
        `document_errors`), disclaimers, and whether the table database is reachable."""
        out = store.overview()
        out["table_database"] = tables.health() if tables is not None else {"configured": False}
        return out

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
        warn = store.outline_warnings(ref)
        return wrap(ref, {"outline": store.outline(ref, max_items),
                          **({"order_warnings": warn, "order_note": "Section numbers are out of sequence: the page columns may have been read in the wrong order "
                                                                    "(or the numbering restarts, e.g. in an appendix). Check the source before relying on the order."} if warn else {})})

    @mcp.tool(annotations=RO)
    @_tool_errors
    def search_graph(ref: str, query: str, node_types: Optional[list[str]] = None, limit: int = 20,
                     match: Literal["all", "any"] = "all") -> dict[str, Any]:
        """Keyword search over node labels/text. match='all' (default): every term must occur in one node; match='any': at least one,
        best matches first. Hyphens, slashes and underscores count as spaces. node_types e.g. ['Paragraph','Heading','Table',
        'Figure','Entity','Identifier']. An empty result lists how many nodes contain each term. When an entry has several documents (e.g. the eCFR text and the
        Federal Register rule), results carry `source_role`, codified regulation text ranks first, and a paragraph repeated in another document is
        shown once with the other node ids under `also_in`."""
        rep: dict[str, Any] = {}
        matches = store.search(ref, query, node_types, limit, match, rep)
        extra: dict[str, Any] = {}
        if not matches and len(rep.get("term_counts", {})) > 1:
            extra = {"term_counts": rep["term_counts"], "note": "no single node contains all the terms; the counts show how many nodes contain each term. Try match='any' or fewer terms."}
        return wrap(ref, {"matches": matches, **extra})

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
    def get_table(ref: str, node_id: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """Rows of a Table node, `limit` rows at a time (default 200; `total_rows`, `truncated` and `next_offset` say whether more remain)."""
        return wrap(ref, store.table(ref, node_id, offset, limit))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def list_assets(ref: str, kind: Optional[Literal["figure", "table"]] = None) -> dict[str, Any]:
        """Figures (PNG) and tables (CSV) exported from the parsed documents of an entry (or one document id), with page,
        caption and size. Documents from paywalled / purchased content carry `warnings`, and their figures are served
        watermarked as reference-only copies."""
        return wrap(ref, store.list_assets(ref, kind))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_figure(ref: str, asset_id: str, max_px: int = 1600) -> CallToolResult:
        """One figure as an image (long edge <= max_px). Figures from paywalled content are re-rendered with a diagonal
        'reference only' watermark and a copyright banner; read the accompanying JSON `warnings` and relay them."""
        r = store.figure(ref, asset_id, max_px)
        return CallToolResult(content=[
            TextContent(type="text", text=json.dumps({**r["info"], "provenance": provenance(ref)})),
            ImageContent(type="image", data=base64.b64encode(r["png"]).decode(), mimeType="image/png")])

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_table_csv(ref: str, asset_id: str, max_rows: int = 500) -> dict[str, Any]:
        """One extracted table as CSV text (first max_rows lines). `warnings`/`paywalled` flag copyrighted content."""
        return wrap(ref, store.table_csv(ref, asset_id, max_rows))

    # ---- tailoring: per session, per task / prompt ---------------------------------------------------------------------
    svc = tables if tables is not None else TableService(store, None)           # file-based tools work without a database

    def skey(ctx: Context, session_id: Optional[str]) -> str:
        """Session = the MCP session when the server is stateful, else the authenticated token + the client's own ``session_id``."""
        who, sid = "anonymous", None
        try:
            req = ctx.request_context.request
            if req is not None:
                sid = req.headers.get("mcp-session-id")
                who = getattr(req.state, "token_name", None) or who
        except Exception:                                                        # noqa: BLE001
            pass
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token
            at = get_access_token()
            if at is not None:
                who = at.subject or at.client_id or who
        except Exception:                                                        # noqa: BLE001
            pass
        return f"{who}:{session_id or sid or 'default'}"

    def prefs_for(ctx: Context, session_id: Optional[str], profile: Optional[str], **overrides: Any):
        return svc.resolve(skey(ctx, session_id), profile, **overrides)

    @mcp.tool(annotations=RO)
    @_tool_errors
    def list_profiles() -> dict[str, Any]:
        """The named tailoring profiles (fast, balanced, strict, exploratory, llm) and what the options mean: join `approach`
        (deterministic | llm | hybrid | consensus), `units` (strict = narrow | broad = diverse | off), `retrieval` (semantic | lexical | hybrid)."""
        return svc.profiles()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    @_tool_errors
    def set_session_preferences(ctx: Context, profile: Optional[str] = None, approach: Optional[Literal["deterministic", "llm", "hybrid", "consensus"]] = None,
                                min_score: Optional[float] = None, include_rejected: Optional[bool] = None, min_agreement: Optional[float] = None,
                                units: Optional[Literal["strict", "broad", "off"]] = None, retrieval: Optional[Literal["semantic", "lexical", "hybrid"]] = None,
                                session_id: Optional[str] = None) -> dict[str, Any]:
        """Set how join inference, unit deduction and retrieval behave for the rest of this session (saved server-side for 8 hours, in memory).
        Start from a `profile` and/or set options individually; every later tool call can still override them (arguments of the call, or a
        `profile` named in the call). Pass a stable `session_id` if several conversations share one token. Does not change any stored data."""
        out = svc.set_session(skey(ctx, session_id), profile, approach=approach, min_score=min_score, include_rejected=include_rejected,
                              min_agreement=min_agreement, units=units, retrieval=retrieval)
        if not session_id:
            out["warning"] = ("saved for every conversation that uses this token without a session_id (key '%s'); pass a unique session_id here and in later "
                              "calls to keep this conversation's settings separate, or run the server with --stateful" % out["session"])
        return out

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_session_preferences(ctx: Context, session_id: Optional[str] = None) -> dict[str, Any]:
        """The effective preferences of this session and what the session saved."""
        key = skey(ctx, session_id)
        return {"session": key, "saved": svc.sessions.get(key), "effective": asdict(svc.resolve(key)), "server_default_profile": svc.default_profile}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    @_tool_errors
    def reset_session_preferences(ctx: Context, session_id: Optional[str] = None) -> dict[str, Any]:
        """Forget this session's saved preferences (back to the server default profile)."""
        return svc.reset_session(skey(ctx, session_id))

    # ---- data dictionaries, relationship maps, join evaluation (published files; no database needed) ----------------------
    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_data_dictionary(ctx: Context, ref: str, format: Literal["json", "markdown"] = "json", units: Optional[Literal["strict", "broad", "off"]] = None,
                            approach: Optional[Literal["deterministic", "llm", "hybrid", "consensus"]] = None, min_score: Optional[float] = None,
                            include_rejected: Optional[bool] = None, profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
        """Data dictionary of the tables extracted from the documents of an entry ('standard:22') or one document id: every table (caption,
        page, section), its columns (original header, type, unit with provenance, distinct values, key candidate, samples) and the columns it can be
        joined on. `units` and the join options follow the session / profile unless given here (units=broad adds inferred units with
        alternatives; approach=consensus keeps only joins confirmed by both methods and the judge). Machine-inferred; check against the source."""
        p = prefs_for(ctx, session_id, profile, units=units, approach=approach, min_score=min_score, include_rejected=include_rejected)
        return wrap(ref, svc.data_dictionary(ref, format, p))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_relationship_map(ctx: Context, ref: str, format: Literal["json", "mermaid"] = "json", approach: Optional[Literal["deterministic", "llm", "hybrid", "consensus"]] = None,
                             min_score: Optional[float] = None, include_rejected: Optional[bool] = None, min_agreement: Optional[float] = None,
                             profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
        """Inferred joins between the tables of each document: matching columns, cardinality, score and evidence (names, shared values, KG, LLM
        rationale, judge verdict), as JSON or a Mermaid ER diagram. Choose the approach per task: deterministic (heuristics only), llm, hybrid
        (default), consensus (strictest); include_rejected adds what the judge rejected, flagged."""
        p = prefs_for(ctx, session_id, profile, approach=approach, min_score=min_score, include_rejected=include_rejected, min_agreement=min_agreement)
        return wrap(ref, svc.relationship_map(ref, format, p))

    @mcp.tool(annotations=RO)
    @_tool_errors
    def get_join_evaluation(ref: str, format: Literal["json", "markdown"] = "json") -> dict[str, Any]:
        """The LLM-as-judge evaluation of the join candidates of each document: every candidate (deterministic, LLM-suggested or both) with the
        judge's votes, reasons and problems, agreement between methods and the consistency statistics. The judge is a model, not ground truth."""
        return wrap(ref, svc.join_evaluation(ref, format))

    @mcp.resource("kg://tables/{docid}/dictionary", mime_type="text/markdown")
    def r_dictionary(docid: str) -> str:
        try:
            return "\n\n".join(d["content"] for d in svc.data_dictionary(docid, "markdown", svc.resolve("resource"))["documents"])
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    @mcp.resource("kg://tables/{docid}/relationships", mime_type="text/plain")
    def r_relationships(docid: str) -> str:
        try:
            return "\n\n".join(d["content"] for d in svc.relationship_map(docid, "mermaid", svc.resolve("resource"))["documents"])
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    @mcp.resource("kg://tables/{docid}/join-evaluation", mime_type="text/markdown")
    def r_join_eval(docid: str) -> str:
        try:
            return "\n\n".join(d["content"] for d in svc.join_evaluation(docid, "markdown")["documents"])
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    # ---- per-task prompts: ready-made workflows that carry the right options ---------------------------------------------
    @mcp.prompt(name="find_design_value", description="Look up a design value / parameter in the standards' tables, with sources and units.")
    def p_find_value(question: str, ref: str = "") -> str:
        scope = f" in {ref}" if ref else ""
        return (f"Find the design value asked for{scope}: {question}\n\n1. Call lookup_values(question, retrieval='hybrid'{', ref=' + repr(ref) if ref else ''}) and read the top rows with their table captions.\n"
                "2. Confirm the exact row with query_table (exact filters on the row labels) and quote the ORIGINAL cell text, never a rounded number.\n"
                "3. State the unit and where it came from (units='strict' unless the table has no explicit unit; then try units='broad' and say it is inferred, with the alternatives).\n"
                "4. Always report the source document, page and table caption, and relay every warning (paywalled content, new editions) and the extraction disclaimer.")

    @mcp.prompt(name="strict_design_lookup", description="Conservative lookup: narrow units, strict joins, no inferred values presented as fact.")
    def p_strict(question: str, ref: str = "") -> str:
        return (f"Answer using ONLY well-supported evidence{' from ' + ref if ref else ''}: {question}\n\nFirst call set_session_preferences(profile='strict'). Then lookup_values and query_table. "
                "If a unit is not explicit or declared (units='strict' returns none), say the unit is not stated rather than guessing. Use join_tables only when the relationship is "
                "'consensus'. Cite document, page and caption; relay all warnings.")

    @mcp.prompt(name="explore_table_relationships", description="Review how the tables of a document relate, including disputed and rejected join candidates.")
    def p_explore(ref: str) -> str:
        return (f"Explore the relationships between the tables of {ref}.\n\n1. get_relationship_map(ref, profile='exploratory', format='mermaid') for the overview.\n"
                "2. get_join_evaluation(ref) and list candidates with status 'disputed', 'rejected' or 'uncertain' with the judge's reasons.\n"
                "3. Compare approach='deterministic' with approach='llm': which joins does only one method find? Say how consistent the LLM runs and the judge were.\n"
                "4. Recommend which joins to trust; remember the judge is a model, not ground truth.")

    @mcp.prompt(name="audit_units", description="Check where the units of a table's numeric columns come from and where strict and broad deduction disagree.")
    def p_units(table_id: str) -> str:
        return (f"Audit the units of table {table_id}.\n\nCall explain_units(table_id) and for each numeric column report the strict answer, the broad answer, the evidence and the alternatives. "
                "Flag columns where the unit is only inferred or where declarations conflict, and say which source text to check.")

    # ---- query front-end of the table database (PostgreSQL + pgvector), registered when the server has a database ---------
    if tables is not None and tables.has_db:
        @mcp.tool(annotations=RO)
        @_tool_errors
        def list_tables(ref: Optional[str] = None, query: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
            """Tables loaded in the table database (extracted from the documents' figures-and-tables export): caption, page, section,
            size and source document. `ref` limits to an entry or document; `query` filters by caption / section / text."""
            return tables.list_tables(ref, query, limit)

        @mcp.tool(annotations=RO)
        @_tool_errors
        def describe_table(ctx: Context, table_id: str, sample_rows: int = 5, units: Optional[Literal["strict", "broad", "off"]] = None,
                           profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
            """One table: caption, section, notes, KG entities, every column (header, type, unit with provenance, distinct values, range) and sample
            rows. `units`: strict (narrow: explicit / declared only), broad (also inferred, with alternatives and confidence) or off."""
            return tables.describe_table(table_id, sample_rows, prefs_for(ctx, session_id, profile, units=units))

        @mcp.tool(annotations=RO)
        @_tool_errors
        def explain_units(ctx: Context, table_id: str, column: Optional[str] = None, profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
            """Why a numeric column has the unit it has: every hypothesis (header, cells, caption / notes declarations, section text, KG entities, the
            document's dominant unit, the customary unit of the quantity) with its evidence, and what strict / broad / off answer."""
            return tables.explain_units(table_id, column, prefs_for(ctx, session_id, profile))

        @mcp.tool(annotations=RO)
        @_tool_errors
        def search_tables(ctx: Context, question: str, ref: Optional[str] = None, k: int = 5, retrieval: Optional[Literal["semantic", "lexical", "hybrid"]] = None,
                          profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
            """Find the tables relevant to a question (semantic search over table captions, sections, columns and notes). `retrieval`: semantic,
            lexical (exact words / codes) or hybrid (both, fused)."""
            return tables.search_tables(question, ref, k, prefs_for(ctx, session_id, profile, retrieval=retrieval))

        @mcp.tool(annotations=RO)
        @_tool_errors
        def lookup_values(ctx: Context, question: str, ref: Optional[str] = None, table_ids: Optional[list[str]] = None, k: int = 10, candidate_tables: int = 5,
                          retrieval: Optional[Literal["semantic", "lexical", "hybrid"]] = None, units: Optional[Literal["strict", "broad", "off"]] = None,
                          profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
            """Look up design values / parameters in the tables: finds the best tables, then the best rows (rows are embedded whole, numbers included),
            and returns them with their original cell text, source document, page, caption and the units of the numeric columns (per `units`: strict
            = narrow, broad = diverse with inference notes, off). Use query_table for exact filters."""
            return tables.lookup_values(question, ref, table_ids, k, candidate_tables, prefs_for(ctx, session_id, profile, retrieval=retrieval, units=units))

        @mcp.tool(annotations=RO)
        @_tool_errors
        def query_table(ctx: Context, table_id: str, where: Optional[list[dict[str, Any]]] = None, columns: Optional[list[str]] = None, order_by: Optional[str] = None,
                        descending: bool = False, limit: int = 20, units: Optional[Literal["strict", "broad", "off"]] = None, profile: Optional[str] = None,
                        session_id: Optional[str] = None) -> dict[str, Any]:
            """Exact filtering of one table. `where` = [{"column": "Damping (%)", "op": ">=", "value": 5}]; ops: = != < <= > >= like in between
            ([low, high]) is_null not_null. Columns may be named by original header or database name. Numeric columns compare numerically; the answer
            lists the units of the numeric columns per `units`."""
            return tables.query_table(table_id, where, columns, order_by, descending, limit, prefs_for(ctx, session_id, profile, units=units))

        @mcp.tool(annotations=RO)
        @_tool_errors
        def join_tables(ctx: Context, left_table: str, right_table: str, left_column: Optional[str] = None, right_column: Optional[str] = None, limit: int = 20,
                        approach: Optional[Literal["deterministic", "llm", "hybrid", "consensus"]] = None, min_score: Optional[float] = None,
                        include_rejected: Optional[bool] = None, profile: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
            """Join two tables of one document. Without columns the inferred relationship is used, chosen by `approach` (deterministic | llm |
            hybrid | consensus) and `min_score`; the answer says how the join was established (methods, score, evidence)."""
            return tables.join_tables(left_table, right_table, left_column, right_column, limit,
                                      prefs_for(ctx, session_id, profile, approach=approach, min_score=min_score, include_rejected=include_rejected))

        if tables.llm_model:
            @mcp.tool(annotations=RO)
            @_tool_errors
            def evaluate_joins(ctx: Context, ref: str, approach: Optional[Literal["deterministic", "llm", "hybrid", "consensus"]] = None, llm_samples: int = 2, judge_runs: int = 3,
                               max_pairs: int = 20, suggester_model: Optional[str] = None, judge_model: Optional[str] = None, max_seconds: Optional[float] = None,
                               min_score: Optional[float] = None, include_rejected: Optional[bool] = None, profile: Optional[str] = None,
                               session_id: Optional[str] = None) -> dict[str, Any]:
                """Run LLM join inference + LLM-as-judge NOW for the documents of `ref` (at most 3 documents; seconds to minutes) with parameters of your
                choice: number of independent suggestion runs, judge votes, table pairs, models. Returns the relationships for the approach plus the
                evaluation statistics (self-consistency of the LLM, judge consistency, agreement with the deterministic joins). Not stored."""
                return tables.evaluate_joins(ref, prefs_for(ctx, session_id, profile, approach=approach, min_score=min_score, include_rejected=include_rejected),
                                             llm_samples, judge_runs, max_pairs, suggester_model, judge_model, max_seconds)

        if tables.allow_sql:
            @mcp.tool(annotations=RO)
            @_tool_errors
            def run_sql(query: str, limit: int = 100) -> dict[str, Any]:
                """A single read-only SELECT against the table database (schema kgtables: kgt_tables, kgt_columns, kgt_joins and one table
                per extracted table). Only enabled by the operator."""
                return tables.run_sql(query, limit)

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

    @mcp.resource("kg://figure/{docid}/{asset}", mime_type="image/png")
    def r_figure(docid: str, asset: str) -> bytes:
        try:
            return store.figure(docid, f"{docid}/{asset}")["png"]
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None

    @mcp.resource("kg://table/{docid}/{asset}", mime_type="text/csv")
    def r_table(docid: str, asset: str) -> str:
        try:
            r = store.table_csv(docid, f"{docid}/{asset}")
        except NotFound as exc:
            raise ValueError(str(exc.args[0])) from None
        head = "".join(f"# WARNING: {w}\n" for w in r["warnings"]) if r["paywalled"] else ""
        return head + r["csv"]

    @mcp.resource("kg://disclaimers", mime_type="text/plain")
    def r_disclaimers() -> str:
        return "\n".join(f"- {d}" for d in GENERAL_DISCLAIMERS)

    @mcp.resource("kg://{kind}/{ident}/metadata", mime_type="application/json")
    def r_meta(kind: str, ident: str) -> str:
        try:
            return json.dumps(scrub(store.entry_metadata(f"{kind}:{ident}")), indent=1)
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
        return JSONResponse({"status": "ok", "agent_last_run": (run or {}).get("finished"), "pid": os.getpid(), "uptime_s": int(time.time() - _STARTED)})

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
# Several worker processes on one port
# ---------------------------------------------------------------------------


def run_workers(n: int, host: str, port: int, make_app: Callable[[], Any], restart_limit: int = 5) -> int:
    """Bind the port once, fork ``n`` workers that each build their own app and serve on the shared socket, and supervise them: a worker that dies
    is restarted (at most ``restart_limit`` times per minute, then the supervisor gives up); SIGINT / SIGTERM stop everything."""
    import multiprocessing
    import signal
    import socket
    import uvicorn
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1024)
    sock.set_inheritable(True)
    ctx = multiprocessing.get_context("fork")

    def worker() -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        app = make_app()
        uvicorn.Server(uvicorn.Config(app, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")).run(sockets=[sock])

    procs = [ctx.Process(target=worker, daemon=True) for _ in range(n)]
    for p in procs:
        p.start()
    log.info("%d worker process(es) started: %s", n, ", ".join(str(p.pid) for p in procs))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    deaths: list[float] = []
    while not stop.wait(1.0):
        for i, p in enumerate(procs):
            if not p.is_alive():
                now = time.time()
                deaths = [t for t in deaths if now - t < 60] + [now]
                log.error("worker %s exited with code %s", p.pid, p.exitcode)
                if len(deaths) > restart_limit:
                    log.error("workers keep dying (%d in a minute); stopping", len(deaths))
                    stop.set()
                    break
                procs[i] = ctx.Process(target=worker, daemon=True)
                procs[i].start()
                log.info("restarted as %s", procs[i].pid)
    for p in procs:
        if p.is_alive():
            p.terminate()
    for p in procs:
        p.join(10)
    sock.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path(_env("COGENT_HOME", "kg_agent_home")), help="agent home directory")
    ap.add_argument("--tokens", type=Path, default=None, help="token file (default <state-dir>/tokens.json)")
    ap.add_argument("--state-dir", type=Path, default=None, help="where tokens.json and oauth.json live (default <home>/mcp); put it on storage "
                    "shared by all servers when several hosts serve the same tokens")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--standards", type=Path, default=Path(_env("COGENT_STANDARDS", "config/standards.json")))
    s.add_argument("--reports", type=Path, default=Path(_env("COGENT_REPORTS", "config/reports.json")))
    s.add_argument("--host", default="127.0.0.1", help="bind address (keep 127.0.0.1 behind a proxy/tunnel)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--public-host", action="append", default=[], metavar="HOST",
                   help="public host name(s) clients use (Host-header allow-list); repeatable. '*' turns the Host check off "
                   "(for tunnels with random names such as *.trycloudflare.com; authentication still applies)")
    s.add_argument("--stale-after", type=float, default=3.0, help="days before captures are flagged stale")
    s.add_argument("--oauth-issuer", metavar="URL", help="enable OAuth 2.1 (claude.ai custom connectors); the public https "
                   "base URL, e.g. https://kg.example.com")
    s.add_argument("--paywalled-assets", choices=["watermark", "block", "allow"], default="watermark",
                   help="figures from paywalled/purchased content: watermark (default), block, or allow unmarked")
    s.add_argument("--tabledb-dsn", default=os.environ.get("COGENT_TABLEDB_DSN"), help="PostgreSQL DSN of the table database (enables list_tables, "
                   "lookup_values, query_table, ...); use a read-only role. Env: COGENT_TABLEDB_DSN")
    s.add_argument("--tabledb-schema", default="kgtables")
    s.add_argument("--embedder", choices=["ollama", "hash", "none"], default=os.environ.get("COGENT_EMBEDDER", "ollama"),
                   help="embedder for questions (must be the one the database was built with); 'none' = lexical search only")
    s.add_argument("--embed-model", default=None)
    s.add_argument("--ollama-host", default=None)
    s.add_argument("--embed-dim", type=int, default=None, help="dimension for --embedder hash")
    s.add_argument("--llm-model", default=os.environ.get("COGENT_LLM_MODEL"), help="Ollama model that enables evaluate_joins (LLM join inference + judge on request)")
    s.add_argument("--judge-model", default=None, help="Ollama model for the judge (default: the --llm-model)")
    s.add_argument("--llm-max-seconds", type=float, default=180.0, help="time budget of one evaluate_joins call (cap 600)")
    s.add_argument("--default-profile", default="balanced", help="tailoring profile of sessions that set none: fast | balanced | strict | exploratory | llm")
    s.add_argument("--allow-sql", action="store_true", help="also expose run_sql (read-only SELECT); use a SELECT-only database role")
    s.add_argument("--no-auth", action="store_true", help="disable authentication (loopback binds only)")
    s.add_argument("--stateful", action="store_true", help="keep per-client sessions (default: stateless)")
    g = s.add_argument_group("scale-out")
    g.add_argument("--workers", type=int, default=int(_env("COGENT_WORKERS", "1")), help="server processes sharing the port (Linux/macOS; each loads its own graphs, "
                   "so memory grows with it). Default 1")
    g.add_argument("--graph-cache", type=int, default=int(_env("COGENT_GRAPH_CACHE", "64")), help="graphs kept in memory per process, least recently used "
                   "first out (0 = no limit)")
    g.add_argument("--session-store", default=_env("COGENT_SESSION_STORE", "memory"), help="where session preferences live: memory (default, per process) | "
                   "sqlite:/path/sessions.db (shared by the workers of one host) | redis://host:6379/0 (several hosts)")
    g.add_argument("--session-hours", type=float, default=8.0, help="how long saved session preferences last")
    g.add_argument("--embed-cache", type=int, default=512, help="recent question embeddings kept per process (0 = off)")
    g.add_argument("--embed-concurrency", type=int, default=2, help="embedder calls in flight per process")
    g.add_argument("--embed-wait", type=float, default=5.0, help="seconds a request waits for a free embedder slot before it falls back to lexical search")
    g.add_argument("--rate-limit", type=float, default=float(_env("COGENT_RATE_LIMIT", "0")), help="requests per minute allowed per credential (0 = off; per process)")
    g.add_argument("--rate-burst", type=float, default=None, help="burst size of the rate limit (default max(5, limit/6))")
    g.add_argument("--metrics", action="store_true", help="serve Prometheus-style /metrics (needs a valid token)")

    t = sub.add_parser("token", help="manage access tokens")
    ts = t.add_subparsers(dest="tcmd", required=True)
    c = ts.add_parser("create", help="create a token (printed once)")
    c.add_argument("--name", required=True)
    c.add_argument("--days", type=float, default=None, help="expire after N days")
    ts.add_parser("list", help="list tokens (no secrets)")
    r = ts.add_parser("revoke", help="revoke a token by name")
    r.add_argument("--name", required=True)

    a = ap.parse_args(argv)
    state_dir = a.state_dir or a.home / "mcp"
    tokens = TokenStore(a.tokens or state_dir / "tokens.json", os.environ.get("COGENT_MCP_TOKEN"))

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
    if a.workers > 1 and not hasattr(os, "fork"):
        ap.error("--workers > 1 needs fork (Linux/macOS); on this system run several instances on different ports behind a proxy instead")
    if a.workers > 1 and a.session_store == "memory":
        log.warning("--workers %d with the in-memory session store: each worker keeps its own session preferences; use --session-store sqlite:<path> "
                    "to share them", a.workers)

    def runtime():
        """Store, table service and ASGI app of one process (called once per worker, after the fork)."""
        from kg_sessions import make_sessions
        store = Store(a.home, a.standards, a.reports, a.stale_after, a.paywalled_assets, a.graph_cache)
        emb = None
        if a.tabledb_dsn and a.embedder != "none":
            from kg_embed import get_embedder
            emb = get_embedder(a.embedder, a.embed_model, a.ollama_host, a.embed_dim)
        tables = TableService(store, a.tabledb_dsn, a.tabledb_schema, emb, a.allow_sql, a.llm_model, a.judge_model, a.ollama_host, a.llm_max_seconds,
                              a.default_profile, sessions=make_sessions(a.session_store, a.session_hours * 3600), embed_cache=a.embed_cache,
                              embed_concurrency=a.embed_concurrency, embed_wait=a.embed_wait)
        app = build_app(store, None if a.no_auth else tokens, tables=tables, issuer_url=None if a.no_auth else a.oauth_issuer,
                        oauth_path=state_dir / "oauth.json", host=a.host, port=a.port, public_hosts=a.public_host, stateless=not a.stateful)
        if a.rate_limit or a.metrics:
            app = Ops(app, None if a.no_auth else tokens, rate_per_min=a.rate_limit, burst=a.rate_burst, metrics=a.metrics, store=store, tables=tables)
        return app, tables

    app, tables = runtime()
    mode = "OFF" if a.no_auth else f"OAuth 2.1 + bearer token (issuer {a.oauth_issuer})" if a.oauth_issuer else "bearer token"
    if tables.has_db:
        h = tables.health()
        if h.get("reachable") and h.get("can_select"):
            log.info("table database reachable as %s (%s table(s))", h.get("user"), h.get("tables"))
        else:
            log.warning("TABLE DATABASE NOT USABLE: %s. %s The table tools will fail until this is fixed (check with `kg_tabledb.py --dsn <dsn> check`).",
                        h.get("error") or ("no SELECT privilege" if h.get("reachable") else "unknown"), h.get("hint", ""))
    log.info("serving %s on http://%s:%s/mcp (auth %s, %d worker(s), sessions %s, graph cache %s)", a.home, a.host, a.port, mode, a.workers, tables.sessions.kind,
             a.graph_cache or "unlimited")
    if a.workers <= 1:
        uvicorn.run(app, host=a.host, port=a.port, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
        return 0
    return run_workers(a.workers, a.host, a.port, lambda: runtime()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
