"""Honest user agent, per-host cooldowns, deferral, manual links, fetchcheck and fetch-problem reporting,
all against local mock servers (no network)."""
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_agent  # noqa: E402
import kg_hosts  # noqa: E402
import kg_ingest  # noqa: E402
import kg_tools as T  # noqa: E402
from mock_site import make_pdf  # noqa: E402


class Server:
    """A scriptable web server: per path a (status, headers, body); optionally refuses browser-looking user agents."""

    def __init__(self):
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.hits: list[tuple[str, str]] = []            # (path, user agent)
        self.block_browser_ua = False
        self.block_all = 0                               # status to answer everything with (0 = off)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ua = self.headers.get("User-Agent", "")
                outer.hits.append((self.path, ua))
                if outer.block_all or (outer.block_browser_ua and ua.startswith("Mozilla")):
                    self.send_response(outer.block_all or 403)
                    self.send_header("x-reference-error", "18.deadbeef.1")
                    self.send_header("Server", "AkamaiGHost")
                    self.end_headers()
                    self.wfile.write(b"<html>Access Denied</html>")
                    return
                status, headers, body = outer.routes.get(self.path, (404, {}, b"not here"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add(self, path, body, ctype="application/pdf", status=200, **headers):
        self.routes[path] = (status, {"Content-Type": ctype, **headers}, body)

    def count(self, path=None):
        return sum(1 for p, _ in self.hits if path is None or p == path)

    def close(self):
        self.httpd.shutdown()


@pytest.fixture()
def srv():
    s = Server()
    yield s
    s.close()


@pytest.fixture()
def srv2():
    s = Server()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def fresh_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("KG_USER_AGENT", raising=False)
    monkeypatch.delenv("KG_BROWSER_FALLBACK", raising=False)
    monkeypatch.delenv("NRC_ADAMS_API_KEY", raising=False)
    kg_hosts.configure(tmp_path / "hosts.json", default_interval=0)
    kg_ingest.reset_image_warnings()
    yield
    kg_hosts.configure(None, persist=False)


def no_browser(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a headless browser must not be started")
    monkeypatch.setattr(kg_ingest, "_fetch_browser", boom)


# ---------------------------------------------------------------------------- user agent

def test_user_agent_is_honest_and_overridable(srv, monkeypatch):
    srv.block_browser_ua = True                    # a filter that refuses anything posing as a browser
    srv.add("/a.pdf", b"%PDF-1.4 x")
    r = T.fetch(srv.base + "/a.pdf", min_interval=0)
    assert r.ok and srv.hits[-1][1].startswith("cogent-kg-agent/")
    assert not srv.hits[-1][1].startswith("Mozilla")
    monkeypatch.setenv("KG_USER_AGENT", "my-lab-bot/2 (ops@example.org)")
    T.fetch(srv.base + "/a.pdf", min_interval=0)
    assert srv.hits[-1][1] == "my-lab-bot/2 (ops@example.org)"
    kg_ingest._fetch_http(srv.base + "/a.pdf", 5)          # the ingest path honours it too
    assert srv.hits[-1][1] == "my-lab-bot/2 (ops@example.org)"
    assert T.UA == "my-lab-bot/2 (ops@example.org)"


# ---------------------------------------------------------------------------- cooldown

def test_403_starts_cooldown_without_retry_or_browser(srv, monkeypatch):
    no_browser(monkeypatch)
    srv.block_all = 403
    r = T.fetch(srv.base + "/x.pdf", min_interval=0, retries=2)
    assert r.blocked and r.status == 403 and not r.deferred
    assert "bot filter" in r.error and "HTTP 403" in r.error
    assert srv.count() == 1                                # no retries
    assert r.headers.get("x-reference-error") or r.headers.get("X-Reference-Error")
    r2 = T.fetch(srv.base + "/y.pdf", min_interval=0)
    assert r2.deferred and r2.host_problem and "deferred: host is blocking this client" in r2.error
    assert srv.count() == 1                                # nothing requested during the cooldown
    assert kg_hosts.HOSTS.cooldown(srv.host)["level"] == 1


@pytest.mark.parametrize("status,headers", [(429, {}), (503, {"Retry-After": "7200"})])
def test_429_and_503_retry_after_start_cooldown(srv, status, headers):
    srv.add("/p", b"busy", "text/plain", status=status, **headers)
    r = T.fetch(srv.base + "/p", min_interval=0, backoff=0)
    assert r.blocked and srv.count() == 1
    assert T.fetch(srv.base + "/p", min_interval=0).deferred


def test_plain_503_is_retried_and_not_a_block(srv):
    srv.add("/p", b"down", "text/plain", status=503)
    r = T.fetch(srv.base + "/p", min_interval=0, retries=2, backoff=0)
    assert not r.blocked and r.status == 503 and srv.count() == 3
    assert kg_hosts.HOSTS.cooldown(srv.host) is None


def test_404_is_just_an_error(srv):
    r = T.fetch(srv.base + "/missing", min_interval=0)
    assert r.status == 404 and not r.host_problem and kg_hosts.HOSTS.cooldown(srv.host) is None


def test_cooldown_persists_grows_and_clears(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "h.json"
    pol = kg_hosts.HostPolicy(path, clock=lambda: now[0])
    pol.block("www.nrc.gov", "blocked by a bot filter (HTTP 403 from host)")
    assert path.exists() and "www.nrc.gov" in json.loads(path.read_text())
    again = kg_hosts.HostPolicy(path, clock=lambda: now[0])          # the next run
    cd = again.cooldown("www.nrc.gov")
    assert cd and cd["until"] - now[0] == 6 * 3600
    now[0] += 6 * 3600 + 1                                          # cooldown over, host usable again ...
    again2 = kg_hosts.HostPolicy(path, clock=lambda: now[0])
    assert again2.cooldown("www.nrc.gov") is None
    again2.block("www.nrc.gov", "blocked")                          # ... but blocked again: twice as long
    assert again2.state["www.nrc.gov"]["level"] == 2
    assert again2.cooldown("www.nrc.gov")["until"] - now[0] == 12 * 3600
    for _ in range(10):
        again2.block("www.nrc.gov", "blocked")
    assert again2.cooldown("www.nrc.gov")["until"] - now[0] == kg_hosts.MAX_COOLDOWN_H * 3600   # capped
    now[0] += 10 ** 7
    again3 = kg_hosts.HostPolicy(path, clock=lambda: now[0])
    again3.success("www.nrc.gov")                                    # a good response resets the ladder
    assert "www.nrc.gov" not in again3.state and "www.nrc.gov" not in json.loads(path.read_text())


def test_retry_after_longer_than_default_wins():
    pol = kg_hosts.HostPolicy(clock=lambda: 0.0, persist=False)
    pol.block("h", "x", retry_after=48 * 3600)
    assert pol.cooldown("h")["until"] == 48 * 3600
    assert kg_hosts.parse_retry_after("120") == 120 and kg_hosts.parse_retry_after("") is None
    assert kg_hosts.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT", now=0) > 10 ** 9


def test_opt_in_browser_fallback_respects_cooldown(srv, monkeypatch):
    srv.block_all = 403
    calls = []

    def browser(url, timeout):
        calls.append(url)
        return b"%PDF-1.4 via browser"
    monkeypatch.setattr(kg_ingest, "_fetch_browser", browser)
    r = T.fetch(srv.base + "/x.pdf", min_interval=0)                      # default: off
    assert r.blocked and not calls
    kg_hosts.configure(None, default_interval=0, persist=False)
    monkeypatch.setenv("KG_BROWSER_FALLBACK", "1")                        # opt in
    r = T.fetch(srv.base + "/x.pdf", min_interval=0)
    assert r.ok and r.via == "browser" and len(calls) == 1
    kg_hosts.configure(None, default_interval=0, persist=False)
    monkeypatch.setattr(kg_ingest, "_fetch_browser", lambda u, t: (_ for _ in ()).throw(RuntimeError("HTTP 403 Forbidden")))
    r = T.fetch(srv.base + "/x.pdf", min_interval=0)                      # both strategies fail -> cooldown
    assert r.blocked and "browser fallback failed" in r.error and kg_hosts.HOSTS.cooldown(srv.host)
    monkeypatch.setattr(kg_ingest, "_fetch_browser", browser)
    assert T.fetch(srv.base + "/x.pdf", min_interval=0).deferred and len(calls) == 1   # no browser during a cooldown


def test_image_fetch_one_warning_per_host(srv, caplog):
    srv.block_all = 403
    e = lambda n: kg_ingest.Element(id=f"f{n}", kind="figure", meta={"src_abs": f"{srv.base}/img{n}.png"})
    with caplog.at_level(logging.WARNING):
        for n in range(4):
            assert kg_ingest._figure_bytes(e(n)) is None
    assert srv.count() == 1
    assert len([r for r in caplog.records if "not downloaded" in r.getMessage()]) == 1


# ---------------------------------------------------------------------------- pacing and interleaving

def test_pacing_defaults_and_jitter():
    now = [1000.0]
    slept = []

    def sleep(sec):
        slept.append(sec)
        now[0] += sec
    pol = kg_hosts.HostPolicy(default_interval=1.0, clock=lambda: now[0], sleep=sleep, persist=False)
    pol.pace("www.nrc.gov")
    pol.pace("www.nrc.gov")
    assert len(slept) == 1 and 3.0 <= slept[0] <= 5.0          # ~4 s +/- 25 %, none before the first request
    pol.pace("example.org")
    pol.pace("example.org")
    assert len(slept) == 2 and 0.74 <= slept[1] <= 1.26
    custom = kg_hosts.HostPolicy(intervals={"www.nrc.gov": 10}, default_interval=0, persist=False)
    assert custom.interval("www.nrc.gov") == 10 and custom.interval("other") == 0


def test_interleave_alternates_hosts_and_keeps_order():
    urls = ["a/1", "a/2", "a/3", "b/1", "c/1", "b/2"]
    out = kg_hosts.interleave(urls, lambda u: u.split("/")[0])
    assert out == ["a/1", "b/1", "c/1", "a/2", "b/2", "a/3"]
    assert sorted(out) == sorted(urls)


# ---------------------------------------------------------------------------- the agent

def make_env(tmp_path, standards: list[dict], reports=None):
    sp, rp = tmp_path / "standards.json", tmp_path / "reports.json"
    sp.write_text(json.dumps({"meta": {"count": len(standards)}, "standards": standards}))
    rp.write_text(json.dumps({"meta": {"count": 0}, "reports": reports or []}))
    return kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0, assets=False, stale_after=1)


def doc_state(s, url):
    return json.loads((s.home / "staging" / "documents" / kg_agent.docid_of(url) / "state.json").read_text())


def test_blocked_host_defers_without_stale_and_keeps_kg(tmp_path, srv, srv2, monkeypatch):
    no_browser(monkeypatch)
    kg_ingest.set_pdf_engine("basic")
    srv.add("/good.pdf", make_pdf("Good", "Good text."))
    srv2.add("/a.pdf", make_pdf("A", "Doc A text."))
    srv2.add("/b.pdf", make_pdf("B", "Doc B text."))
    std = [{"id": 1, "document": "Good doc", "links": [{"text": "pdf", "url": srv.base + "/good.pdf"}], "update_tracking_urls": []},
           {"id": 2, "document": "Doc A", "links": [{"text": "pdf", "url": srv2.base + "/a.pdf"}], "update_tracking_urls": [srv2.base + "/idx.html"]},
           {"id": 3, "document": "Doc B", "links": [{"text": "pdf", "url": srv2.base + "/b.pdf"}], "update_tracking_urls": []}]
    s = make_env(tmp_path, std)
    st = kg_agent.run_once(s)
    assert st["summary"]["docs_created"] == 3 and st["summary"]["errors"] == 1          # the tracker page /idx.html is a plain 404
    assert "tracker URL not found" in " ".join(st["summary"]["error_messages"])
    assert "update_tracking_urls" in " ".join(st["summary"]["error_messages"])
    assert st["summary"]["fetch_problems"][srv2.host]["gone"] == [srv2.base + "/idx.html"]
    # the host now blocks this client
    srv2.block_all = 403
    before = srv2.count()
    st = kg_agent.run_once(s)
    sm = st["summary"]
    assert sm["docs_error"] == 0 and sm["errors"] == 0, st["report"]
    assert sm["docs_deferred"] == 2 and sm["docs_unchanged"] == 1
    assert srv2.count() - before == 1                       # ONE blocked request; everything else on that host was deferred
    assert sm["fetch_problems"][srv2.host]["deferred"] >= 1
    assert "bot filter" in sm["fetch_problems"][srv2.host]["blocked"]
    assert "### Fetch problems by host" in st["report"]
    for u in ("/a.pdf", "/b.pdf"):
        d = doc_state(s, srv2.base + u)
        assert d["consecutive_errors"] == 0 and "STALE" not in (d["last_error"] or "")
    assert (s.home / "state" / "hosts.json").exists()
    # more runs: nothing requested, never STALE, KGs untouched
    for _ in range(3):
        st = kg_agent.run_once(s)
        assert st["summary"]["docs_error"] == 0 and "STALE" not in st["report"].split("### Fetch problems")[0]
    assert srv2.count() - before == 1
    assert all(act["action"] == "skipped" for act in json.loads(json.dumps(st["doc_actions"])) if srv2.host in act["url"])
    assert list((s.home / "staging" / "documents").glob("*/kg.json"))


def test_cooldown_survives_a_new_process(tmp_path, srv):
    srv.block_all = 403
    s = make_env(tmp_path, [{"id": 1, "document": "D", "links": [{"text": "p", "url": srv.base + "/d.pdf"}], "update_tracking_urls": []}])
    kg_agent.run_once(s)
    assert srv.count() == 1
    kg_hosts.configure(None, persist=False)                 # "restart": a new process knows nothing in memory
    srv.block_all = 0
    srv.add("/d.pdf", make_pdf("D", "text"))
    st = kg_agent.run_once(s)
    assert srv.count() == 1 and st["summary"]["docs_deferred"] == 1 and st["summary"]["docs_error"] == 0


def test_manual_link_is_never_requested_and_is_listed(tmp_path, srv):
    kg_ingest.set_pdf_engine("basic")
    url = srv.base + "/docs/ML99999A001.pdf"
    srv.add("/docs/ML99999A001.pdf", make_pdf("Manual", "Manual text."))
    s = make_env(tmp_path, [{"id": 7, "document": "Manual doc", "links": [{"text": "pdf", "url": url, "manual": True}], "update_tracking_urls": []}])
    st = kg_agent.run_once(s)
    assert srv.count() == 0
    alerts = [a for a in st["alerts"] if a["kind"] == "manual_download_needed"]
    assert len(alerts) == 1 and alerts[0]["details"]["url"] == url and not alerts[0]["details"]["present"]
    save_to = Path(alerts[0]["details"]["save_to"])
    assert save_to.name == "ML99999A001.pdf" and save_to.parent == s.home / "local" / "standards" / "7"
    assert url in st["report"] and str(save_to) in st["report"] and "MANUAL DOWNLOAD NEEDED" in st["report"]
    assert save_to.parent.is_dir()
    save_to.write_bytes(make_pdf("Manual", "Manual text."))                    # the user downloads it in a browser
    st = kg_agent.run_once(s)
    assert srv.count() == 0
    assert not [a for a in st["alerts"] if a["kind"] == "manual_download_needed"]
    assert st["summary"]["docs_created"] == 1 and st["summary"]["docs_error"] == 0


def test_store_page_403_is_an_info_alert_not_an_error(tmp_path, srv, monkeypatch, caplog):
    srv.block_all = 403
    monkeypatch.setattr(kg_agent, "PAYWALL_HOSTS", ("127.0.0.1",))
    s = make_env(tmp_path, [{"id": 9, "document": "ASCE 43-19", "paywalled": True, "links": [{"text": "store", "url": srv.base + "/doi/book/1"}],
                             "update_tracking_urls": [srv.base + "/doi/book/1"]}])
    with caplog.at_level(logging.INFO):
        st = kg_agent.run_once(s)
    assert st["summary"]["errors"] == 0 and st["summary"]["docs_error"] == 0, st["report"]
    a = [x for x in st["alerts"] if x["kind"] == "store_page_blocked"]
    assert len(a) == 1 and a[0]["severity"] == "info" and a[0]["entry"] == "standard:9"
    rec = [r for r in caplog.records if r.getMessage().startswith("[store_page_blocked]")]
    assert rec and rec[0].levelno == logging.INFO                          # info alerts are not logged as warnings
    warn = [r for r in caplog.records if r.getMessage().startswith("[no_local_copy]")]
    assert warn and warn[0].levelno == logging.WARNING                     # real warnings still are
    assert srv.count() == 1


def test_tracker_on_blocked_host_is_deferred_not_stale(tmp_path, srv):
    srv.add("/rg.html", b"<html><body><table><tr><td>1.233</td><td>Rev. 0</td></tr></table></body></html>", "text/html")
    s = make_env(tmp_path, [{"id": 2, "document": "NRC RG 1.233", "links": [], "update_tracking_urls": [srv.base + "/rg.html"]}])
    kg_agent.run_once(s)
    srv.block_all = 403
    for _ in range(3):
        st = kg_agent.run_once(s)
        assert st["summary"]["errors"] == 0, st["report"]
    ts = json.loads((s.home / "state" / "trackers.json").read_text())[srv.base + "/rg.html"]
    assert ts["consecutive_errors"] == 0
    assert st["tracker_log"][0]["deferred"] and not st["tracker_log"][0]["stale"]


# ---------------------------------------------------------------------------- fetchcheck

def test_fetchcheck_one_request_and_report(tmp_path, srv):
    srv.block_all = 403
    s = make_env(tmp_path, [])
    lines = []
    rc = kg_agent.fetchcheck(s, srv.base + "/x.pdf", out=lines.append)
    text = "\n".join(lines)
    assert rc == 1 and srv.count() == 1
    assert "cogent-kg-agent/" in text and "status      403" in text
    assert "x-reference-error 18.deadbeef.1" in text.replace("  ", " ") or "x-reference-error" in text
    assert "akamaighost" in text.lower() and "Access Denied" in text and "bot filter" in text
    lines.clear()
    assert kg_agent.fetchcheck(s, srv.base + "/x.pdf", out=lines.append) == 2         # in a cooldown: not requested
    assert srv.count() == 1 and "NOT REQUESTED" in "\n".join(lines)
    srv.block_all = 0
    srv.add("/x.pdf", b"%PDF-1.4 hello")
    lines.clear()
    assert kg_agent.fetchcheck(s, srv.base + "/x.pdf", ignore_cooldown=True, out=lines.append) == 0
    assert "binary (PDF)" in "\n".join(lines)


def test_fetchcheck_404_verdict(tmp_path, srv):
    lines = []
    kg_agent.fetchcheck(make_env(tmp_path, []), srv.base + "/gone", out=lines.append)
    assert "not found" in "\n".join(lines) and "hide it from this client" in "\n".join(lines)
