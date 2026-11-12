"""LLM usage of the chat client is reported with the rest of cogent: same metric names, service="chat"; the Prometheus / Grafana files know them."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import ROOT, KeywordEmbeddings  # noqa: E402

import kg_telemetry as telemetry  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from kg_chat.llm import UsageCallback, UsageOllamaEmbeddings  # noqa: E402


def test_chat_calls_report_tokens_calls_and_duration_under_service_chat():
    m = telemetry.metrics()
    b_in = m.value("cogent_llm_tokens_total", service="chat", model="fake-chat", direction="input")
    b_out = m.value("cogent_llm_tokens_total", service="chat", model="fake-chat", direction="output")
    b_calls = m.value("cogent_llm_calls_total", service="chat", model="fake-chat", kind="answer", outcome="ok")
    llm = GenericFakeChatModel(messages=iter([AIMessage(content="hi", usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})]))
    llm.invoke("question", config={"callbacks": [UsageCallback("fake-chat", "answer")]})
    assert m.value("cogent_llm_tokens_total", service="chat", model="fake-chat", direction="input") == b_in + 120
    assert m.value("cogent_llm_tokens_total", service="chat", model="fake-chat", direction="output") == b_out + 30
    assert m.value("cogent_llm_calls_total", service="chat", model="fake-chat", kind="answer", outcome="ok") == b_calls + 1
    assert m.count("cogent_llm_call_duration_seconds", service="chat", kind="answer") >= 1


def test_a_failed_chat_call_is_counted_as_an_error():
    m = telemetry.metrics()
    before = m.value("cogent_llm_calls_total", service="chat", model="boom", kind="answer", outcome="error")

    class Boom(GenericFakeChatModel):
        def _generate(self, *a, **k):
            raise ConnectionError("model server down")
    with pytest.raises(ConnectionError):
        Boom(messages=iter([])).invoke("q", config={"callbacks": [UsageCallback("boom", "answer")]})
    assert m.value("cogent_llm_calls_total", service="chat", model="boom", kind="answer", outcome="error") == before + 1


def test_embeddings_report_the_tokens_ollama_counted():
    class Client:
        def embed(self, model, texts, **kw):
            return {"embeddings": [[0.0, 1.0] for _ in texts], "prompt_eval_count": 7 * len(texts)}
    e = UsageOllamaEmbeddings(model="nomic-test")
    e._client = Client()
    m = telemetry.metrics()
    before = m.value("cogent_llm_tokens_total", service="chat", model="nomic-test", direction="input")
    assert e.embed_documents(["a", "b", "c"]) == [[0.0, 1.0]] * 3 and e.embed_query("q") == [0.0, 1.0]
    assert m.value("cogent_llm_tokens_total", service="chat", model="nomic-test", direction="input") == before + 7 * 4
    assert m.value("cogent_llm_calls_total", service="chat", model="nomic-test", kind="embed", outcome="ok") >= 2


def test_prometheus_text_has_the_chat_metrics_next_to_the_others():
    telemetry.record_chat_question("answered", 0.2)
    telemetry.record_chat_mcp("lookup_values", "ok", 0.05)
    text = telemetry.metrics().render()
    for name in ("cogent_chat_questions_total", "cogent_chat_question_duration_seconds_bucket", "cogent_chat_mcp_calls_total", "cogent_llm_tokens_total"):
        assert name in text
    assert 'service="chat"' in text


def _dashboard():
    out = subprocess.run([sys.executable, str(ROOT / "deploy/observability/grafana/build_dashboard.py")], capture_output=True, text=True, check=True).stdout
    return json.loads(out), out


def test_dashboard_is_regenerated_and_every_metric_it_uses_exists():
    dash, out = _dashboard()
    committed = (ROOT / "deploy/observability/grafana/dashboards/cogent-observability.json").read_text()
    assert json.loads(committed) == dash, "dashboards/cogent-observability.json is stale: re-run build_dashboard.py"
    exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", []) if t.get("expr")]
    names = {re.sub(r"_(bucket|sum|count)$", "", n) for e in exprs for n in re.findall(r"\bcogent_[a-z0-9_]+", e)}
    server_side = set(re.findall(r"cogent_[a-z0-9_]+", (ROOT / "src/kg_mcp.py").read_text()))        # HTTP / rate-limit / cache / embedder metrics are rendered by the server
    known = set(telemetry.SCHEMA) | server_side
    assert names and names <= known, names - known
    titles = " | ".join(p["title"] for p in dash["panels"])
    assert "Conversational agent" in titles and "by service" in titles.lower()
    assert any('service="chat"' in e for e in exprs) and any("sum by (service" in e for e in exprs)


def test_prometheus_config_scrapes_the_chat_client_too():
    import yaml
    cfg = yaml.safe_load((ROOT / "deploy/observability/prometheus/prometheus.yml").read_text())
    jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert {"cogent", "cogent-chat"} <= set(jobs)
    assert jobs["cogent-chat"]["static_configs"][0]["targets"] == ["127.0.0.1:5000"]
    assert "chat_token" in jobs["cogent-chat"]["authorization"]["credentials_file"]


def test_the_agents_triple_extraction_is_counted_too():
    """The heaviest LLM use of the daily agent (OllamaExtractor) used to be invisible to the metrics; it is now kind="extract"."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"message": {"content": '{"triples": []}'}, "prompt_eval_count": 11, "eval_count": 5}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import kg_ingest
        ex = kg_ingest.OllamaExtractor(model="extract-test", host=f"http://127.0.0.1:{srv.server_port}")
        m = telemetry.metrics()
        svc = telemetry.service()
        before = m.value("cogent_llm_tokens_total", service=svc, model="extract-test", direction="input")
        ex.extract_text("The pump supplies the reactor.")
        assert m.value("cogent_llm_tokens_total", service=svc, model="extract-test", direction="input") == before + 11
        assert m.value("cogent_llm_tokens_total", service=svc, model="extract-test", direction="output") >= 5
        assert m.value("cogent_llm_calls_total", service=svc, model="extract-test", kind="extract", outcome="ok") >= 1
    finally:
        srv.shutdown()
    dead = kg_ingest.OllamaExtractor(model="extract-test", host="http://127.0.0.1:9")
    dead.extract_text("x y z")                                                       # unreachable: falls back to rules, and the failed call is counted
    assert telemetry.metrics().value("cogent_llm_calls_total", model="extract-test", kind="extract", outcome="error") >= 1


def test_the_shared_usage_callback_defaults_to_the_processs_own_service_name():
    from kg_llm_usage import UsageCallback as Shared
    llm = GenericFakeChatModel(messages=iter([AIMessage(content="n", usage_metadata={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11})]))
    m = telemetry.metrics()
    svc = telemetry.service()
    before = m.value("cogent_llm_tokens_total", service=svc, model="narr-test", direction="input")
    llm.invoke("q", config={"callbacks": [Shared("narr-test", "narrate")]})
    assert m.value("cogent_llm_tokens_total", service=svc, model="narr-test", direction="input") == before + 9
