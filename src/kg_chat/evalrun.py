"""Evaluation run: ask a list of questions of the REAL stack (MCP server + Ollama models) and check what the unit tests cannot.

The unit tests use fake models, so they cannot show how a particular Ollama model behaves inside this agent: whether it follows the answer
schema, how cautious it is with ``found_in_context``, whether the prompt fits its context window, how long an answer takes. This run measures
exactly that, per question: the verdict, the prompt tokens Ollama counted against the window, excerpts dropped, retries, sources, latency, and
pass / FAIL against the expectations written next to each question (``chat_eval/questions.json`` is a template: edit it for your data).

    python src/kg_chat_app.py eval --mcp-url https://host/mcp --questions chat_eval/questions.json --report chat_eval/report.md
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .agent import CogentAgent, ModelAnswerError
from .models import ChatReply


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(q, dict) and isinstance(q.get("question"), str) for q in data):
        raise ValueError(f"{path}: expected a JSON list of objects with a 'question'")
    return data


def check(expect: dict[str, Any], reply: Optional[ChatReply], error: str = "") -> list[str]:
    """What is wrong with a reply, as a list of short reasons (empty = pass)."""
    if reply is None:
        return [f"no answer: {error}"]
    bad: list[str] = []
    text = reply.answer.lower()
    if "found" in expect and reply.found_in_context != bool(expect["found"]):
        bad.append(f"found_in_context is {reply.found_in_context}, expected {bool(expect['found'])}")
    for w in expect.get("answer_contains", []):
        if w.lower() not in text:
            bad.append(f"the answer lacks '{w}'")
    any_of = expect.get("answer_contains_any", [])
    if any_of and not any(w.lower() in text for w in any_of):
        bad.append("the answer has none of: " + ", ".join(any_of))
    names = " | ".join(f"{s.entry_name} {s.entry} {s.caption}" for s in reply.sources).lower()
    if expect.get("source_entry_names") and not any(n.lower() in names for n in expect["source_entry_names"]):
        bad.append("no source among: " + ", ".join(expect["source_entry_names"]))
    if len(reply.tables) < int(expect.get("tables_min", 0)):
        bad.append(f"fewer than {expect['tables_min']} table(s)")
    notes = " ".join(n.text for n in reply.notices).lower()
    for w in expect.get("notice_contains", []):
        if w.lower() not in notes:
            bad.append(f"no note mentions '{w}'")
    if expect.get("allow_overflow") is not True and reply.diagnostics.get("overflow"):
        bad.append("the prompt filled the model's whole context window")
    return bad


def run_eval(agent: CogentAgent, questions: list[dict[str, Any]], progress: Callable[[str], None] = lambda s: None) -> list[dict[str, Any]]:
    results = []
    for q in questions:
        agent.history = []                                                       # every question starts a fresh conversation
        t0 = time.perf_counter()
        reply, err = None, ""
        try:
            reply = agent.command(q["question"]) or agent.ask(q["question"])
        except ModelAnswerError as exc:
            err = f"model output: {exc}"
        except Exception as exc:                                                 # model server down, MCP error ...
            err = f"{type(exc).__name__}: {str(exc)[:160]}"
        bad = check(q.get("expect", {}), reply, err)
        d = reply.diagnostics if reply else {}
        row = {"id": q.get("id") or q["question"][:30], "question": q["question"], "pass": not bad, "problems": bad, "seconds": round(time.perf_counter() - t0, 1),
               "found": reply.found_in_context if reply else None, "answer": (reply.answer if reply else err)[:400], "sources": len(reply.sources) if reply else 0,
               "tables": len(reply.tables) if reply else 0, "prompt_tokens": d.get("prompt_tokens"), "num_ctx": d.get("num_ctx"), "dropped": d.get("excerpts_dropped"),
               "note": q.get("note", "")}
        results.append(row)
        progress(f"{'PASS' if row['pass'] else 'FAIL'}  {row['id']}  ({row['seconds']} s)" + ("" if row["pass"] else "  " + "; ".join(bad)))
    return results


def report_markdown(results: list[dict[str, Any]], model: str, embed_model: str, retrieval: str) -> str:
    n_fail = sum(1 for r in results if not r["pass"])
    L = [f"# Chat agent evaluation: {len(results) - n_fail} of {len(results)} passed", "",
         f"Chat model `{model}`, embeddings `{embed_model}`, retrieval `{retrieval}`.", "",
         "| result | id | found | prompt tokens / window | dropped | sources | tables | seconds | problems |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        win = f"{r['prompt_tokens']} / {r['num_ctx']}" if r["prompt_tokens"] else "n/a"
        L.append(f"| {'PASS' if r['pass'] else '**FAIL**'} | {r['id']} | {r['found']} | {win} | {r['dropped'] if r['dropped'] is not None else ''} | {r['sources']} | {r['tables']} | "
                 f"{r['seconds']} | {'; '.join(r['problems']).replace('|', '/') or ''} |")
    L += ["", "## Answers", ""]
    for r in results:
        L.append(f"**{r['id']}**: {r['question']}\n\n> {r['answer'].replace(chr(10), ' ')}" + (f"\n\n_{r['note']}_" if r["note"] else "") + "\n")
    L += ["## How to read this", "",
          "* *found* wrong in either direction: too timid (`False` where the data has the answer) or too eager. Compare with `/explain <question>` to see whether the "
          "retrieval had the excerpt; if it did and the model still said not found, try a larger chat model.",
          "* *prompt tokens / window* near the window: Ollama may cut the prompt; raise `--num-ctx` (and `OLLAMA_CONTEXT_LENGTH` on the server) or lower `--top-k`.",
          "* *no answer: model output*: the model broke the answer schema twice; use a model with structured-output support.",
          "* Expectations are data-dependent: edit `chat_eval/questions.json` for the standards you have staged."]
    return "\n".join(L) + "\n"
