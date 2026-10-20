"""LLM join inference with LLM-as-judge: suggestions, voting, reconciliation, statistics, approaches; Ollama chat client."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import networkx as nx
import pytest

import kg_llm
import kg_llmjoin as L
import kg_relmap as R
import kg_tables as K
from conftest import DOC_A, stage_document


def mk(tid, header, rows, caption=""):
    fr = K.frame([header] + rows)
    return R.T(tid, tid.split("/")[-1], K.profile(fr), fr.rows, K.Context(caption=caption or tid))


@pytest.fixture()
def tabs(tmp_path):
    """Table 3-1 / 4-1 of the real document (deterministic join on Seismic Category) plus two synthetic tables:
    'Classes' (joins to Table 4-1 only semantically: codes 'SC-3' vs 'SDC-3') and an unrelated one."""
    home = tmp_path / "h"
    d = stage_document(home, "a" * 16, DOC_A, "https://x/y.html", ["standard:1"])
    g = nx.node_link_graph(json.loads((d / "kg.json").read_text()), edges="edges")
    man = json.loads(next((d / "assets").glob("*/manifest.json")).read_text())
    out = []
    for a in man["assets"]:
        fr = K.frame(K.read_csv(next((d / "assets").glob(f"*/{a['file']}"))))
        out.append(R.T("a" * 16 + "/" + a["id"], a["id"], K.profile(fr), fr.rows, K.kg_context(g, a["id"], a["caption"])))
    out.append(mk("a" * 16 + "/tblx1", ["Class", "Description"], [["SC-3", "important"], ["SC-4", "major"], ["SC-5", "critical"], ["SC-6", "extreme"]], "Table 7-1: Classes"))
    out.append(mk("a" * 16 + "/tblx2", ["Colour", "Shade"], [["red", "x"], ["blue", "y"], ["green", "z"], ["pink", "w"]], "Table 8-1: Colours"))
    return out


def col(user, which):
    """Header of column A or B named in a judge prompt ('proposed join: "X" in table A  =  "Y" in table B')."""
    line = user.split("proposed join:")[1].split("\n")[0]
    parts = line.split('"')
    return parts[1] if which == "A" else parts[3]


def script(llm_joins, verdicts):
    """handler: suggest -> the given (left_column, right_column) pairs; judge -> verdict by the column pair of the prompt."""
    def handler(task, system, user, temp, seed):
        if task == "suggest":
            a = user.split("TABLE A:")[1].split("\n")[0].strip()
            b = user.split("TABLE B:")[1].split("\n")[0].strip()
            return {"joins": [dict(j) for j in llm_joins(a, b)]}
        pair = {col(user, "A"), col(user, "B")}
        for key, v in verdicts.items():
            if set(key) == pair:
                return v
        return {"verdict": "reject", "confidence": 0.6, "reasons": ["no evidence"], "problems": []}
    return handler


ACCEPT = {"verdict": "accept", "confidence": 0.9, "reasons": ["same codes"], "problems": []}


def sj(l, r, conf=0.9):
    return {"left_column": l, "right_column": r, "confidence": conf, "rationale": f"{l} matches {r}"}


def test_statuses_and_cross_method_agreement(tabs):
    det = R.infer(tabs)
    assert [(r["left_column"], r["right_column"]) for r in det if r["kind"] == "join"] == [("seismic_category", "seismic_category")]

    def joins(a, b):
        out = []
        if "Table 4-1" in a + b and "Table 3-1" in a + b:
            out.append(sj("Seismic Category", "Seismic Category"))
        if "Classes" in a + b and "Table 3-1" in a + b:                 # semantic join the heuristics cannot see (SC-3 ~ SDC-3)
            left, right = ("Class", "Seismic Category") if "Classes" in a else ("Seismic Category", "Class")
            out.append(sj(left, right, 0.8))
        if "Colours" in a + b and "Classes" in a + b:                    # a bad suggestion
            left, right = ("Colour", "Class") if "Colours" in a else ("Class", "Colour")
            out.append(sj(left, right, 0.6))
        return out
    llm = kg_llm.FakeLLM(script(joins, {("Seismic Category", "Seismic Category"): ACCEPT, ("Class", "Seismic Category"): ACCEPT}))
    ev = L.evaluate(tabs, det, llm, None, L.Config(samples=2, judge_runs=3, max_pairs=10))
    by = {(c["left_header"], c["right_header"]): c for c in ev["candidates"]}
    both = by[("Seismic Category", "Seismic Category")]
    assert both["status"] == "confirmed" and both["methods"] == ["deterministic", "llm"] and both["llm"]["agreement"] == 1.0 and both["judge"]["verdict"] == "accept"
    llm_only = next(c for c in ev["candidates"] if c["status"] == "llm_only")
    assert {llm_only["left_header"], llm_only["right_header"]} == {"Class", "Seismic Category"} and not llm_only["deterministic"]["proposed"]
    rejected = next(c for c in ev["candidates"] if c["status"] == "rejected")
    assert {rejected["left_header"], rejected["right_header"]} == {"Colour", "Class"} and rejected["judge"]["verdict"] == "reject" and rejected["final_score"] <= 0.3
    m = ev["metrics"]
    assert (m["deterministic"], m["llm"], m["both"], m["llm_only"], m["deterministic_only"]) == (1, 3, 1, 2, 0)
    assert m["llm_self_consistency"] == 1.0 and m["hallucination_rate"] == 0.0 and m["judge_consistency"] == 1.0
    assert m["deterministic_confirmed_by_llm"] == 1.0 and m["llm_confirmed_by_deterministic"] == round(1 / 3, 3)
    assert m["judge_accept_rate_deterministic"] == 1.0 and m["judge_accept_rate_llm"] == round(2 / 3, 3) and m["judge_accept_rate_llm_only"] == 0.5
    assert m["deterministic_precision_vs_judge"] == 1.0 and m["deterministic_recall_vs_judge"] == 0.5 and m["llm_recall_vs_judge"] == 1.0
    assert ev["status"]["llm"] == "ok" and ev["status"]["judge"] == "ok" and "not ground truth" in ev["judge_note"]
    md = L.evaluation_markdown("Seismic", ev)
    assert "# Join evaluation" in md and "confirmed" in md and "rejected" in md and "same codes" in md


def test_suggestion_runs_use_different_styles_and_temperatures_and_bad_columns_are_counted(tabs):
    calls_seen = []

    def handler(task, system, user, temp, seed):
        if task == "suggest":
            calls_seen.append((temp, seed, "Pay most attention to column names" in user, "come from the same set of codes" in user))
            return {"joins": [sj("No Such Column", "Seismic Category"), sj("Seismic Category", "Seismic Category", 0.2),          # hallucinated / low confidence
                              {"left_column": "Seismic Category", "right_column": "Seismic Category", "confidence": "high", "rationale": "x"}]}   # bad number
        return ACCEPT
    llm = kg_llm.FakeLLM(handler)
    ev = L.evaluate(tabs[:2], [], llm, None, L.Config(samples=3, judge_runs=1))
    assert [c[0] for c in calls_seen] == [0.0, 0.7, 0.7] and len({c[1] for c in calls_seen}) == 3
    assert [(c[2], c[3]) for c in calls_seen] == [(False, False), (True, False), (False, True)]                        # balanced / names / values
    assert ev["status"]["invalid_suggestions"] == 3 and ev["status"]["suggestions"] == 9 and ev["metrics"]["hallucination_rate"] == round(3 / 9, 3)
    assert len(ev["candidates"]) == 1 and ev["candidates"][0]["llm"]["agreement"] == 1.0                              # 'high' falls back to 0.5, which counts


def test_judge_alternates_the_column_order_and_votes(tabs):
    seen = []

    def handler(task, system, user, temp, seed):
        if task == "suggest":
            return {"joins": [sj("Seismic Category", "Seismic Category")]}
        seen.append(user.split("TABLE A:")[1].split("\n")[0])
        n = len(seen)
        return {"verdict": "accept" if n in (1, 3) else "reject", "confidence": 0.7 + 0.05 * n, "reasons": [f"vote {n}"], "problems": ["p"] if n == 2 else []}
    ev = L.evaluate(tabs[:2], R.infer(tabs[:2]), kg_llm.FakeLLM(handler), None, L.Config(samples=1, judge_runs=3))
    assert len(set(seen)) == 2 and seen[0] == seen[2] != seen[1]                                                       # position bias control: A and B swap on run 2
    j = ev["candidates"][0]["judge"]
    assert j["votes"] == {"accept": 2, "reject": 1, "uncertain": 0} and j["verdict"] == "accept" and j["consistency"] == round(2 / 3, 3) and j["runs"] == 3
    assert j["confidence"] == round((0.75 + 0.85) / 2, 3) and j["problems"] == ["p"]
    # a tie between accept and reject is 'uncertain'
    toggle = iter(["accept", "reject"])
    ev = L.evaluate(tabs[:2], R.infer(tabs[:2]), kg_llm.FakeLLM(lambda t, s, u, tp, sd: {"joins": []} if t == "suggest" else {"verdict": next(toggle), "confidence": 0.8, "reasons": []}),
                    None, L.Config(samples=1, judge_runs=2))
    assert ev["candidates"][0]["judge"]["verdict"] == "uncertain" and ev["candidates"][0]["status"] == "uncertain"


def test_deterministic_join_disputed_by_the_judge_and_ambiguity_flag(tabs):
    ev = L.evaluate(tabs[:2], R.infer(tabs[:2]), kg_llm.FakeLLM(lambda t, s, u, tp, sd: {"joins": []} if t == "suggest" else {"verdict": "reject", "confidence": 0.9, "reasons": ["too generic"]}),
                    None, L.Config(samples=1, judge_runs=1))
    c = ev["candidates"][0]
    assert c["status"] == "disputed" and c["deterministic"]["proposed"] and c["llm"]["proposed_by"] == 0 and c["final_score"] == 0.3     # capped
    # two suggested right-hand columns for one left column: the weaker one is flagged as an alternative
    t3 = mk("a" * 16 + "/tblx3", ["Code", "Code Again"], [["SDC-3", "SDC-3"], ["SDC-4", "SDC-4"], ["SDC-5", "SDC-5"]], "Table 9-1")

    def sug(task, system, user, temp, seed):
        if task == "judge":
            return ACCEPT
        return {"joins": [sj("Seismic Category", "Code", 0.9), sj("Seismic Category", "Code Again", 0.5)]} if "Table 3-1" in user.split("TABLE B:")[0] or "Table 3-1" in user else {"joins": []}
    ev = L.evaluate([tabs[0], t3], [], kg_llm.FakeLLM(sug), None, L.Config(samples=1, judge_runs=1))
    alt = {c["right_header"]: c.get("ambiguous_alternative") for c in ev["candidates"]}
    assert alt == {"Code": False, "Code Again": True}


def test_llm_down_keeps_deterministic_results_and_time_budget(tabs):
    def down(*a):
        raise kg_llm.LLMError("connection refused")
    ev = L.evaluate(tabs[:2], R.infer(tabs[:2]), kg_llm.FakeLLM(down), None, L.Config(samples=2, judge_runs=2))
    assert ev["status"]["llm"] == "unavailable" and ev["status"]["judge"] == "not run" and "connection refused" in ev["status"]["errors"][0]
    c = ev["candidates"][0]
    assert c["status"] == "deterministic_only" and c["llm"] is None and c["judge"] is None and c["final_score"] == c["deterministic"]["score"]
    # a judge that goes down later leaves the candidates unjudged but present
    n = {"i": 0}

    def flaky(task, system, user, temp, seed):
        if task == "suggest":
            return {"joins": []}
        raise kg_llm.LLMError("judge died")
    ev = L.evaluate(tabs[:2], R.infer(tabs[:2]), kg_llm.FakeLLM(flaky), None, L.Config(samples=1, judge_runs=1))
    assert ev["status"]["judge"] == "unavailable" and ev["candidates"][0]["judge"] is None
    # no LLM at all: plain deterministic candidates
    ev = L.evaluate(tabs, R.infer(tabs), None)
    assert [c["status"] for c in ev["candidates"]] == ["deterministic_only"] and ev["status"]["llm"] == "not run"
    # time budget
    clock = iter(range(0, 1000, 10))
    import time as _t
    real = _t.time
    L.time.time = lambda: next(clock)
    try:
        ev = L.evaluate(tabs, R.infer(tabs), kg_llm.FakeLLM(lambda t, s, u, tp, sd: {"joins": []}), None, L.Config(samples=3, max_seconds=25))
    finally:
        L.time.time = real
    assert ev["status"]["llm"] == "partial" and any("time budget" in e for e in ev["status"]["errors"])


def test_select_by_approach_and_thresholds(tabs):
    def joins(a, b):
        out = []
        if "Table 4-1" in a + b and "Table 3-1" in a + b:
            out.append(sj("Seismic Category", "Seismic Category"))
        if "Classes" in a + b and "Table 3-1" in a + b:
            out.append(sj(*(("Class", "Seismic Category") if "Classes" in a else ("Seismic Category", "Class")), 0.8))
        if "Colours" in a + b and "Classes" in a + b:
            out.append(sj(*(("Colour", "Class") if "Colours" in a else ("Class", "Colour")), 0.6))
        return out
    verdicts = {("Seismic Category", "Seismic Category"): ACCEPT, ("Class", "Seismic Category"): ACCEPT}
    cands = L.evaluate(tabs, R.infer(tabs), kg_llm.FakeLLM(script(joins, verdicts)), None, L.Config(samples=2, judge_runs=1, max_pairs=10))["candidates"]
    ids = lambda sel: sorted(c["status"] for c in sel)
    assert ids(L.select(cands, "deterministic")) == ["confirmed"]
    assert ids(L.select(cands, "llm")) == ["confirmed", "llm_only"]                      # the judge-rejected colour join is excluded
    assert ids(L.select(cands, "hybrid")) == ["confirmed", "llm_only"]
    assert ids(L.select(cands, "consensus")) == ["confirmed"]
    assert ids(L.select(cands, "hybrid", include_rejected=True)) == ["confirmed", "llm_only", "rejected"]
    assert [c["rejected"] for c in L.select(cands, "hybrid", include_rejected=True)] == [False, False, True]
    assert L.select(cands, "hybrid", min_score=0.95) == []
    assert ids(L.select(cands, "llm", min_agreement=1.0)) == ["confirmed", "llm_only"] and L.select(cands, "consensus", min_score=0.99) == []
    rel = L.candidates_to_relations(L.select(cands, "hybrid"))[0]
    assert rel["kind"] == "join" and rel["methods"] == ["deterministic", "llm"] and any(e.startswith("LLM:") for e in rel["evidence"]) and any(e.startswith("judge") for e in rel["evidence"])
    # without any LLM evidence: hybrid = deterministic, llm / consensus are empty
    plain = L.evaluate(tabs, R.infer(tabs), None)["candidates"]
    assert len(L.select(plain, "hybrid")) == 1 and L.select(plain, "llm") == [] and L.select(plain, "consensus") == []
    with pytest.raises(ValueError):
        L.select(cands, "nope")
    assert set(L.PROFILES) == {"fast", "balanced", "strict", "exploratory", "llm"} and all(p["approach"] in L.APPROACHES and p["units"] in ("strict", "broad", "off") for p in L.PROFILES.values())


# ------------------------------------------------------------------------------------------ Ollama chat client
class _Chat(BaseHTTPRequestHandler):
    bodies: list = []
    answers: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).bodies.append(body)
        ans = type(self).answers.pop(0) if type(self).answers else {"message": {"content": "{}"}}
        if isinstance(ans, int):
            self.send_response(ans)
            self.end_headers()
            return
        data = json.dumps(ans).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def chat():
    _Chat.bodies, _Chat.answers = [], []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ollama_chat_json_structured_output_parsing_and_fallbacks(chat, monkeypatch):
    monkeypatch.setattr(kg_llm.time, "sleep", lambda s: None)
    llm = kg_llm.OllamaLLM("llama3.1:8b", chat)
    _Chat.answers = [{"message": {"content": '{"joins": []}'}}]
    assert llm.chat_json("sys", "usr", L.SUGGEST_SCHEMA, temperature=0.7, seed=5) == {"joins": []}
    b = _Chat.bodies[0]
    assert b["model"] == "llama3.1:8b" and b["stream"] is False and b["format"] == L.SUGGEST_SCHEMA and b["options"]["temperature"] == 0.7 and b["options"]["seed"] == 5
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    _Chat.answers = [{"message": {"content": 'Sure!\n```json\n{"verdict": "accept"}\n```'}}]
    assert llm.chat_json("s", "u") == {"verdict": "accept"} and _Chat.bodies[-1]["format"] == "json"
    _Chat.answers = [400, {"message": {"content": 'Here: {"a": 1} done'}}]                      # server without structured outputs: retry with plain json mode
    assert llm.chat_json("s", "u", {"type": "object"}) == {"a": 1} and _Chat.bodies[-1]["format"] == "json"
    _Chat.answers = [500, {"message": {"content": "not json at all"}}, {"message": {"content": '{"ok": true}'}}]       # transient error and a bad answer are retried
    assert llm.chat_json("s", "u") == {"ok": True}
    with pytest.raises(kg_llm.LLMError, match="ollama pull"):
        kg_llm.OllamaLLM("m", "http://127.0.0.1:9", retries=0).chat_json("s", "u")
    assert kg_llm.parse_json('{"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(kg_llm.LLMError):
        kg_llm.parse_json("nothing")
    with pytest.raises(ValueError):
        kg_llm.get_llm("nope")
