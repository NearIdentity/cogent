"""Scale-out pieces: shared session stores, embedder cache and concurrency limit."""
import multiprocessing as mp
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_sessions as SS  # noqa: E402
import kg_store  # noqa: E402
import kg_tableservice as TS  # noqa: E402


# ---------------------------------------------------------------------------------------------------- sessions
class StubRedis:
    """The few redis calls RedisSessions uses, in memory (expiry honoured)."""
    def __init__(self):
        self.d = {}

    def get(self, k):
        v = self.d.get(k)
        return v[0] if v and v[1] > time.time() else None

    def setex(self, k, ttl, v):
        self.d[k] = (v, time.time() + ttl)

    def delete(self, k):
        return 1 if self.d.pop(k, None) else 0

    def scan_iter(self, match=None):
        pre = (match or "").rstrip("*")
        return [k for k, (_, e) in list(self.d.items()) if k.startswith(pre) and e > time.time()]


@pytest.fixture(params=["memory", "sqlite", "redis"])
def store_kind(request, tmp_path):
    if request.param == "memory":
        return SS.MemorySessions(ttl=60)
    if request.param == "sqlite":
        return SS.make_sessions(f"sqlite:{tmp_path / 'sub' / 's.db'}", 60)
    return SS.RedisSessions(client=StubRedis(), ttl=60)


def test_every_session_backend_behaves_the_same(store_kind):
    s = store_kind
    assert s.get("a:1") == {} and s.count() == 0 and s.clear("a:1") is False
    s.set("a:1", {"units": "off", "approach": "consensus"})
    s.set("b:1", {"units": "broad"})
    assert s.get("a:1") == {"units": "off", "approach": "consensus"} and s.count() == 2
    s.set("a:1", {"units": "strict"})                                                   # replaced, not merged
    assert s.get("a:1") == {"units": "strict"} and s.get("b:1") == {"units": "broad"}
    assert s.clear("a:1") is True and s.get("a:1") == {} and s.count() == 1


def test_sessions_expire():
    for s in (SS.MemorySessions(ttl=0.05), SS.RedisSessions(client=StubRedis(), ttl=1)):
        s.set("k", {"units": "off"})
        assert s.get("k") == {"units": "off"}
    m = SS.MemorySessions(ttl=0.05)
    m.set("k", {"units": "off"})
    time.sleep(0.12)
    assert m.get("k") == {} and m.count() == 0


def _child_set(path, q):
    s = SS.make_sessions(f"sqlite:{path}")
    s.set("web:chat-1", {"approach": "llm"})
    q.put(True)


def test_sqlite_sessions_are_shared_between_processes(tmp_path):
    path = tmp_path / "s.db"
    q = mp.get_context("spawn").Queue()
    p = mp.get_context("spawn").Process(target=_child_set, args=(str(path), q))
    p.start()
    assert q.get(timeout=30) is True
    p.join(30)
    assert SS.make_sessions(f"sqlite:{path}").get("web:chat-1") == {"approach": "llm"}      # written by another process, read here


def test_session_spec_validation():
    assert isinstance(SS.make_sessions(None), SS.MemorySessions) and isinstance(SS.make_sessions("memory"), SS.MemorySessions)
    with pytest.raises(ValueError, match="unknown session store"):
        SS.make_sessions("etcd://x")


def test_table_service_preferences_use_the_given_store(tmp_path):
    shared = SS.make_sessions(f"sqlite:{tmp_path / 's.db'}")
    a = TS.TableService(kg_store.Store(tmp_path), None, sessions=shared)
    b = TS.TableService(kg_store.Store(tmp_path), None, sessions=shared)               # a second worker
    a.set_session("web:x", "strict", units="off")
    assert b.resolve("web:x").units == "off" and b.resolve("web:x").profile == "strict"
    assert b.reset_session("web:x")["cleared"] is True and a.resolve("web:x").profile == "balanced"


# ---------------------------------------------------------------------------------------------------- embedder
class SlowEmbedder:
    spec = "fake:2"

    def __init__(self):
        self.calls, self.gate = [], threading.Event()
        self.gate.set()

    def embed_query(self, q):
        self.calls.append(q)
        self.gate.wait(5)
        return [1.0, float(len(q))]


def service(tmp_path, **kw):
    svc = TS.TableService(kg_store.Store(tmp_path), None, **kw)
    svc.db = SimpleNamespace(meta=lambda c: {"spec": "fake:2"})
    svc.embedder = SlowEmbedder()
    return svc


def test_question_vectors_are_cached(tmp_path):
    svc = service(tmp_path, embed_cache=2)
    prefs = svc.resolve("s")
    v1, _ = svc._qvec(None, "allowable stress", prefs)
    v2, _ = svc._qvec(None, "allowable stress", prefs)
    assert v1 == v2 and svc.embedder.calls == ["allowable stress"] and svc.embed_stats["cache_hits"] == 1
    svc._qvec(None, "second", prefs), svc._qvec(None, "third", prefs)                    # evicts the oldest (size 2)
    svc._qvec(None, "allowable stress", prefs)
    assert svc.embedder.calls.count("allowable stress") == 2


