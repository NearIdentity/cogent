"""Telemetry: the metrics registry and its Prometheus text, persistence across runs, LLM token / cost / retry / hallucination / judge metrics, tracing."""
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_embed  # noqa: E402
import kg_llm  # noqa: E402
import kg_llmjoin as L  # noqa: E402
import kg_relmap as R  # noqa: E402
import kg_tables as K  # noqa: E402
import kg_telemetry as T  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_process_registry(monkeypatch):
    """Every test records into its own registry (the process registry is shared by the whole session)."""
    reg = T.Registry()
    monkeypatch.setattr(T, "_current", reg)
    monkeypatch.setattr(T, "_PROCESS", reg)
    return reg


# ------------------------------------------------------------------------------------------------------------------ registry
def test_registry_renders_valid_prometheus_text(fresh_process_registry):
    r = fresh_process_registry
    r.inc("cogent_agent_runs_total", result="ok")
    for v in (0.004, 0.2, 3, 700):
        r.observe("cogent_agent_operation_duration_seconds", v, op="fetch")
    r.set("cogent_agent_last_run_success", 1)
    r.inc("cogent_llm_tokens_total", 12, service="agent", model='odd"name\\x', direction="input")
    text = r.render()
    assert '# TYPE cogent_agent_operation_duration_seconds histogram' in text
    assert 'cogent_agent_operation_duration_seconds_bucket{op="fetch",le="+Inf"} 4' in text and 'le="0.25"} 2' in text
    assert 'cogent_agent_operation_duration_seconds_count{op="fetch"} 4' in text
    assert 'model="odd\\"name\\\\x"' in text
    promtool = shutil.which("promtool")
    if promtool:                                                    # the real linter, when it is installed
        res = subprocess.run([promtool, "check", "metrics"], input=text, text=True, capture_output=True)
        assert res.returncode == 0, res.stderr + res.stdout


def test_registry_persists_merges_and_survives_a_changed_bucket_layout(tmp_path):
    a, b = T.Registry(), T.Registry()
    a.inc("cogent_agent_runs_total", result="ok")
    a.observe("cogent_agent_run_duration_seconds", 3.0)
    a.set("cogent_agent_last_run_duration_seconds", 3.0)
    b.inc("cogent_agent_runs_total", 2, result="ok")
    b.observe("cogent_agent_run_duration_seconds", 40.0)
    b.set("cogent_agent_last_run_duration_seconds", 40.0)
    T.persist(tmp_path, a)
    total = T.persist(tmp_path, b)
    assert total.value("cogent_agent_runs_total", result="ok") == 3 and total.count("cogent_agent_run_duration_seconds") == 2
    assert total.value("cogent_agent_last_run_duration_seconds") == 40.0                      # gauges hold the last run
    assert (tmp_path / "metrics" / "cogent_agent.prom").read_text() == total.render()
    reloaded = T.load_persisted(tmp_path)
    assert reloaded.value("cogent_agent_runs_total") == 3
    data = json.loads((tmp_path / "metrics" / "agent_metrics.json").read_text())
    data["series"]["cogent_agent_run_duration_seconds"][0][1]["b"] = [1, 2]                  # an old file with another bucket layout
    assert T.Registry.from_dict(data).count("cogent_agent_run_duration_seconds") == 0


def test_telemetry_calls_never_raise(monkeypatch):
    monkeypatch.setattr(T, "_current", None)                        # a broken registry
    T.record_retry("x"), T.record_failure("x"), T.record_llm("m", "chat", 1, 1), T.record_output("triple", True), T.record_tool("t", "ok", 0.1)


def test_exposition_merges_the_live_registry_with_the_agents_persisted_totals(tmp_path, fresh_process_registry):
    agent = T.Registry()
    agent.inc("cogent_agent_runs_total", result="ok")
    agent.inc("cogent_llm_tokens_total", 100, service="agent", model="m", direction="input")
    T.persist(tmp_path, agent)
    T.record_tool("get_overview", "ok", 0.01)
    T.record_llm("m", "embed", 5, 0, 0.1, service_name="mcp")
    text = T.Exposition(tmp_path).render()
    assert 'cogent_agent_runs_total{result="ok"} 1' in text and 'cogent_mcp_tool_calls_total{outcome="ok",tool="get_overview"} 1' in text
    assert 'service="agent"' in text and 'service="mcp"' in text and text.count("# TYPE cogent_llm_tokens_total") == 1


