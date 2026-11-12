#!/usr/bin/env python3
"""Launcher for the cogent conversational agent (web UI or terminal).

    export COGENT_MCP_TOKEN=...                                     # or --token-file
    python src/kg_chat_app.py web  --mcp-url https://host/mcp       # http://127.0.0.1:5000
    python src/kg_chat_app.py chat --mcp-url https://host/mcp

See docs/chat-client.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kg_chat.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
