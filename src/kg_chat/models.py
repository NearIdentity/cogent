"""Pydantic models shared by the MCP connection, the knowledge source and the chat agent.

As in 0xA6E07, Pydantic gives validated, typed configuration and a schema that LangChain's ``with_structured_output`` hands to the LLM, so the
model's reply is parsed and checked rather than free-form text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator


class ClientConfig(BaseModel):
    """Settings of the chat client. The token is a ``SecretStr``: it never appears in a repr, a log line or a JSON dump."""

    mcp_url: str                                  # the cogent MCP endpoint, e.g. https://host/mcp (a bare https://host gets /mcp appended)
    token: SecretStr                              # bearer token (kg_mcp.py token create ...)
    chat_model: str = "llama3.1"                  # any chat model pulled in Ollama that supports structured output
    embed_model: str = "nomic-embed-text"         # any embedding model pulled in Ollama
    ollama_url: str = "http://localhost:11434"
    chunk_size: int = Field(1000, ge=100)
    chunk_overlap: int = Field(150, ge=0)
    top_k: int = Field(6, ge=1, le=30)            # text chunks retrieved per question
    table_rows: int = Field(8, ge=1, le=50)       # table rows asked of the table database per question
    history_turns: int = Field(3, ge=0)           # past Q/A pairs given to the model
    entries: list[str] = Field(default_factory=list)   # restrict to these entries (e.g. ["standard:22"]); empty = every entry that has a graph
    cache_dir: Optional[Path] = None              # where fetched graph sections and embeddings are cached (None: no cache)
    max_nodes: int = Field(200_000, ge=100)       # nodes requested per entry graph (export_graph)
    timeout: float = Field(60.0, ge=1.0)          # seconds per MCP request
    notice_ttl: float = Field(300.0, ge=0.0)      # seconds an entry's provenance (warnings, edition status) is reused

    @field_validator("mcp_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        u = urlparse(v if "://" in v else "https://" + v)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("mcp_url must be an http(s) URL such as https://host/mcp")
        if u.username or u.password or u.query:
            raise ValueError("mcp_url must not carry credentials or a query string: pass the token separately (--token-file / COGENT_MCP_TOKEN)")
        path = u.path.rstrip("/") or "/mcp"
        return f"{u.scheme}://{u.netloc}{path}"

    @field_validator("token")
    @classmethod
    def _token(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("the MCP token is empty")
        return SecretStr(v.get_secret_value().strip())

    @property
    def insecure_remote(self) -> bool:
        """A token sent over plain http to a host that is not this machine travels in clear text."""
        u = urlparse(self.mcp_url)
        return u.scheme == "http" and (u.hostname or "") not in ("127.0.0.1", "localhost", "::1")


class AgentAnswer(BaseModel):
    """Structured reply the LLM must produce (validated by Pydantic): the same contract as in 0xA6E07."""

    answer: str = Field(description="Answer to the question, based only on the provided context. Markdown is allowed for emphasis and lists.")
    found_in_context: bool = Field(description="True only if the context actually contains the answer.")


class SourceRef(BaseModel):
    """Where a piece of context came from. Built from retrieval metadata, never from the model, so it cannot be invented."""

    label: str = ""                               # S1, S2, ... as numbered in the prompt context
    kind: Literal["text", "table"] = "text"
    entry: str = ""                               # standard:22
    entry_name: str = ""
    document: str = ""
    section: str = ""
    page: Optional[int] = None
    url: str = ""                                 # the document's source URL (only http(s) is shown as a link)
    table_id: str = ""
    caption: str = ""

    @property
    def title(self) -> str:
        bits = [self.entry_name or self.entry, self.caption or self.section]
        text = " — ".join(b for b in bits if b)
        return text + (f" (p. {self.page})" if self.page else "")


class TableView(BaseModel):
    """A table from the MCP server, ready to display: verbatim cell text plus the Markdown that the interface renders."""

    title: str
    markdown: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    source: Optional[SourceRef] = None


class Notice(BaseModel):
    """A caveat the server attached (stale capture, not the latest edition, paywalled content, open warning). Relayed by code, not by the model."""

    level: Literal["info", "warning"] = "info"
    text: str
    entry: str = ""


class ChatReply(BaseModel):
    answer: str
    found_in_context: bool = False
    sources: list[SourceRef] = Field(default_factory=list)
    tables: list[TableView] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
