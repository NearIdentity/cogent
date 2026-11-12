"""Keyword (lexical) retrieval over the indexed chunks, and its fusion with the vector search.

Embeddings find passages that *mean* the same; they are weak on exact codes such as ``NEI 18-04``, ``A572``, ``DOE-STD-1104`` or ``50.69``. A small
BM25 index over the same chunks covers that, and the two rankings are fused with reciprocal rank fusion (RRF), so a chunk that either method ranks
well is retrieved. Standard library only.

Tokens follow the server's own keyword search (``kg_store.search``): case-insensitive, hyphens, slashes and underscores count as spaces
(``DOE-STD`` finds ``DOE STD``), and a dotted number stays one token (``50.69``, ``1.233``).
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Callable, Hashable, Iterable, Optional, Sequence

from langchain_core.documents import Document

_SEP = re.compile(r"[-_/‐-―]+")
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")


# Words of a question that say nothing about which passage is meant; they are dropped from the QUERY only (the index keeps every token).
STOPWORDS = frozenset("a an the of to in on for and or is are was were be been what which who whom whose does do did it its say says said about with by from "
                      "how much many that this these those there as at can could should shall will would please tell me give show list find any all i you we they "
                      "he she them his her their our your into than then so if not no yes also more most such".split())


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(_SEP.sub(" ", text.lower()))


def query_tokens(text: str) -> list[str]:
    """Informative tokens of a question: no stop words (a lone stop-word query finds nothing, so the vector search decides)."""
    return list(dict.fromkeys(t for t in tokenize(text) if t not in STOPWORDS))


def has_identifier(text: str) -> bool:
    """Does the question contain a code (a token with a digit: ``A572``, ``18``, ``50.69``)? Then the keyword ranking is given more weight."""
    return any(any(c.isdigit() for c in t) for t in query_tokens(text))


def chunk_key(d: Document) -> Hashable:
    """Identity of a chunk across the two indexes."""
    return (d.metadata.get("entry"), d.metadata.get("node"), d.page_content)


class Bm25Index:
    """Okapi BM25 over chunk texts (the section path is part of the text, so a heading such as "4 Damping" is searchable too)."""

    def __init__(self, docs: Sequence[Document], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = list(docs), k1, b
        self._tf: list[Counter] = []
        self._len: list[int] = []
        self._post: dict[str, list[int]] = defaultdict(list)
        for i, d in enumerate(self.docs):
            toks = tokenize(f"{d.metadata.get('section', '')} {d.page_content}")
            c = Counter(toks)
            self._tf.append(c)
            self._len.append(len(toks))
            for t in c:
                self._post[t].append(i)
        n = len(self.docs)
        self._avg = (sum(self._len) / n) if n else 0.0
        self._idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self._post.items()}

    def search(self, query: str, k: int) -> list[tuple[Document, float]]:
        """The ``k`` best chunks with their scores (only chunks that contain at least one query token)."""
        terms = query_tokens(query)
        scores: dict[int, float] = defaultdict(float)
        for t in terms:
            idf = self._idf.get(t)
            if idf is None:
                continue
            for i in self._post[t]:
                tf = self._tf[i][t]
                scores[i] += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self._len[i] / (self._avg or 1.0)))
        best = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]
        return [(self.docs[i], s) for i, s in best]


def fuse(rankings: Iterable[Sequence[Document]], k: int, rrf_k: int = 60, weights: Optional[Sequence[float]] = None) -> list[tuple[Document, list[int]]]:
    """Weighted reciprocal rank fusion of ranked lists of chunks. Returns the top ``k`` with the indexes of the lists that ranked each one."""
    score: dict[Hashable, float] = defaultdict(float)
    doc: dict[Hashable, Document] = {}
    seen_in: dict[Hashable, list[int]] = defaultdict(list)
    for li, ranking in enumerate(rankings):
        for rank, d in enumerate(ranking):
            key = chunk_key(d)
            score[key] += (weights[li] if weights else 1.0) / (rrf_k + rank + 1)
            doc.setdefault(key, d)
            if li not in seen_in[key]:
                seen_in[key].append(li)
    order = sorted(score, key=lambda x: -score[x])[:k]
    return [(doc[key], seen_in[key]) for key in order]