def test_a_busy_embedder_falls_back_to_lexical_search(tmp_path):
    svc = service(tmp_path, embed_concurrency=1, embed_wait=0.2)
    svc.embedder.gate.clear()                                                          # the first call hangs inside the embedder
    prefs = svc.resolve("s")
    t = threading.Thread(target=lambda: svc._qvec(None, "first question", prefs))
    t.start()
    time.sleep(0.1)
    vec, note = svc._qvec(None, "second question", prefs)
    assert vec is None and "busy" in note and svc.embed_stats["busy"] == 1
    svc.embedder.gate.set()
    t.join(5)
    vec, note = svc._qvec(None, "third question", prefs)                               # the slot is free again
    assert vec is not None and note is None


# ---------------------------------------------------------------------------------------------------- rate limit and metrics
import asyncio  # noqa: E402
import json  # noqa: E402

import kg_mcp  # noqa: E402


async def call(app, path="/mcp", headers=None, client="1.2.3.4"):
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    scope = {"type": "http", "method": "POST", "path": path, "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()], "client": (client, 1)}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body.decode()


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def test_rate_limit_is_per_credential_and_refills():
    now = [0.0]
    ops = kg_mcp.Ops(ok_app, rate_per_min=60, burst=3, clock=lambda: now[0])

    def run(**kw):
        return asyncio.run(call(ops, **kw))
    a = {"Authorization": "Bearer aaa"}
    b = {"Authorization": "Bearer bbb"}
    assert [run(headers=a)[0] for _ in range(3)] == [200, 200, 200]
    st, hdr, body = run(headers=a)
    assert st == 429 and int(hdr[b"retry-after"]) >= 1 and "rate limit" in body
    assert run(headers=b)[0] == 200                                               # another credential has its own bucket
    assert run(path="/healthz", headers=a)[0] == 200                              # probes are never limited
    now[0] += 2.0                                                                 # 1 request per second refills
    assert run(headers=a)[0] == 200 and run(headers=a)[0] == 200 and run(headers=a)[0] == 429
    assert ops.rate_limited == 2


def test_anonymous_requests_are_limited_by_address():
    now = [0.0]
    ops = kg_mcp.Ops(ok_app, rate_per_min=60, burst=1, clock=lambda: now[0])
    assert asyncio.run(call(ops, client="9.9.9.9"))[0] == 200 and asyncio.run(call(ops, client="9.9.9.9"))[0] == 429
    assert asyncio.run(call(ops, client="8.8.8.8"))[0] == 200


def test_metrics_need_a_valid_token_and_report_this_process(tmp_path):
    tokens = kg_mcp.TokenStore(tmp_path / "t.json")
    secret = tokens.create("probe")
    store = kg_store.Store(tmp_path)
    ops = kg_mcp.Ops(ok_app, tokens, metrics=True, store=store, tables=TS.TableService(store, None, embed_cache=4))
    assert asyncio.run(call(ops, "/metrics"))[0] == 401
    assert asyncio.run(call(ops, "/metrics", {"Authorization": "Bearer wrong"}))[0] == 401
    asyncio.run(call(ops, "/mcp", {"Authorization": f"Bearer {secret}"}))
    st, hdr, text = asyncio.run(call(ops, "/metrics", {"Authorization": f"Bearer {secret}"}))
    assert st == 200 and b"text/plain" in hdr[b"content-type"]
    assert 'cogent_http_requests_total{pid="' in text and 'status="200"} 1' in text and "cogent_graph_cache_graphs" in text
    assert 'cogent_embed_events_total' in text and 'cogent_sessions{' in text and 'store="memory"' in text


def test_metrics_are_off_unless_enabled(tmp_path):
    ops = kg_mcp.Ops(ok_app, None, rate_per_min=0, metrics=False)
    assert asyncio.run(call(ops, "/metrics"))[1:] == ({b"content-type": b"text/plain"}, "ok")      # passed through to the app


# ---------------------------------------------------------------------------------------------------- several workers
import os  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import urllib.request  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="workers need fork")
def test_two_workers_share_one_port_and_a_session_store(tmp_path):
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    env = {**os.environ, "COGENT_MCP_TOKEN": "test-token-123", "PYTHONPATH": str(SRC)}
    cmd = [sys.executable, str(SRC / "kg_mcp.py"), "--home", str(tmp_path / "home"), "serve", "--port", str(port), "--workers", "2",
           "--embedder", "none", "--public-host", "*", "--session-store", f"sqlite:{tmp_path / 'sess.db'}", "--metrics", "--rate-limit", "6000"]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        pids = set()
        deadline = time.time() + 60
        while time.time() < deadline and len(pids) < 2:
            try:
                with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"Connection": "close"}), timeout=2) as r:
                    pids.add(json.loads(r.read())["pid"])
            except OSError:
                time.sleep(0.2)
        assert len(pids) == 2, f"requests were answered by {pids}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/metrics", headers={"Authorization": "Bearer test-token-123"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert b"cogent_http_requests_total" in r.read()
        # a worker that dies is replaced
        victim = next(iter(pids))
        os.kill(victim, 9)
        time.sleep(3)
        again = set()
        for _ in range(60):
            try:
                with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/healthz", headers={"Connection": "close"}), timeout=2) as r:
                    again.add(json.loads(r.read())["pid"])
            except OSError:
                pass
        assert victim not in again and len(again) >= 1
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    assert "2 worker process(es) started" in out and "restarted as" in out, out[-2000:]
