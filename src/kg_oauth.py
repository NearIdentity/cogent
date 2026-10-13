"""Minimal OAuth 2.1 authorization server for the MCP service (what claude.ai custom connectors expect).

Endpoints come from the MCP SDK (``/.well-known/oauth-*``, ``/register`` dynamic client registration, ``/authorize``,
``/token``, ``/revoke``); this module supplies the storage and the one human step: the **consent page**, where the
operator approves a client by entering one of the access tokens created with ``kg_mcp.py token create``.
Authorization code + PKCE (S256, enforced by the SDK). Access tokens live 1 h, refresh tokens 30 d; only hashes are stored.
Static bearer tokens keep working on ``/mcp`` and revoking one also kills the OAuth tokens approved with it.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "kg:read"
ACCESS_TTL, REFRESH_TTL, CODE_TTL, PENDING_TTL = 3600, 30 * 86400, 300, 600
MAX_CLIENTS = 200
MAX_FAILS, FAIL_WINDOW = 5, 300


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class CogentOAuthProvider:
    def __init__(self, path: Path, tokens, issuer_url: str):
        self.path, self.tokens, self.issuer = Path(path), tokens, issuer_url.rstrip("/")
        self._lock = threading.Lock()
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}
        self._fails: dict[str, list[float]] = {}
        try:
            self.db = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.db = {}
        for k in ("clients", "access", "refresh"):
            self.db.setdefault(k, {})

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            self.db[kind] = {k: v for k, v in self.db[kind].items() if v["expires_at"] > now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.db, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ----------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        raw = self.db["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            if len(self.db["clients"]) >= MAX_CLIENTS:
                raise ValueError("too many registered clients")
            self.db["clients"][client_info.client_id] = client_info.model_dump(mode="json")
            self._save()

    # --------------------------------------------------------- authorize / code
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
            if len(self._pending) > 500:
                raise ValueError("too many pending authorizations")
            pid = secrets.token_urlsafe(24)
            self._pending[pid] = (client, params, now + PENDING_TTL)
        return f"{self.issuer}/consent?pid={pid}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> Optional[AuthorizationCode]:
        c = self._codes.get(authorization_code)
        return c if c and c.client_id == client.client_id and c.expires_at > time.time() else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:       # single use
            raise ValueError("authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject, authorization_code.resource)

    def _issue(self, client_id: str, scopes: list[str], subject: Optional[str], resource: Optional[str]) -> OAuthToken:
        at, rt, now = "cga_" + secrets.token_urlsafe(32), "cgr_" + secrets.token_urlsafe(32), time.time()
        with self._lock:
            self.db["access"][_h(at)] = {"client_id": client_id, "scopes": scopes, "subject": subject, "resource": resource,
                                         "expires_at": now + ACCESS_TTL}
            self.db["refresh"][_h(rt)] = {"client_id": client_id, "scopes": scopes, "subject": subject, "resource": resource,
                                          "expires_at": now + REFRESH_TTL}
            self._save()
        return OAuthToken(access_token=at, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=rt)

    # ----------------------------------------------------------------- refresh
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        r = self.db["refresh"].get(_h(refresh_token))
        if not r or r["client_id"] != client.client_id or r["expires_at"] <= time.time() or not self._subject_ok(r["subject"]):
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=r["scopes"], expires_at=int(r["expires_at"]),
                            resource=r["resource"], subject=r["subject"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self._lock:
            self.db["refresh"].pop(_h(refresh_token.token), None)        # rotate
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject, refresh_token.resource)

    # ------------------------------------------------------------ verification
    def _subject_ok(self, subject: Optional[str]) -> bool:
        return subject is not None and subject in self.tokens.active_names()

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        name = self.tokens.verify(token)                                  # static bearer token
        if name:
            return AccessToken(token=token, client_id=f"static:{name}", scopes=[SCOPE], subject=name)
        r = self.db["access"].get(_h(token))
        if not r or r["expires_at"] <= time.time() or not self._subject_ok(r["subject"]):
            return None
        return AccessToken(token=token, client_id=r["client_id"], scopes=r["scopes"], expires_at=int(r["expires_at"]),
                           resource=r["resource"], subject=r["subject"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            self.db["access"].pop(_h(token.token), None)
            self.db["refresh"].pop(_h(token.token), None)
            self._save()

    # ------------------------------------------------------------ consent page
    def _page(self, pid: str, client: OAuthClientInformationFull, params: AuthorizationParams, error: str = "") -> HTMLResponse:
        e = html.escape
        body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Authorize access</title><style>body{{font:16px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem}}
input,button{{font:inherit;padding:.5rem;margin:.25rem 0}}input{{width:100%;box-sizing:border-box}}.err{{color:#b00}}</style>
<h1>Authorize MCP client</h1>
<p><b>{e(client.client_name or client.client_id)}</b> wants read-only access to the cogent knowledge graphs.</p>
<p>It will be sent back to <code>{e(str(params.redirect_uri))}</code>.</p>
{f'<p class=err>{e(error)}</p>' if error else ''}
<form method=post><input type=hidden name=pid value="{e(pid)}">
<label>Access token (from <code>kg_mcp.py token create</code>)<input type=password name=token autocomplete=off autofocus></label>
<button name=action value=allow>Allow</button> <button name=action value=deny>Deny</button></form>"""
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Content-Security-Policy": "frame-ancestors 'none'; default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
                                           "Referrer-Policy": "no-referrer"})

    async def consent(self, request: Request) -> Response:
        if request.method == "GET":
            pid = request.query_params.get("pid", "")
        else:
            form = await request.form()
            pid = str(form.get("pid", ""))
        item = self._pending.get(pid)
        if not item or item[2] < time.time():
            return HTMLResponse("This authorization request has expired. Start again from the client.", 400)
        client, params, _ = item
        if request.method == "GET":
            return self._page(pid, client, params)
        ip = (request.client.host if request.client else "?")
        now = time.time()
        recent = [t for t in self._fails.get(ip, []) if t > now - FAIL_WINDOW]
        if len(recent) >= MAX_FAILS:
            return HTMLResponse("Too many failed attempts; try again later.", 429)
        redirect = lambda **kw: RedirectResponse(construct_redirect_uri(str(params.redirect_uri), state=params.state, **kw), 302,
                                                 headers={"Cache-Control": "no-store"})
        if form.get("action") == "deny":
            self._pending.pop(pid, None)
            return redirect(error="access_denied", error_description="denied by operator")
        name = self.tokens.verify(str(form.get("token", "")).strip())
        if not name:
            self._fails[ip] = recent + [now]
            return self._page(pid, client, params, "That token is not valid.")
        self._pending.pop(pid, None)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=now + CODE_TTL, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource, subject=name)
        return redirect(code=code)