# ------------------------------------------------------------------------------------------------------------------ pricing
def test_cost_uses_configured_prices(monkeypatch):
    assert T.cost_usd("llama3.1:8b", 1_000_000, 1_000_000) == 0.0                        # local models are free unless priced
    monkeypatch.setenv("COGENT_LLM_PRICES", json.dumps({"llama3.1:8b": {"in": 0.5, "out": 1.5}, "*": {"in": 1.0, "out": 2.0}}))
    assert T.cost_usd("llama3.1:8b", 2_000_000, 1_000_000) == pytest.approx(2.5)
    assert T.cost_usd("other", 1_000_000, 0) == pytest.approx(1.0)
    monkeypatch.setenv("COGENT_LLM_PRICES", "not json")
    assert T.cost_usd("llama3.1:8b", 10, 10) == 0.0


# ------------------------------------------------------------------------------------------------------------------ mock Ollama
class Ollama(BaseHTTPRequestHandler):
    fail_first = {"n": 0}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/chat":
            if Ollama.fail_first["n"] > 0:
                Ollama.fail_first["n"] -= 1
                self.send_response(500)
                self.end_headers()
                return
            out = {"message": {"content": json.dumps({"joins": []})}, "prompt_eval_count": 120, "eval_count": 30}
        else:
            out = {"embeddings": [[1.0, 0.0] for _ in body.get("input", [])], "prompt_eval_count": 7 * len(body.get("input", []))}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Ollama.fail_first["n"] = 0
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_llm_tokens_cost_and_retries_are_recorded(ollama, monkeypatch, fresh_process_registry):
    monkeypatch.setenv("COGENT_LLM_PRICES", json.dumps({"m1": {"in": 1.0, "out": 2.0}}))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    llm = kg_llm.OllamaLLM("m1", ollama)
    Ollama.fail_first["n"] = 1                                                          # the first attempt gets a 500
    llm.chat_json("sys", "user", task="suggest")
    r = fresh_process_registry
    assert r.value("cogent_llm_tokens_total", model="m1", direction="input") == 120 and r.value("cogent_llm_tokens_total", model="m1", direction="output") == 30
    assert r.value("cogent_llm_cost_usd_total", model="m1") == pytest.approx((120 * 1.0 + 30 * 2.0) / 1e6)
    assert r.value("cogent_llm_calls_total", kind="suggest", outcome="ok") == 1 and r.value("cogent_agent_retries_total", component="llm") == 1
    emb = kg_embed.OllamaEmbedder("nomic-embed-text", ollama, dim=2)
    emb.embed_docs(["a", "b", "c"])
    assert r.value("cogent_llm_tokens_total", model="nomic-embed-text", direction="input") == 21
    Ollama.fail_first["n"] = 99
    with pytest.raises(kg_llm.LLMError):
        kg_llm.OllamaLLM("m1", ollama, retries=1).chat_json("s", "u", task="judge")
    assert r.value("cogent_llm_calls_total", kind="judge", outcome="error") == 1 and r.value("cogent_agent_failures_total", stage="llm") == 1


# ------------------------------------------------------------------------------------------------------------------ judge and hallucination
def mk(tid, header, rows):
    fr = K.frame([header] + rows)
    return R.T(tid, tid, K.profile(fr), fr.rows, K.Context(caption=tid))


def test_hallucinated_join_suggestions_and_judge_agreement_are_counted(fresh_process_registry):
    a = mk("d/1", ["Zone", "Limit"], [["z1", "1"], ["z2", "2"], ["z3", "3"], ["z4", "4"]])
    b = mk("d/2", ["Zone", "Name"], [["z1", "x"], ["z2", "y"], ["z3", "z"], ["z4", "w"]])
    votes = iter(["accept", "accept", "accept", "accept", "reject", "accept"])           # candidate 1 unanimous, candidate 2 split (2 runs each, then 3)

    def handler(task, system, user, temp, seed):
        if task == "suggest":
            return {"joins": [{"left_column": "Zone", "right_column": "Zone", "confidence": 0.9, "relationship": "join", "rationale": "same"},
                              {"left_column": "Zoneish", "right_column": "Nothing", "confidence": 0.8, "relationship": "join", "rationale": "made up"}]}
        return {"verdict": next(votes, "accept"), "confidence": 0.8, "reasons": [], "problems": []}
    ev = L.evaluate([a, b], R.infer([a, b]), kg_llm.FakeLLM(handler), None, L.Config(samples=2, judge_runs=2, max_pairs=5))
    r = fresh_process_registry
    assert r.value("cogent_llm_outputs_total", kind="join_suggestion", outcome="hallucinated") == 2        # one invented pair in each of the two runs
    assert r.value("cogent_llm_outputs_total", kind="join_suggestion", outcome="grounded") == 2
    assert ev["metrics"]["hallucination_rate"] == 0.5
    assert r.value("cogent_join_judge_candidates_total", agreement="unanimous") + r.value("cogent_join_judge_candidates_total", agreement="split") == ev["metrics"]["judged"]
    assert r.value("cogent_join_llm_vs_deterministic_total", result="both") >= 1


