"""What fake-model tests can still say about the real model's limits: context window, schema failures, over-cautious 'not found', the evaluation run.

A fake model cannot show how llama3.1 behaves; these tests pin down the parts that do not depend on the model (the prompt must fit the window, the
failure modes must be reported, the evaluation harness must catch what a real model does) and simulate the Ollama behaviours that matter."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import ENTRY, FakeConn, KeywordEmbeddings, base_tools, cfg, make_llm, node  # noqa: E402
from test_chat_retrieval import big_graph  # noqa: E402

import kg_telemetry as telemetry  # noqa: E402
from kg_chat import evalrun  # noqa: E402
from kg_chat.agent import CogentAgent, ModelAnswerError, est_tokens  # noqa: E402
from kg_chat.llm import make_chat_model  # noqa: E402
from kg_chat.source import KnowledgeSource  # noqa: E402
from kg_chat.web import create_app  # noqa: E402


def long_graph(sections=30, chars=900):
    t = "ab12cd34"
    nodes = [node(f"{t}:doc:a.html", "Document", "Seismic Design Criteria")]
    n = 0
    for i in range(sections):
        n += 1
        nodes.append(node(f"{t}:h{n:04d}", "Heading", f"{i + 1} Damping topic {i}", level="2"))
        n += 1
        nodes.append(node(f"{t}:p{n:04d}", "Paragraph", ("Damping values for welded steel structures and the facility safety case. " * 20)[:chars] + f" Item {i}."))
    return {"graph": {"sources": [{"tag": t, "source": "https://www.example.org/d.html"}]}, "nodes": nodes, "edges": [], "truncated": False}


def agent_with(handler, graph=None, table_mode=None, ctx=0, cpt=3.0, **kw):
    tools = base_tools(table_mode)
    tools["export_graph"] = lambda a: {"graph": graph or long_graph()}
    c = cfg(**kw)
    llm = make_llm(handler, ctx=ctx, cpt=cpt)
    return CogentAgent(c, KnowledgeSource(FakeConn(tools), c), llm=llm, embeddings=KeywordEmbeddings()), llm


def test_the_model_is_created_with_an_explicit_context_window():
    assert make_chat_model("m", "http://127.0.0.1:1", 6000).num_ctx == 6000
    assert make_chat_model("m", "http://127.0.0.1:1").num_ctx == 8192
    assert cfg(num_ctx=4096).num_ctx == 4096
    with pytest.raises(Exception):
        cfg(num_ctx=100)


def test_the_prompt_is_trimmed_to_fit_the_window_and_the_instructions_survive():
    """Ollama cuts the START of an overlong prompt: the instructions and the best excerpts. The client must never send more than the window."""
    def handler(text):
        return ("answer", True)
    agent, llm = agent_with(handler, ctx=2048, num_ctx=2048, top_k=20, answer_reserve=256)
    r = agent.ask("Damping for welded steel structures?")
    prompt = llm.prompts[-1]
    assert est_tokens(prompt) <= 2048 - 256 + 100 and prompt.startswith("System: You answer questions about nuclear-industry standards")
    assert "[S1] Seismic Design Criteria" in prompt                                    # the best-ranked excerpt is in
    assert r.diagnostics["excerpts_dropped"] > 0 and any("left out" in n.text for n in r.notices)
    roomy, llm2 = agent_with(handler, ctx=16000, num_ctx=16000, top_k=6)
    r2 = roomy.ask("Damping for welded steel structures?")
    assert r2.diagnostics["excerpts_dropped"] == 0 and not any("left out" in n.text for n in r2.notices)


def test_without_the_budget_the_same_question_would_overflow_the_window():
    agent, _ = agent_with(lambda t: ("a", True), num_ctx=2048, top_k=20)
    hits = agent.retrieve("Damping for welded steel structures?")
    unbudgeted, _, _, rep = agent._context(hits, [], "", budget=10 ** 6)
    assert est_tokens(unbudgeted) > 2048 and rep["excerpts_dropped"] == 0            # the problem is real: 20 chunks of ~900 characters do not fit 2048 tokens


def test_what_matters_most_survives_a_small_window():
    """Order of priority: the server's own state, then table rows, then text chunks in rank order."""
    seen = []
    agent, llm = agent_with(lambda t: (seen.append(t) or "ok", True), table_mode="rows", ctx=1800, num_ctx=1800, top_k=10, answer_reserve=200)
    r = agent.ask("What is staged on the server, and what damping for welded steel?")
    p = llm.prompts[-1]
    assert "## What the MCP server has staged" in p and "Table 4-1" in p and "Welded steel" in p                 # facts and the table rows are in
    assert r.diagnostics["excerpts_dropped"] + r.diagnostics["excerpts_cut"] > 0
    assert all(tl.source is None or tl.source.label for tl in r.tables)                                          # the labels of the kept tables are right


