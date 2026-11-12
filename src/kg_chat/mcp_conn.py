"""Token-authenticated connection to the cogent MCP server (streamable HTTP).

Every call opens a short MCP session (``initialize`` then the tool calls), because the server runs stateless by default; ``call_tools`` runs
several calls in one session. The synchronous API suits Flask threads and the terminal client. The token goes only into the
``Authorization: Bearer`` header and is removed from any error text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional, Sequence

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import kg_telemetry as telemetry

log = logging.getLogger("kg_chat.mcp")


class McpError(RuntimeError):
    """The server could not be reached, or answered in a way the client cannot use."""


class McpAuthError(McpError):
    """The server rejected the token (HTTP 401 / 403)."""


class McpToolError(McpError):
    """The tool ran and reported an error (unknown entry, table database not reachable, ...)."""


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for e in exc.exceptions:
            out += _leaves(e)
        return out
    return [exc]


class McpConnection:
    def __init__(self, url: str, token: str, timeout: float = 60.0):
        self.url, self._token, self.timeout = url, token, timeout

    def with_timeout(self, timeout: float) -> "McpConnection":
        return McpConnection(self.url, self._token, timeout)

    # ---- plumbing
    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _translate(self, exc: BaseException) -> McpError:
        leaves = _leaves(exc)
        for e in leaves:
            if isinstance(e, McpError):
                return e
        for e in leaves:
            if isinstance(e, httpx.HTTPStatusError):
                code = e.response.status_code
                if code in (401, 403):
                    return McpAuthError(f"the MCP server rejected the token (HTTP {code}): check the token and that it was not revoked or expired")
                if code == 404:
                    return McpError(f"no MCP endpoint at {self.url} (HTTP 404): does the URL end in /mcp?")
                return McpError(f"the MCP server answered HTTP {code}")
        for e in leaves:
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, httpx.NetworkError)):
                return McpError(f"cannot reach the MCP server at {self.url}: {self._redact(str(e) or type(e).__name__)}")
        first = leaves[0] if leaves else exc
        return McpError(self._redact(f"{type(first).__name__}: {first}"))

    async def _session(self, fn: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        async with streamablehttp_client(self.url, headers=self._headers(), timeout=timedelta(seconds=self.timeout)) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)

    def _run(self, fn: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        if not self._token.strip():
            raise McpAuthError("no MCP token: the server requires one (Authorization: Bearer ...)")
        try:
            return asyncio.run(self._session(fn))
        except McpError:
            raise
        except BaseException as exc:                      # includes ExceptionGroup from the anyio task groups of the transport
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            err = self._translate(exc)
            telemetry.record_chat_mcp("connect", "auth" if isinstance(err, McpAuthError) else "error", 0.0)
            raise err from None

    @staticmethod
    async def _call(s: ClientSession, name: str, args: Optional[dict[str, Any]]) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            res = await s.call_tool(name, args or {})
            texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
            if res.isError:
                raise McpToolError(texts[0] if texts else f"tool {name} failed")
        except McpToolError:
            telemetry.record_chat_mcp(name, "error", time.perf_counter() - t0)
            raise
        telemetry.record_chat_mcp(name, "ok", time.perf_counter() - t0)
        if isinstance(res.structuredContent, dict):
            return res.structuredContent
        if texts:
            try:
                data = json.loads(texts[0])
                return data if isinstance(data, dict) else {"result": data}
            except ValueError:
                return {"text": texts[0]}
        return {}

    # ---- API
    def list_tools(self) -> set[str]:
        async def go(s: ClientSession) -> set[str]:
            return {t.name for t in (await s.list_tools()).tools}
        return self._run(go)

    def list_tool_details(self) -> list[dict[str, Any]]:
        """Name, description and the read-only annotation of every tool the server offers."""
        async def go(s: ClientSession) -> list[dict[str, Any]]:
            out = []
            for t in (await s.list_tools()).tools:
                ann = getattr(t, "annotations", None)
                out.append({"name": t.name, "description": (t.description or "").strip(), "read_only": getattr(ann, "readOnlyHint", None) if ann else None})
            return out
        return self._run(go)

    def call_tool(self, name: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._run(lambda s: self._call(s, name, args))

    def call_tools(self, calls: Sequence[tuple[str, Optional[dict[str, Any]]]]) -> list[Any]:
        """Several tool calls in one session. A failing call yields its ``McpToolError`` in place of a result; the others still run."""
        async def go(s: ClientSession) -> list[Any]:
            out: list[Any] = []
            for name, args in calls:
                try:
                    out.append(await self._call(s, name, args))
                except McpToolError as exc:
                    out.append(exc)
            return out
        return self._run(go)