def test_ungrounded_llm_triples_are_counted(fresh_process_registry):
    import kg_ingest
    text = "The Nuclear Regulatory Commission endorsed Regulatory Guide 1.233 in 2020."
    ts = [kg_ingest.Triple("Nuclear Regulatory Commission", "endorsed", "Regulatory Guide 1.233", strict=False),
          kg_ingest.Triple("Acme Corporation", "endorsed", "Regulatory Guide 1.233", strict=False),
          kg_ingest.Triple("NRC staff", "endorsed", "Guide", strict=True)]               # rule-based: not checked
    kg_ingest._check_grounding(ts, text)
    r = fresh_process_registry
    assert r.value("cogent_llm_outputs_total", kind="triple", outcome="hallucinated") == 1 and r.value("cogent_llm_outputs_total", kind="triple", outcome="grounded") == 3
    assert kg_ingest.entity_grounded("Regulatory Guide", text) and not kg_ingest.entity_grounded("Quantum Reactor Division", text)


# ------------------------------------------------------------------------------------------------------------------ tracing
@pytest.fixture()
def spans():
    pytest.importorskip("opentelemetry.sdk.trace", reason="OpenTelemetry SDK not installed")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exp = InMemorySpanExporter()
    T.init("cogent-test", exporter=exp)
    yield exp
    exp.clear()


def test_spans_nest_record_errors_and_measure(spans, fresh_process_registry):
    with T.span("outer", a=1):
        with T.op("fetch", **{"http.url": "http://x"}) as o:
            pass
        with pytest.raises(ValueError):
            with T.op("sync_doc"):
                raise ValueError("boom")
        with T.op("sync_doc") as o2:
            o2.error("handled failure")
    done = {s.name: s for s in spans.get_finished_spans()}
    assert {"outer", "agent.op fetch", "agent.op sync_doc"} <= set(done)
    outer = done["outer"]
    assert done["agent.op fetch"].parent.span_id == outer.context.span_id
    sync = [s for s in spans.get_finished_spans() if s.name == "agent.op sync_doc"]
    assert len(sync) == 2 and all(s.status.status_code.name == "ERROR" for s in sync) and any(e.name == "exception" for s in sync for e in s.events)
    r = fresh_process_registry
    assert r.value("cogent_agent_operations_total", op="sync_doc", outcome="error") == 2 and r.value("cogent_agent_failures_total", stage="sync_doc") == 2
    assert r.value("cogent_agent_operations_total", op="fetch", outcome="ok") == 1 and r.count("cogent_agent_operation_duration_seconds", op="fetch") == 1


def test_traceparent_is_extracted_and_injected(spans):
    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    ctx = T.extract_context({"traceparent": tp})
    with T.span("http POST /mcp", parent=ctx):
        pass
    s = spans.get_finished_spans()[0]
    assert format(s.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c" and format(s.parent.span_id, "016x") == "b7ad6b7169203331"
    env: dict = {}
    with T.span("daemon.run") as sp:
        T.inject_env(env)
    assert env["COGENT_TRACEPARENT"].split("-")[1] == format(sp.get_span_context().trace_id, "032x")
    assert T.extract_context({}) is None


def test_mcp_tool_wrapper_traces_and_counts(spans, fresh_process_registry):
    import kg_mcp
    from kg_store import NotFound

    @kg_mcp._tool_errors
    def get_thing(ref: str) -> dict:
        if ref == "missing":
            raise NotFound("no such thing")
        if ref == "bad":
            raise RuntimeError("bug")
        return {"ok": True}
    assert get_thing(ref="standard:1") == {"ok": True}
    with pytest.raises(ValueError):
        get_thing(ref="missing")
    with pytest.raises(RuntimeError):
        get_thing(ref="bad")
    r = fresh_process_registry
    assert [r.value("cogent_mcp_tool_calls_total", tool="get_thing", outcome=o) for o in ("ok", "not_found", "error")] == [1, 1, 1]
    assert r.count("cogent_mcp_tool_duration_seconds", tool="get_thing") == 3
    names = [(s.name, s.attributes.get("cogent.ref")) for s in spans.get_finished_spans()]
    assert ("mcp.tool get_thing", "standard:1") in names and len(names) == 3


def test_pushgateway_push_puts_the_text_format_and_failure_returns_false(fresh_process_registry):
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_PUT(self):
            got["path"] = self.path
            got["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    reg = T.Registry()
    reg.inc("cogent_agent_runs_total", outcome="ok")
    try:
        assert T.push(f"http://127.0.0.1:{srv.server_port}", reg) is True
    finally:
        srv.shutdown()
    assert got["path"] == "/metrics/job/cogent_agent"
    assert "cogent_agent_runs_total" in got["body"]
    assert T.push(f"http://127.0.0.1:{srv.server_port}", reg, timeout=1) is False