def test_a_full_window_reported_by_ollama_is_flagged():
    """The client's estimate can be too low for a model's tokenizer. Ollama's own prompt_eval_count (capped at the window when it cuts) tells."""
    m = telemetry.metrics()
    before = m.value("cogent_chat_context_overflow_total")
    agent, _ = agent_with(lambda t: ("answer", True), ctx=2048, cpt=1.2, num_ctx=2048, top_k=6)                 # a tokenizer 2.5x denser than the estimate
    r = agent.ask("Damping for welded steel structures?")
    assert r.diagnostics["overflow"] is True and r.diagnostics["prompt_tokens"] >= 0.98 * 2048
    assert any(n.level == "warning" and "whole context window" in n.text for n in r.notices)
    assert m.value("cogent_chat_context_overflow_total") == before + 1
    fine, _ = agent_with(lambda t: ("answer", True), ctx=8192, num_ctx=8192)
    assert not fine.ask("Damping for welded steel?").diagnostics.get("overflow")


def test_a_broken_answer_schema_is_retried_once_then_reported_not_hidden_as_not_found():
    calls = []

    def flaky(text):
        calls.append(1)
        return None if len(calls) == 1 else ("Recovered answer.", True)
    agent, _ = agent_with(flaky)
    assert agent.ask("Damping for welded steel?").answer == "Recovered answer." and len(calls) == 2
    broken, _ = agent_with(lambda t: None)
    before = telemetry.metrics().value("cogent_chat_questions_total", outcome="error")
    with pytest.raises(ModelAnswerError) as e:
        broken.ask("Damping for welded steel?")
    assert "structured output" in str(e.value)
    assert telemetry.metrics().value("cogent_chat_questions_total", outcome="error") == before + 1
    client = create_app(broken).test_client()
    r = client.post("/api/chat", json={"message": "Damping for welded steel?"})
    assert r.status_code == 502 and "structured output" in r.get_json()["error"]


def test_a_too_cautious_not_found_points_at_the_excerpts_that_do_contain_the_code():
    """llama3.1:8b-class models say 'not found' too readily. When the question's code IS in the data, say so and say where."""
    agent, _ = agent_with(lambda t: ("I could not find that in the knowledge graphs.", False), graph=big_graph(), top_k=3)
    r = agent.ask("What does the guide say about ASTM A572?")
    assert not r.found_in_context
    texts = [n.text for n in r.notices]
    assert any(t.startswith("A572 does appear in the knowledge graphs") and "13 Referenced documents" in t for t in texts)
    assert not any("No indexed text mentions" in t for t in texts)


def test_a_code_the_data_lacks_is_reported_as_absent_not_as_a_retrieval_miss():
    agent, _ = agent_with(lambda t: ("not found", False), graph=big_graph())
    r = agent.ask("What does the guide say about ASTM A999?")
    assert any("No indexed text mentions A999" in n.text for n in r.notices)


# ---------------------------------------------------------------------------- the evaluation run

