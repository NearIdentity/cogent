"""LangChain callback that reports Ollama usage to kg_telemetry (``cogent_llm_*`` metrics): tokens as the model server reports them, duration, outcome.

Used by the daily agent (report narration), and by the chat client (kg_chat) under its own service name, so that every LLM call of cogent can be
added up in one dashboard. ``service=None`` uses the process's own service name (``agent``, ``mcp``).
"""
from __future__ import annotations

import time
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

import kg_telemetry as telemetry


class UsageCallback(BaseCallbackHandler):
    def __init__(self, model: str, kind: str, service: Optional[str] = None, failure_stage: str = "llm"):
        self.model, self.kind, self.service, self.failure_stage = model, kind, service, failure_stage
        self._t0: dict[UUID, float] = {}

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._t0[run_id] = time.perf_counter()

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._t0[run_id] = time.perf_counter()

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        tin = tout = 0
        for gens in getattr(response, "generations", []) or []:
            for g in gens:
                um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                tin += int(um.get("input_tokens") or 0)
                tout += int(um.get("output_tokens") or 0)
        telemetry.record_llm(self.model, self.kind, tin, tout, time.perf_counter() - self._t0.pop(run_id, time.perf_counter()), "ok", service_name=self.service)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        telemetry.record_llm(self.model, self.kind, 0, 0, time.perf_counter() - self._t0.pop(run_id, time.perf_counter()), "error", service_name=self.service)
        telemetry.record_failure(self.failure_stage)
