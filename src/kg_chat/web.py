"""Flask web UI for the cogent conversational agent (adapted from the 0xA6E07 web app).

    python src/kg_chat_app.py web --mcp-url https://host/mcp           # token from $COGENT_MCP_TOKEN or --token-file

Design (as in 0xA6E07):
  * the vector index is built ONCE at start-up (embedding is the slow part), from what the MCP server serves;
  * each browser session gets its own conversation: a shallow copy of the agent that shares the index/models but has a private ``history``;
  * browser <-> server traffic is JSON; the page is templates/index.html. Answers and tables arrive as Markdown and are rendered by the page
    with DOM calls only (never as HTML), because they derive from untrusted documents.

The MCP token stays on the server. The app has no login of its own, so it binds to 127.0.0.1 and refuses another address unless you say
``--allow-remote``: anyone who can reach this app can read what the token can read.
"""
from __future__ import annotations

import copy
import hmac
import os
import secrets
import threading
from collections import OrderedDict
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request, session

import kg_telemetry as telemetry

from .agent import CogentAgent
from .mcp_conn import McpAuthError, McpError

MAX_MESSAGE_CHARS = 2000   # reject absurdly long questions
MAX_SESSIONS = 100         # oldest idle conversations are dropped beyond this


class ConversationStore:
    """Per-browser-session conversations built from one shared, indexed agent."""

    def __init__(self, base_agent: CogentAgent, max_sessions: int = MAX_SESSIONS):
        self._base, self._max = base_agent, max_sessions
        self._items: "OrderedDict[str, tuple[CogentAgent, threading.Lock]]" = OrderedDict()   # LRU eviction
        self._guard = threading.Lock()

    def get(self, sid: str) -> tuple[CogentAgent, threading.Lock]:
        with self._guard:
            if sid not in self._items:
                convo = copy.copy(self._base)            # shares index, models and chains...
                convo.history = []                       # ...but not the conversation memory
                self._items[sid] = (convo, threading.Lock())
                while len(self._items) > self._max:
                    self._items.popitem(last=False)
            self._items.move_to_end(sid)
            telemetry.set_chat_gauges(conversations=len(self._items))
            return self._items[sid]

    def reset(self, sid: str) -> None:
        with self._guard:
            self._items.pop(sid, None)
            telemetry.set_chat_gauges(conversations=len(self._items))


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def create_app(agent: CogentAgent, title: str = "", disclaimer: str = "", metrics: bool = False, metrics_token: Optional[str] = None) -> Flask:
    """Build the Flask app around an already-indexed agent (injectable for tests).

    ``metrics``: serve the Prometheus text of this process on ``/metrics`` (the chat client's LLM usage, questions, MCP calls). It needs
    ``Authorization: Bearer <metrics_token>`` when a token is given."""
    app = Flask(__name__)
    # Signs the session cookie. Set FLASK_SECRET_KEY to keep sessions valid across restarts.
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    store = ConversationStore(agent)

    def current_sid() -> str:
        if "sid" not in session:
            session["sid"] = secrets.token_hex(16)
        return session["sid"]

    @app.get("/")
    def index():
        current_sid()
        return render_template("index.html", title=title, chunks=len(agent.chunks), disclaimer=disclaimer, tables=agent.source.has_tables)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "chunks": len(agent.chunks)}

    @app.get("/api/status")
    def status():
        return jsonify(title=title, chunks=len(agent.chunks), table_tools=agent.source.has_tables, chat_model=agent.cfg.chat_model,
                       embed_model=agent.cfg.embed_model, problems=agent.source.problems[:10])

    @app.post("/api/chat")
    def chat():
        # get_json() only parses application/json bodies. Requiring that content type also blocks simple cross-site form posts (basic CSRF protection).
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            return jsonify(error='Send JSON like {"message": "..."}.'), 400
        message = data["message"].strip()
        if not message:
            return jsonify(error="Message is empty."), 400
        if len(message) > MAX_MESSAGE_CHARS:
            return jsonify(error=f"Message is too long (max {MAX_MESSAGE_CHARS} characters)."), 413
        convo, lock = store.get(current_sid())
        try:
            with lock:                                   # one question at a time per conversation keeps history ordered
                reply = convo.command(message) or convo.ask(message)
        except McpAuthError:
            app.logger.error("The MCP server rejected the token")
            return jsonify(error="The MCP server rejected the token this app was started with."), 502
        except Exception:
            # Log details server-side; don't leak internals to the browser.
            app.logger.exception("Agent call failed")
            return jsonify(error="The language model or MCP request failed. Are the model server and the MCP server running?"), 502
        return jsonify(reply.model_dump(mode="json"))

    @app.post("/api/reset")
    def reset():
        store.reset(current_sid())
        return jsonify(ok=True)

    if metrics:
        @app.get("/metrics")
        def prom():
            if metrics_token:
                auth = request.headers.get("Authorization", "")
                given = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
                if not hmac.compare_digest(given.encode(), metrics_token.encode()):
                    return Response('{"error":"unauthorized"}', 401, mimetype="application/json", headers={"WWW-Authenticate": 'Bearer realm="cogent-chat"'})
            return Response(telemetry.metrics().render(), mimetype="text/plain; version=0.0.4")

    return app