def test_check_finds_what_a_real_model_gets_wrong():
    from kg_chat.models import ChatReply, Notice, SourceRef
    ok = ChatReply(answer="RG 1.233 endorses it.", found_in_context=True, sources=[SourceRef(label="S1", entry_name="NRC RG 1.233", entry="standard:23")])
    assert evalrun.check({"found": True, "answer_contains_any": ["1.233"], "source_entry_names": ["RG 1.233"]}, ok) == []
    timid = ChatReply(answer="I could not find that.", found_in_context=False)
    assert "found_in_context is False, expected True" in evalrun.check({"found": True}, timid)[0]
    assert "the answer lacks 'x'" in evalrun.check({"answer_contains": ["x"]}, ok)[0]
    assert "no source among" in evalrun.check({"source_entry_names": ["NQA"]}, ok)[0]
    assert "no note mentions 'A572'" in evalrun.check({"found": False, "notice_contains": ["A572"]}, timid)[0]
    assert evalrun.check({"notice_contains": ["a572"]}, ChatReply(answer="n", notices=[Notice(text="No indexed text mentions A572")])) == []
    assert "no answer" in evalrun.check({}, None, "ModelAnswerError: bad")[0]
    over = ChatReply(answer="x", found_in_context=True, diagnostics={"overflow": True})
    assert "whole context window" in evalrun.check({}, over)[0] and evalrun.check({"allow_overflow": True}, over) == []


def test_the_run_reports_prompt_sizes_failures_and_writes_a_report(tmp_path, capsys):
    def handler(text):
        q = text.rsplit("Question:", 1)[-1].lower()
        if "world cup" in q:
            return ("I could not find that.", False)
        if "timid" in q:
            return ("I could not find that.", False)                     # a model that is too cautious
        return ("Damping for welded steel is in Table 4-1.", True)
    agent, _ = agent_with(handler, ctx=8192)
    qs = [{"id": "ok", "question": "Damping for welded steel?", "expect": {"found": True, "answer_contains": ["Table 4-1"]}},
          {"id": "refuse", "question": "Who won the world cup in 1998?", "expect": {"found": False}},
          {"id": "too-timid", "question": "A timid question about damping", "expect": {"found": True}}]
    res = evalrun.run_eval(agent, qs, progress=print)
    assert [r["pass"] for r in res] == [True, True, False] and res[0]["prompt_tokens"] > 0 and res[0]["num_ctx"] == 8192
    assert "FAIL  too-timid" in capsys.readouterr().out
    md = evalrun.report_markdown(res, "llama3.1", "nomic-embed-text", "hybrid")
    assert md.startswith("# Chat agent evaluation: 2 of 3 passed") and "| **FAIL** | too-timid | False |" in md and "How to read this" in md
    qf = tmp_path / "q.json"
    qf.write_text(json.dumps(qs))
    assert len(evalrun.load_questions(qf)) == 3
    qf.write_text('{"not": "a list"}')
    with pytest.raises(ValueError):
        evalrun.load_questions(qf)


def test_a_model_that_breaks_the_schema_fails_the_question_instead_of_crashing_the_run():
    agent, _ = agent_with(lambda t: None)
    res = evalrun.run_eval(agent, [{"id": "q", "question": "Damping?", "expect": {"found": True}}])
    assert res[0]["pass"] is False and "model output" in res[0]["problems"][0]


def test_the_shipped_question_list_is_valid_and_has_the_two_reported_prompts():
    qs = evalrun.load_questions(Path(__file__).resolve().parents[1] / "chat_eval" / "questions.json")
    texts = [q["question"] for q in qs]
    assert "What is staged, and is the database read-only?" in texts and "What is the allowable stress for ASTM A572? Quote the source." in texts
    assert all("expect" in q for q in qs)


def test_eval_command_exits_nonzero_on_failures(monkeypatch, tmp_path):
    from kg_chat import cli
    agent, _ = agent_with(lambda t: ("I could not find that.", False))
    monkeypatch.setattr(cli, "build_agent", lambda cfg_, progress=print: agent)
    monkeypatch.setenv("COGENT_MCP_TOKEN", "x")
    qf = tmp_path / "q.json"
    qf.write_text(json.dumps([{"id": "must-be-found", "question": "Damping?", "expect": {"found": True}}]))
    rep = tmp_path / "out" / "report.md"
    with pytest.raises(SystemExit) as e:
        cli.main(["eval", "--mcp-url", "https://h/mcp", "--questions", str(qf), "--report", str(rep)])
    assert e.value.code == 1 and "FAIL" in rep.read_text()
