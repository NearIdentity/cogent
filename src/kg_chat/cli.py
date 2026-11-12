"""Command-line and web entry points of the cogent chat client."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import SecretStr, ValidationError

try:  # enables line editing/history in input(); not available on every platform
    import readline  # noqa: F401
    _HAS_READLINE = True
except ImportError:
    _HAS_READLINE = False

import kg_telemetry as telemetry

from .agent import CogentAgent
from .mcp_conn import McpConnection, McpError
from .models import ChatReply, ClientConfig
from .source import KnowledgeSource

# ANSI colours: the user's prompt is yellow and the agent's reply is cyan (skipped when output is not a terminal, or NO_COLOR is set).
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
YELLOW, CYAN, RESET = "\033[33m", "\033[36m", "\033[0m"


def _paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _USE_COLOR else text


def read_token(token_file: Optional[Path]) -> str:
    """The MCP token from ``--token-file`` or ``$COGENT_MCP_TOKEN``. Never from the command line (it would show in the process list)."""
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"Error: cannot read the token file: {exc.strerror}")
    tok = os.environ.get("COGENT_MCP_TOKEN", "").strip()
    if not tok:
        raise SystemExit("Error: no MCP token. Put it in $COGENT_MCP_TOKEN or a file given with --token-file (create one with: python src/kg_mcp.py token create --name chat).")
    return tok


def format_reply(reply: ChatReply) -> str:
    """A reply as Markdown text for the terminal (tables stay Markdown tables)."""
    parts = [reply.answer] if reply.answer else []
    parts += [t.markdown for t in reply.tables]
    if reply.notices:
        parts.append("Notes:\n" + "\n".join(f"- {'WARNING: ' if n.level == 'warning' else ''}{n.text}" for n in reply.notices))
    if reply.sources:
        parts.append("sources:\n" + "\n".join(f"  [{s.label}] {s.title}" + (f"  {s.url}" if s.url.startswith(("http://", "https://")) else "") for s in reply.sources))
    return "\n\n".join(parts)


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mcp-url", default=os.environ.get("COGENT_MCP_URL"), help="cogent MCP endpoint, e.g. https://host/mcp (default $COGENT_MCP_URL)")
    p.add_argument("--token-file", type=Path, help="file holding the bearer token (default: $COGENT_MCP_TOKEN)")
    p.add_argument("--model", default=os.environ.get("CHAT_MODEL", "llama3.1"), help="Ollama chat model (must support structured output)")
    p.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"), help="Ollama embedding model")
    p.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    p.add_argument("--entries", help="comma-separated entries to index, e.g. standard:22,report:1 (default: every entry with a graph)")
    p.add_argument("--cache-dir", type=Path, default=Path("chat_cache"), help="graph sections and embeddings are cached here (default chat_cache/); 'none' disables")
    p.add_argument("--top-k", type=int, default=6, help="text chunks retrieved per question")
    p.add_argument("--table-rows", type=int, default=8, help="table rows requested per question")
    p.add_argument("--otlp-endpoint", help="OpenTelemetry OTLP/HTTP endpoint for traces (default $OTEL_EXPORTER_OTLP_ENDPOINT)")


def build_config(a: argparse.Namespace) -> ClientConfig:
    if not a.mcp_url:
        raise SystemExit("Error: no MCP URL. Use --mcp-url https://host/mcp or set $COGENT_MCP_URL.")
    cache = None if str(a.cache_dir).lower() == "none" else a.cache_dir
    try:
        return ClientConfig(mcp_url=a.mcp_url, token=SecretStr(read_token(a.token_file)), chat_model=a.model, embed_model=a.embed_model, ollama_url=a.ollama_url,
                            entries=[e.strip() for e in (a.entries or "").split(",") if e.strip()], cache_dir=cache, top_k=a.top_k, table_rows=a.table_rows)
    except ValidationError as exc:
        raise SystemExit("Error: " + "; ".join(e["msg"] for e in exc.errors()))


def build_agent(cfg: ClientConfig, progress=print) -> CogentAgent:
    if cfg.insecure_remote:
        print("WARNING: the MCP URL is plain http to another host: the token travels in clear text. Use https.", file=sys.stderr)
    conn = McpConnection(cfg.mcp_url, cfg.token.get_secret_value(), cfg.timeout)
    source = KnowledgeSource(conn, cfg)
    try:
        progress(f"Connecting to {cfg.mcp_url} ...")
        tools = source.tools                                    # fails fast on a bad URL or token
        progress(f"Connected: {len(tools)} tool(s); table database {'available' if source.has_tables else 'not available'}.")
        return CogentAgent(cfg, source, progress=progress)
    except (McpError, RuntimeError) as exc:
        raise SystemExit(f"Error: {exc}")


def do_chat(a: argparse.Namespace) -> None:
    agent = build_agent(build_config(a))
    print(f"Ready: {len(agent.chunks)} chunks indexed. Ask a question, or /help (blank line or Ctrl-D to quit).\n")
    while True:
        try:
            q = (input(("\001" + YELLOW + "\002" if _USE_COLOR and _HAS_READLINE else YELLOW if _USE_COLOR else "") + "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        finally:
            if _USE_COLOR:
                sys.stdout.write(RESET)
        if not q:
            break
        try:
            reply = agent.command(q) or agent.ask(q)
        except McpError as exc:
            print(_paint(f"agent> {exc}", CYAN))
            continue
        except Exception as exc:                                # the model server may be down
            print(_paint(f"agent> the request failed ({type(exc).__name__}); are Ollama and the MCP server running?", CYAN))
            continue
        print("\n" + _paint("agent> " + format_reply(reply), CYAN) + "\n")


def do_web(a: argparse.Namespace) -> None:
    from .web import _is_loopback, create_app
    if not _is_loopback(a.host) and not a.allow_remote:
        raise SystemExit("Error: this app has no login and holds the MCP token: it binds to 127.0.0.1 only. "
                         f"Use --allow-remote to serve on {a.host} (put authentication in front of it).")
    mtoken = os.environ.get("COGENT_CHAT_METRICS_TOKEN", "").strip() or None
    if a.metrics and not mtoken and not _is_loopback(a.host):
        raise SystemExit("Error: --metrics on a non-loopback address needs $COGENT_CHAT_METRICS_TOKEN.")
    cfg = build_config(a)
    telemetry.init("cogent-chat", a.otlp_endpoint)
    agent = build_agent(cfg)
    disclaimer = " ".join(agent.source.general_disclaimers()[:1])
    app = create_app(agent, title=cfg.mcp_url.split("//", 1)[-1].split("/", 1)[0], disclaimer=disclaimer, metrics=a.metrics, metrics_token=mtoken)
    print(f"Ready: {len(agent.chunks)} chunks indexed. Serving on http://{a.host}:{a.port}" + ("  (metrics on /metrics)" if a.metrics else ""))
    app.run(host=a.host, port=a.port, threaded=True)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Conversational agent for the cogent MCP server (LangChain + Ollama).")
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("chat", help="terminal chat")
    add_common(c)
    w = sub.add_parser("web", help="web UI")
    add_common(w)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=5000)
    w.add_argument("--allow-remote", action="store_true", help="serve on a non-loopback address (the app has no login)")
    w.add_argument("--metrics", action="store_true", help="serve Prometheus metrics on /metrics (token: $COGENT_CHAT_METRICS_TOKEN)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    telemetry.init("cogent-chat", getattr(a, "otlp_endpoint", None))
    {"chat": do_chat, "web": do_web}[a.command](a)


if __name__ == "__main__":
    main()
