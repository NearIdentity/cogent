"""Relation names for the knowledge graph.

The rule-based extractor takes the text between two entity mentions as the predicate, which produced hundreds of junk relation types
("or", "10", "the", URLs, table-of-contents dot leaders, table headers). ``relation`` turns a raw predicate into a small vocabulary:

* rule-based predicates must contain a known verb; the relation is the verb's lemma plus a following preposition (``is required by`` ->
  ``require_by``, ``endorsed in`` -> ``endorse_in``); anything else becomes ``related_to``;
* predicates from an LLM only have to pass the quality filter (no URLs, digits, punctuation, stop-word-only text, over-long phrases);
* table cells become ``has_value`` (the column header stays on the edge as ``predicate``).

The raw text is never lost: the edge keeps it as its ``predicate`` attribute.
"""
from __future__ import annotations

import re
from typing import Optional

DEFAULT = "related_to"
HAS_VALUE = "has_value"

STOP = frozenset("a an the and or of to in on at by for from with as is are was were be been being it its this that these those which who whom whose "
                 "than then so such not no nor but if when where while also both either neither each any all other more most".split())
AUX = frozenset("shall will would may might must should can could is are was were be been being has have had do does did not".split())
PREPS = frozenset("in of to for by with from on under at as against through within per into over".split())

VERBS = frozenset("""
accept account achieve add address adopt affect agree align allow amend analyse analyze apply approve assess assign assume
authorize base cite classify comply compute conduct confirm consider constitute contain control cover create define demonstrate
depend derive describe design determine develop document ensure establish estimate evaluate exceed exempt expect explain extend
follow give govern guide identify implement include incorporate indicate inform inspect issue justify list maintain make mandate
meet modify monitor note obtain occur offer operate perform permit prescribe prevent prohibit protect provide publish qualify
reference regulate relate remove replace report represent require restrict result retain review revise satisfy select
specify state submit supersede supplement support test treat update use validate verify withdraw
endorse recognize recommend propose reaffirm refer rely complement endorse supply represent
""".split())


PHRASES = {"in accordance with": "comply_with", "according to": "comply_with", "pursuant to": "comply_with", "subject to": "subject_to",
           "based on": "base_on", "referred to": "refer_to"}


def lemma(token: str) -> Optional[str]:
    """The base form of ``token`` if it is a known verb (crude suffix stripping: required, requires, requiring -> require)."""
    t = token.lower()
    cands = [t, t[:-1], t[:-2], t[:-1] if t.endswith("d") else "", t[:-3], t[:-3] + "e", t[:-3] + "y", t[:-4] if t[-3:-2:] == t[-4:-3] else ""]
    if t.endswith("ies"):
        cands.append(t[:-3] + "y")
    if t.endswith("ed") or t.endswith("ing"):
        stem = t[:-2] if t.endswith("ed") else t[:-3]
        cands += [stem, stem + "e", stem[:-1] if len(stem) > 2 and stem[-1] == stem[-2] else ""]
    return next((c for c in cands if c and c in VERBS), None)


def acceptable(pred: str) -> bool:
    """Quality filter shared by every extractor."""
    p = (pred or "").strip()
    if not p or len(p) > 40 or len(p.split()) > 5:
        return False
    low = p.lower()
    if re.search(r"https?:|www\.|@|\.{3,}|…|(?:\. ){3,}|/\w*\d", low):
        return False
    if any(ch.isdigit() for ch in p):
        return False
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", low)
    return bool(words) and not all(w in STOP for w in words)


def relation(pred: str, strict: bool = True, table: bool = False) -> str:
    """The relation name for a raw predicate (see the module docstring)."""
    if table:
        return HAS_VALUE
    if (pred or "").strip().lower() == DEFAULT:
        return DEFAULT
    p = re.sub(r"[_\s]+", " ", (pred or "").strip())
    if not acceptable(p):
        return DEFAULT
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", p.lower())
    for phrase, rel in PHRASES.items():
        if phrase in " ".join(words):
            return rel
    if not strict:
        core = [w for w in words if w not in ("the", "a", "an")]
        return "_".join(core) or DEFAULT
    for i, w in enumerate(words):
        if w in AUX or w in STOP and w not in PREPS:
            continue
        v = lemma(w)
        if v:
            nxt = words[i + 1] if i + 1 < len(words) and words[i + 1] in PREPS else ""
            return f"{v}_{nxt}" if nxt else v
    return DEFAULT


def is_toc_like(text: str) -> bool:
    """Table-of-contents lines (dot leaders, trailing page numbers) carry no relations."""
    t = text or ""
    return bool(re.search(r"\.{5,}|(?:\.\s){5,}|…{2,}", t))
