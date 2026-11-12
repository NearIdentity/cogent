"""kg_chat: a conversational front-end for the cogent MCP server.

Adapted from the company-website Q&A agent in NearIdentity/0xA6E07 (LangChain retrieval + Ollama models + Pydantic-validated
structured answers + a Flask web UI). The documents are not crawled pages but the knowledge graphs and tables that the cogent MCP server
serves; the connection is a bearer-token authenticated MCP session. See docs/chat-client.md.
"""
