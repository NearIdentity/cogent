"""Chat LLMs for the join-inference pipeline (suggester and judge): Ollama, plus a scriptable fake for tests.

``OllamaLLM.chat_json`` calls ``POST {host}/api/chat`` with ``format`` set to a JSON schema (structured output; falls back to plain
``"json"`` mode on older servers), parses the answer defensively (code fences, leading prose) and retries. Standard library only."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("kg_llm")
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.1:8b"


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> Any:
    """JSON from an LLM answer: plain, in a code fence, or embedded in prose."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(text[a:b + 1])
        except ValueError:
            pass
    raise LLMError(f"no JSON in the model's answer: {text[:120]!r}")


class LLM:
    name = "base"
    model = ""

    def chat_json(self, system: str, user: str, schema: Optional[dict] = None, temperature: float = 0.0, seed: Optional[int] = None,
                  task: str = "") -> Any:
        raise NotImplementedError


class OllamaLLM(LLM):
    name = "ollama"

    def __init__(self, model: str = DEFAULT_MODEL, host: Optional[str] = None, timeout: float = 300.0, retries: int = 2, num_ctx: int = 8192):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.timeout, self.retries, self.num_ctx = timeout, retries, num_ctx

    def _post(self, body: dict) -> dict:
        req = Request(self.host + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def chat_json(self, system, user, schema=None, temperature=0.0, seed=None, task=""):
        opts: dict[str, Any] = {"temperature": temperature, "num_ctx": self.num_ctx}
        if seed is not None:
            opts["seed"] = seed
        body = {"model": self.model, "stream": False, "options": opts, "format": schema or "json",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        last: Exception = LLMError("no attempt")
        for attempt in range(self.retries + 1):
            try:
                try:
                    out = self._post(body)
                except HTTPError as exc:
                    if exc.code == 400 and schema is not None:          # server without structured-output support
                        body["format"] = "json"
                        out = self._post(body)
                    else:
                        raise
                return parse_json(out["message"]["content"])
            except (URLError, HTTPError, TimeoutError, ConnectionError, KeyError, ValueError, LLMError) as exc:
                last = exc
                log.warning("ollama chat attempt %d/%d failed (%s): %s", attempt + 1, self.retries + 1, self.model, exc)
                time.sleep(min(2.0 ** attempt, 8.0))
        raise LLMError(f"Ollama at {self.host} (model {self.model}) gave no usable answer: {last}. Is it running and is the model pulled "
                       f"(ollama pull {self.model})?")


class FakeLLM(LLM):
    """Scripted model for tests: ``handler(task, system, user, temperature, seed) -> dict`` (raise to simulate a failure)."""
    name = "fake"

    def __init__(self, handler: Callable[..., Any], model: str = "fake"):
        self.handler, self.model = handler, model
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, system, user, schema=None, temperature=0.0, seed=None, task=""):
        self.calls.append({"task": task, "system": system, "user": user, "temperature": temperature, "seed": seed})
        return self.handler(task, system, user, temperature, seed)


def get_llm(kind: str = "ollama", model: Optional[str] = None, host: Optional[str] = None) -> OllamaLLM:
    kind = kind or "ollama"
    if kind == "ollama":
        return OllamaLLM(model or os.environ.get("COGENT_LLM_MODEL") or DEFAULT_MODEL, host)
    raise ValueError(f"unknown LLM kind {kind!r} (use 'ollama')")
