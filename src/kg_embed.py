"""Text embedders for the table database: Ollama (production) and a deterministic hashing embedder (tests / offline).

Every table, row and query is embedded with the *same* embedder so vectors are comparable: one model, one dimension
(the ``spec`` string, stored in the database and checked on every use), L2-normalised, cosine distance.

``OllamaEmbedder`` talks to ``POST {host}/api/embed`` (batch input; falls back to the older ``/api/embeddings``). Models
trained with task prefixes (``nomic-embed-text``: ``search_document: `` / ``search_query: ``) get them automatically.
Standard library only (urllib)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from typing import Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("kg_embed")

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text"
# model-name prefix -> (document prefix, query prefix)
PREFIXES = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "mxbai-embed-large": ("", "Represent this sentence for searching relevant passages: "),
    "snowflake-arctic-embed": ("", "Represent this sentence for searching relevant passages: "),
    "bge-": ("", "Represent this sentence for searching relevant passages: "),
}
MAX_CHARS = 6000            # inputs are truncated to this many characters (models have a context limit)


class EmbedError(RuntimeError):
    pass


def _normalise(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [float(x) / n for x in v]


class Embedder:
    name = "base"
    dim: int = 0

    @property
    def spec(self) -> str:
        """Identifies the vector space: tables embedded with different specs must not be mixed."""
        return f"{self.name}/{self.dim}"

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed_docs([text])[0]


class HashEmbedder(Embedder):
    """Deterministic bag-of-words (+ bigram) feature hashing: no model, no network. Texts that share words get a high cosine
    similarity, which is all the tests and an offline smoke run need. Not for real retrieval quality."""

    name = "hash"

    def __init__(self, dim: int = 64):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = re.findall(r"[a-z0-9]+", text.lower())
        for i, t in enumerate(toks):
            for feat, w in ((t, 1.0), (f"{t} {toks[i + 1]}" if i + 1 < len(toks) else None, 0.5)):
                if feat:
                    h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
                    v[h % self.dim] += w if (h >> 40) & 1 else -w
        return _normalise(v)

    def embed_docs(self, texts):
        return [self._vec(t) for t in texts]


class OllamaEmbedder(Embedder):
    name = "ollama"

    def __init__(self, model: str = DEFAULT_MODEL, host: Optional[str] = None, batch: int = 32, timeout: float = 120.0,
                 retries: int = 3, dim: Optional[int] = None):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.batch, self.timeout, self.retries = batch, timeout, retries
        base = model.split(":")[0]
        self.doc_prefix, self.query_prefix = next((p for k, p in PREFIXES.items() if base.startswith(k)), ("", ""))
        self._dim = dim

    @property
    def dim(self) -> int:                                         # type: ignore[override]
        if not self._dim:
            self._dim = len(self._call(["dimension probe"])[0])    # one tiny request discovers the dimension
        return self._dim

    @property
    def spec(self) -> str:
        return f"ollama:{self.model}/{self.dim}"

    def _post(self, path: str, body: dict) -> dict:
        req = Request(self.host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def _call(self, texts: list[str]) -> list[list[float]]:
        texts = [t[:MAX_CHARS] or " " for t in texts]
        last: Exception = EmbedError("no attempt")
        for attempt in range(self.retries):
            try:
                try:
                    out = self._post("/api/embed", {"model": self.model, "input": texts, "truncate": True})["embeddings"]
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                    out = [self._post("/api/embeddings", {"model": self.model, "prompt": t})["embedding"] for t in texts]
                if len(out) != len(texts):
                    raise EmbedError(f"expected {len(texts)} embeddings, got {len(out)}")
                return [_normalise(v) for v in out]
            except (URLError, HTTPError, TimeoutError, ConnectionError, KeyError, ValueError, EmbedError) as exc:
                last = exc
                log.warning("ollama embed attempt %d/%d failed: %s", attempt + 1, self.retries, exc)
                time.sleep(min(2.0 ** attempt, 8.0))
        raise EmbedError(f"Ollama at {self.host} (model {self.model}) did not return embeddings: {last}. "
                         f"Is it running and is the model pulled (ollama pull {self.model})?")

    def embed_docs(self, texts):
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out += self._call([self.doc_prefix + t for t in texts[i:i + self.batch]])
        if out and not self._dim:
            self._dim = len(out[0])
        return out

    def embed_query(self, text):
        return self._call([self.query_prefix + text])[0]


def get_embedder(kind: str = "ollama", model: Optional[str] = None, host: Optional[str] = None, dim: Optional[int] = None) -> Embedder:
    """``kind``: 'ollama' (default) or 'hash'. Environment fallbacks: COGENT_EMBEDDER, COGENT_EMBED_MODEL, OLLAMA_HOST."""
    kind = kind or os.environ.get("COGENT_EMBEDDER", "ollama")
    if kind == "hash":
        return HashEmbedder(dim or 64)
    if kind == "ollama":
        return OllamaEmbedder(model or os.environ.get("COGENT_EMBED_MODEL") or DEFAULT_MODEL, host, dim=dim)
    raise ValueError(f"unknown embedder {kind!r} (use 'ollama' or 'hash')")
