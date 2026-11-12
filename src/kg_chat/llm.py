"""Ollama models for the chat agent (LangChain), instrumented so the chat client's LLM usage is reported with the rest of cogent.

Every call goes through the same ``kg_telemetry.record_llm`` as the agent and the MCP server (``cogent_llm_*`` metrics) under the service name
``chat``, so the dashboard's LLM panels can add all three together or show them side by side.
"""
from __future__ import annotations

import time

from langchain_ollama import ChatOllama, OllamaEmbeddings

import kg_telemetry as telemetry
from kg_llm_usage import UsageCallback as _Usage

SERVICE = "chat"


class UsageCallback(_Usage):
    """The shared usage callback (kg_llm_usage), reporting under the service name ``chat``."""

    def __init__(self, model: str, kind: str):
        super().__init__(model, kind, service=SERVICE, failure_stage="chat_llm")


class UsageOllamaEmbeddings(OllamaEmbeddings):
    """``OllamaEmbeddings`` that also reports the tokens Ollama says it embedded (LangChain's wrapper drops ``prompt_eval_count``)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        t0 = time.perf_counter()
        try:
            resp = self._client.embed(self.model, texts, dimensions=self.dimensions, options=self._default_params, keep_alive=self.keep_alive)
            vectors = resp["embeddings"]
            tokens = int((resp.get("prompt_eval_count") if hasattr(resp, "get") else getattr(resp, "prompt_eval_count", 0)) or 0)
        except AttributeError:                       # a langchain-ollama without these internals: no token count, still works
            vectors, tokens = super().embed_documents(texts), 0
        except Exception:
            telemetry.record_llm(self.model, "embed", 0, 0, time.perf_counter() - t0, "error", service_name=SERVICE)
            telemetry.record_failure("chat_embed")
            raise
        telemetry.record_llm(self.model, "embed", tokens, 0, time.perf_counter() - t0, "ok", service_name=SERVICE)
        return vectors


def make_chat_model(model: str, base_url: str, num_ctx: int = 8192) -> ChatOllama:
    """``num_ctx`` is sent to Ollama: without it the server's default window (often 2048 to 4096 tokens) applies, and a longer prompt is silently cut."""
    return ChatOllama(model=model, base_url=base_url, temperature=0, num_ctx=num_ctx)


def make_embeddings(model: str, base_url: str) -> OllamaEmbeddings:
    return UsageOllamaEmbeddings(model=model, base_url=base_url)
