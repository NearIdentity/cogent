"""Disk caches so that a restart does not re-download every graph or re-embed every chunk.

* ``SectionCache``  one JSON file per entry: the Markdown sections built from its graph, valid while the entry's ``graph_built`` time (and the
                    settings that shaped the sections) are unchanged.
* ``DiskEmbeddings`` wraps any LangChain ``Embeddings``: vectors are remembered in SQLite by model name and text hash, so only new text is sent
                    to Ollama.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings


class SectionCache:
    def __init__(self, directory: Optional[Path]):
        self.dir = Path(directory) / "sections" if directory else None

    def _path(self, entry: str) -> Optional[Path]:
        return self.dir / (re.sub(r"[^A-Za-z0-9._-]", "_", entry) + ".json") if self.dir else None

    def load(self, entry: str, signature: str) -> Optional[list[Document]]:
        p = self._path(entry)
        if not p or not p.exists():
            return None
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("signature") != signature:
            return None
        return [Document(page_content=d["text"], metadata=d["metadata"]) for d in data.get("sections", [])]

    def save(self, entry: str, signature: str, sections: list[Document]) -> None:
        p = self._path(entry)
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(json.dumps({"signature": signature, "sections": [{"text": d.page_content, "metadata": d.metadata} for d in sections]}), "utf-8")
        tmp.replace(p)


class DiskEmbeddings(Embeddings):
    def __init__(self, inner: Embeddings, model: str, directory: Optional[Path], batch: int = 64):
        self.inner, self.model, self.batch = inner, model, batch
        self.hits = self.misses = 0
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(Path(directory) / "embeddings.sqlite"), check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS vec (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            self._db.commit()

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\x1f{text}".encode("utf-8")).hexdigest()

    def _get(self, key: str) -> Optional[list[float]]:
        if self._db is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT v FROM vec WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, items: list[tuple[str, list[float]]]) -> None:
        if self._db is None or not items:
            return
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO vec (k, v) VALUES (?, ?)", [(k, json.dumps(v)) for k, v in items])
            self._db.commit()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        keys = [self._key(t) for t in texts]
        out: list[Any] = [self._get(k) for k in keys]
        todo = [i for i, v in enumerate(out) if v is None]
        self.hits += len(texts) - len(todo)
        for s in range(0, len(todo), self.batch):
            idx = todo[s:s + self.batch]
            vecs = self.inner.embed_documents([texts[i] for i in idx])
            self._put([(keys[i], v) for i, v in zip(idx, vecs)])
            for i, v in zip(idx, vecs):
                out[i] = v
        self.misses += len(todo)
        return out

    def embed_query(self, text: str) -> list[float]:
        key = self._key("q:" + text)
        hit = self._get(key)
        if hit is not None:
            return hit
        v = self.inner.embed_query(text)
        self._put([(key, v)])
        return v
