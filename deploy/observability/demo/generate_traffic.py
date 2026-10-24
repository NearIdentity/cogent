#!/usr/bin/env python3
"""Produce realistic telemetry for the Grafana dashboard without any real sources: this is how docs/img/grafana-dashboard.png was made.

It starts, in one process tree:
  * a private PostgreSQL + pgvector cluster (needs the PostgreSQL server binaries and the pgvector extension, as the test suite does),
  * the mock web site of the tests (tests/mock_site.py) that serves the standards, regulatory-guide index pages and a page with tables,
  * a mock Ollama (chat + embeddings) that reports token counts, sometimes invents a column, sometimes splits the judge's votes, and can fail on demand,
  * the MCP server (``kg_mcp.py serve --metrics``) on the agent home, plus a client that calls its tools every second or so,
and then runs the agent a number of times: quiet runs, a new edition left for review and then applied, retries, and finally runs against a site that
has gone away, so that the failure-rate alert has something to fire on.

    python3 deploy/observability/demo/generate_traffic.py --work /tmp/cogent-demo --minutes 12
    # in another shell: Prometheus + Grafana as in docs/observability.md, with the token printed at the start

Everything is local and temporary. The demo model "demo-llm:8b" is given an illustrative price so that the cost panels are not flat zero.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import kg_agent  # noqa: E402
import kg_ingest  # noqa: E402
import kg_llmjoin  # noqa: E402
import kg_mcp  # noqa: E402
from mock_site import Site  # noqa: E402


def free_port() -> int:
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


# ---------------------------------------------------------------------------------------------------- PostgreSQL
class Postgres:
    def __init__(self, root: Path):
        import glob
        bins = sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
        if not bins:
            raise SystemExit("PostgreSQL server binaries not found (apt install postgresql postgresql-16-pgvector)")
        self.bin, self.root, self.port = Path(bins[0]), root, free_port()
        self.as_root = os.geteuid() == 0
        root.mkdir(parents=True, exist_ok=True)
        if self.as_root:
            shutil.chown(root, user="postgres")
        self.data = root / "data"
        for cmd in ([self.bin / "initdb", "-D", self.data, "-A", "trust", "-U", "postgres"],
                    [self.bin / "pg_ctl", "-D", self.data, "-o", f"-p {self.port} -k {root} -c listen_addresses=127.0.0.1", "-l", root / "log", "-w", "start"]):
            r = self._run(cmd)
            if r.returncode:
                raise SystemExit(f"postgres: {r.stderr[-300:]}")
        self.dsn = f"postgresql://postgres@127.0.0.1:{self.port}/postgres"
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")

    def _run(self, cmd):
        line = " ".join(shlex.quote(str(c)) for c in cmd)
        return subprocess.run(["su", "postgres", "-c", line] if self.as_root else ["sh", "-c", line], capture_output=True, text=True)

    def stop(self):
        self._run([self.bin / "pg_ctl", "-D", self.data, "-m", "fast", "stop"])


# ---------------------------------------------------------------------------------------------------- mock Ollama
class MockOllama:
    """Chat + embeddings with token counts. ``fail_chat`` = number of next chat calls that answer 500; ``invent`` = share of suggestions naming a column that does not exist."""

    def __init__(self):
        self.fail_chat, self.invent, self.calls = 0, 0.35, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                time.sleep(random.uniform(0.01, 0.06))
                outer.calls += 1
                if self.path == "/api/chat":
                    if outer.fail_chat > 0:
                        outer.fail_chat -= 1
                        self.send_response(500)
                        self.end_headers()
                        return
                    msgs = body.get("messages", [])
                    system, user = msgs[0]["content"], msgs[-1]["content"]
                    if system == kg_llmjoin.SUGGEST_SYSTEM:
                        joins = [{"left_column": "Seismic Category", "right_column": "Seismic Category", "relationship": "join", "cardinality": "N:M",
                                  "confidence": round(random.uniform(0.7, 0.95), 2), "rationale": "same category codes"}]
                        if random.random() < outer.invent:
                            joins.append({"left_column": "Seismic Class", "right_column": "Category Code", "relationship": "join", "cardinality": "N:M",
                                          "confidence": 0.6, "rationale": "similar meaning"})
                        answer = {"joins": joins}
                    else:
                        verdict = random.choices(["accept", "reject", "uncertain"], [0.7, 0.2, 0.1])[0]
                        answer = {"verdict": verdict, "confidence": round(random.uniform(0.5, 0.95), 2), "reasons": ["values overlap"], "problems": []}
                    out = {"message": {"content": json.dumps(answer)}, "prompt_eval_count": len(user) // 4 + len(system) // 4, "eval_count": random.randint(25, 70)}
                else:
                    inputs = body.get("input") or [body.get("prompt", "")]
                    out = {"embeddings": [[random.random() for _ in range(64)] for _ in inputs], "prompt_eval_count": sum(max(1, len(t) // 4) for t in inputs)}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


# ---------------------------------------------------------------------------------------------------- MCP client traffic
def tool_traffic(port: int, token: str, stop: threading.Event) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    calls = [("get_overview", {}), ("list_entries", {}), ("search_graph", {"ref": "standard:2", "query": "RG"}), ("search_graph", {"ref": "standard:4", "query": "seismic category"}),
             ("list_tables", {}), ("lookup_values", {"question": "damping for welded steel"}), ("lookup_values", {"question": "annual probability of exceedance SDC-4"}),
             ("get_warnings", {}), ("get_entry_metadata", {"entry": "standard:2"}), ("get_node", {"ref": "standard:2", "node_id": "no-such-node"}),
             ("get_dependents", {"entry": "standard:2"}), ("get_outline", {"ref": "standard:4"})]

    async def go():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                while not stop.is_set():
                    name, args = random.choice(calls)
                    try:
                        await s.call_tool(name, args)
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(0.2, 1.2))
    while not stop.is_set():
        try:
            asyncio.run(go())
        except Exception:
            time.sleep(2)


# ---------------------------------------------------------------------------------------------------- the story
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, default=Path(tempfile.mkdtemp(prefix="cogent_demo_")))
    ap.add_argument("--minutes", type=float, default=12.0, help="how long to keep producing traffic")
    ap.add_argument("--step", type=float, default=40.0, help="seconds between agent runs")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    work: Path = a.work
    work.mkdir(parents=True, exist_ok=True)
    os.environ["COGENT_LLM_PRICES"] = json.dumps({"demo-llm:8b": {"in": 0.20, "out": 0.60}, "nomic-embed-text": {"in": 0.02, "out": 0.0}})
    kg_ingest.set_pdf_engine("basic")

    pg = Postgres(work / "pg")
    site = Site()
    ollama = MockOllama()
    home = work / "home"
    b = site.base
    std = {"meta": {}, "standards": [
        {"id": 1, "document": "10 CFR 50.69", "links": [{"text": "eCFR", "url": f"{b}/current/title-10/section-50.69"}],
         "update_tracking_urls": [f"{b}/api/versioner/v1/versions/title-10.json?part=50", f"{b}/agencies/nuclear-regulatory-commission"]},
        {"id": 2, "document": "NRC RG 1.233", "links": [{"text": "RG 1.233 Rev. 0", "url": f"{b}/docs/rg1233_rev0.pdf"}], "update_tracking_urls": [f"{b}/index/rg.html"]},
        {"id": 3, "document": "DOE-STD-1271-2025, Authorization Pathway", "links": [{"text": "PDF", "url": f"{b}/docs/doe1271_2025.pdf"}], "update_tracking_urls": [f"{b}/index/doe.html"]},
        {"id": 4, "document": "Seismic Design Criteria for Nuclear Facilities", "links": [{"text": "HTML", "url": f"{b}/docs/seismic_tables.html"}], "update_tracking_urls": []}]}
    rep = {"meta": {}, "reports": [{"id": 1, "report": "Licensing basis report", "standards_to_comply_with_ids": [1, 2], "template_links": [], "update_tracking_urls": []},
                                   {"id": 2, "report": "Authorization report", "standards_to_comply_with_ids": [3], "template_links": [], "update_tracking_urls": []}]}
    sp, rp = work / "standards.json", work / "reports.json"
    sp.write_text(json.dumps(std))
    rp.write_text(json.dumps(rep))

    def settings(**kw):
        base = dict(home=home, standards=sp, reports=rp, min_interval=0, tables=True, tabledb_dsn=pg.dsn, embedder="ollama", embed_model="nomic-embed-text",
                    ollama_host=ollama.url, llm_joins=True, join_model="demo-llm:8b", join_samples=2, join_judge_runs=3, join_max_pairs=6)
        base.update(kw)
        return kg_agent.Settings(**base)

    # the first run builds the data the server needs; then the server starts with --metrics
    print("== first run (bootstrap)", flush=True)
    kg_agent.run_once(settings())
    tokens = kg_mcp.TokenStore(home / "mcp" / "tokens.json")
    token = tokens.create("demo")
    (work / "prometheus_token").write_text(tokens.create("prometheus"))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "COGENT_TABLEDB_DSN": pg.dsn}
    server = subprocess.Popen([sys.executable, str(ROOT / "src/kg_mcp.py"), "--home", str(home), "serve", "--port", str(a.port), "--public-host", "*",
                               "--standards", str(sp), "--reports", str(rp), "--metrics", "--embedder", "ollama", "--embed-model", "nomic-embed-text",
                               "--ollama-host", ollama.url, "--llm-model", "demo-llm:8b", "--rate-limit", "6000"], env=env,
                              stdout=open(work / "mcp.log", "w"), stderr=subprocess.STDOUT)
    stop = threading.Event()
    threading.Thread(target=tool_traffic, args=(a.port, token, stop), daemon=True).start()
    print(f"MCP server on :{a.port} (log {work / 'mcp.log'}); Prometheus token file: {work / 'prometheus_token'}", flush=True)

    orig_route = site.route
    story = [("quiet run", {}, None),
             ("new RG revision / DOE edition, left for review", {"auto_apply": False}, 3),
             ("the same findings applied automatically", {}, None),
             ("Ollama flaky: retries", {"force": True}, "flaky"),
             ("everything down: failures", {"force": True}, "down"),
             ("still down", {"force": True}, "down")]
    t_end = time.time() + a.minutes * 60
    i = 0
    try:
        while time.time() < t_end:
            label, kw, event = story[min(i, len(story) - 1)] if i < len(story) else ("still down", {"force": True}, "down")
            if event == 3:
                site.day = 3
            elif event == "flaky":
                ollama.fail_chat = 5
            elif event == "down":
                site.route = lambda path, q: None                             # every source answers 404
            print(f"== run {i + 2}: {label}", flush=True)
            try:
                st = kg_agent.run_once(settings(**kw))
                m = st.get("metrics") or {}
                print(f"   success={m.get('success')} errors={m.get('errors')} failures={m.get('failures')} retries={m.get('retries')} tokens={m.get('tokens')}", flush=True)
            except Exception as exc:                                       # noqa: BLE001
                print("   run raised:", exc, flush=True)
            i += 1
            for _ in range(int(a.step)):
                if time.time() >= t_end:
                    break
                time.sleep(1)
    finally:
        site.route = orig_route
        stop.set()
        print("== done; leaving the MCP server and the data in place (Ctrl+C to stop)", flush=True)
        try:
            server.wait(timeout=None)
        except KeyboardInterrupt:
            pass
        server.terminate()
        pg.stop()
        site.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
