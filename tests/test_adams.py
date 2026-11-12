"""ADAMS Public Search adapter: accession parsing, fingerprints, a mock APS server, pacing / cooldown, change detection
inside the agent, and key hygiene. The fixture is synthetic (no NRC text)."""
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_adams  # noqa: E402
import kg_agent  # noqa: E402
import kg_hosts  # noqa: E402
import kg_ingest  # noqa: E402
from mock_site import make_pdf  # noqa: E402
from test_fetch_blocking import Server, make_env, doc_state  # noqa: E402

KEY = "k3y-NOT-A-REAL-KEY-0123456789abcdef"


def record(acc="ML18019A922", added="2018-01-19T10:11:12Z", date="2018-01-10", content="Synthetic text of a synthetic letter."):
    """Shaped like a real response: top-level score / highlights / semanticSearch / document."""
    return {"score": 1.0, "highlights": [], "semanticSearch": None,
            "document": {"AccessionNumber": acc, "AccessionNumberLower": acc.lower(), "DocumentTitle": "Synthetic title",
                         "DocumentType": "Letter", "DateAddedTimestamp": added, "DocumentDate": date, "DocumentSize": "12345",
                         "EstimatedPageCount": 3, "Url": f"https://example.invalid/{acc}", "content": content}}


class Aps:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self.hits: list[tuple[str, str, str]] = []       # (method, path, key header)
        self.status = 200
        self.headers: dict = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, method):
                outer.hits.append((method, self.path, self.headers.get("Ocp-Apim-Subscription-Key", "")))
                if self.headers.get("Ocp-Apim-Subscription-Key") != KEY:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"message":"Access denied due to invalid subscription key."}')
                    return
                if outer.status != 200:
                    self.send_response(outer.status)
                    for k, v in outer.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    return
                if method == "POST":
                    body = {"results": list(outer.records.values())}
                else:
                    rec = outer.records.get(self.path.rsplit("/", 1)[-1])
                    if rec is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = rec
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer("GET")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._answer("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture()
def aps():
    s = Aps()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("NRC_ADAMS_API_KEY", raising=False)
    monkeypatch.delenv("KG_USER_AGENT", raising=False)
    kg_hosts.configure(tmp_path / "hosts.json", default_interval=0)
    yield
    kg_hosts.configure(None, persist=False)


def test_accession_numbers_from_urls():
    f = kg_adams.accession_from_url
    assert f("https://www.nrc.gov/docs/ML18/ML18019A922.pdf") == "ML18019A922"
    assert f("https://www.nrc.gov/docs/ML0706/ML070630003.pdf") == "ML070630003"
    assert f("https://www.nrc.gov/docs/ML0913/ml091320468.pdf") == "ML091320468"          # lower case in the configuration
    assert f("https://www.nrc.gov/reading-rm/doc-collections/reg-guides/index.html") is None
    assert f("https://example.org/docs/ML18019A922.pdf") is None                        # only NRC hosts
    assert f("https://www.nrc.gov/docs/ML18019A92.pdf") is None                         # malformed


def test_fingerprint_both_shapes_and_extra_fields():
    rec = record()
    fp1, fields = kg_adams.fingerprint(rec)                       # envelope
    fp2, _ = kg_adams.fingerprint(rec["document"])                # the bare record
    assert fp1 == fp2 and fp1.startswith("ML18019A922|2018-01-19T10:11:12Z|2018-01-10|")
    assert fields["content_chars"] == len("Synthetic text of a synthetic letter.")
    rec["document"]["DocumentSize"] = "999"                       # the undocumented field is tolerated and ignored
    assert kg_adams.fingerprint(rec)[0] == fp1
    for change in ({"content": "other"}, {"DocumentDate": "2019-01-01"}, {"DateAddedTimestamp": "2020-01-01T00:00:00Z"}):
        r2 = record(); r2["document"].update(change)
        assert kg_adams.fingerprint(r2)[0] != fp1
    r3 = record(); r3["document"]["DocumentType"] = "Something else"           # DocumentType is not trusted: not part of the fingerprint
    assert kg_adams.fingerprint(r3)[0] == fp1
    with pytest.raises(ValueError):
        kg_adams.fingerprint({"document": {}})


def test_get_document_sends_key_header_and_search(aps):
    aps.records["ML18019A922"] = record()
    c = kg_adams.AdamsClient(KEY, aps.base)
    r = c.get_document("ml18019a922")
    assert r.ok and r.accession == "ML18019A922" and r.raw_keys == ["document", "highlights", "score", "semanticSearch"]
    assert aps.hits == [("GET", "/aps/api/search/ML18019A922", KEY)] and c.requests == 1
    recs, err = c.search({"q": "synthetic", "skip": 0})
    assert not err and recs[0]["AccessionNumber"] == "ML18019A922" and aps.hits[-1][:2] == ("POST", "/aps/api/search")
    assert not c.get_document("not-an-accession").ok and c.requests == 2         # rejected locally, no request


def test_wrong_key_and_unknown_record_never_leak_the_key(aps):
    aps.records["ML18019A922"] = record()
    bad = kg_adams.AdamsClient("wrong-key-value-xyz", aps.base)
    r = bad.get_document("ML18019A922")
    assert not r.ok and r.status == 401 and "NRC_ADAMS_API_KEY" in r.error and "wrong-key-value-xyz" not in r.error
    assert kg_hosts.HOSTS.cooldown(aps.host) is None                             # a bad key is not a block
    r = kg_adams.AdamsClient(KEY, aps.base).get_document("ML18019A999")
    assert not r.ok and r.status == 404


def test_429_starts_cooldown_and_is_counted(aps):
    aps.records["ML18019A922"] = record()
    aps.status, aps.headers = 429, {"Retry-After": "3600"}
    c = kg_adams.AdamsClient(KEY, aps.base)
    assert not c.get_document("ML18019A922").ok
    r = c.get_document("ML18019A922")
    assert r.deferred and c.requests == 1 and len(aps.hits) == 1
    assert kg_hosts.HOSTS.cooldown(aps.host)["until"] > 0


def test_default_pacing_is_one_request_every_two_seconds():
    assert kg_hosts.HostPolicy(persist=False).interval(kg_adams.ADAMS_HOST) == 2.0
    assert kg_adams.AdamsClient(KEY).host == kg_adams.ADAMS_HOST
    assert kg_adams.AdamsClient.from_env() is None


def adams_env(tmp_path, srv, aps, monkeypatch, name="ML99990A001"):
    kg_ingest.set_pdf_engine("basic")
    monkeypatch.setenv("NRC_ADAMS_API_KEY", KEY)
    monkeypatch.setenv("NRC_ADAMS_BASE", aps.base)
    monkeypatch.setattr(kg_adams, "_DOCS_HOSTS", (srv.host,))
    url = f"{srv.base}/docs/{name}.pdf"
    s = make_env(tmp_path, [{"id": 5, "document": "Letter", "links": [{"text": "pdf", "url": url}], "update_tracking_urls": []}])
    return s, url


def test_adams_change_detection_skips_unchanged_documents(tmp_path, monkeypatch, aps, caplog):
    srv = Server()
    try:
        srv.add("/docs/ML99990A001.pdf", make_pdf("Letter", "Version one text."))
        aps.records["ML99990A001"] = record("ML99990A001")
        s, url = adams_env(tmp_path, srv, aps, monkeypatch)
        with caplog.at_level(logging.DEBUG):
            st = kg_agent.run_once(s)
            assert st["summary"]["docs_created"] == 1 and srv.count() == 1 and st["summary"]["adams_requests"] == 1
            st = kg_agent.run_once(s)                                   # same record: the document host is not contacted
            assert st["summary"]["docs_unchanged"] == 1 and srv.count() == 1 and st["summary"]["adams_requests"] == 1
            assert "ADAMS record ML99990A001 unchanged" in st["doc_actions"][0]["detail"]
            aps.records["ML99990A001"] = record("ML99990A001", date="2026-01-02", content="Revised synthetic text.")
            srv.add("/docs/ML99990A001.pdf", make_pdf("Letter", "Version two text, now different."))
            st = kg_agent.run_once(s)                                   # the record changed: the PDF is fetched and rebuilt
            assert st["summary"]["docs_updated"] == 1 and srv.count() == 2
            assert doc_state(s, url)["adams_fp"].startswith("ML99990A001|")
        # the key is nowhere: logs, report, state, summary
        blob = caplog.text + st["report"] + json.dumps(st["summary"], default=str)
        for f in s.home.rglob("*"):
            if f.is_file() and f.suffix in (".json", ".md", ".jsonl", ".log"):
                blob += f.read_text(errors="ignore")
        assert KEY not in blob
    finally:
        srv.close()


def test_adams_failure_falls_back_to_normal_fetch(tmp_path, monkeypatch, aps):
    srv = Server()
    try:
        srv.add("/docs/ML99990A001.pdf", make_pdf("Letter", "Text."))
        s, url = adams_env(tmp_path, srv, aps, monkeypatch)             # the record is unknown to the API (404)
        st = kg_agent.run_once(s)
        assert st["summary"]["docs_created"] == 1 and st["summary"]["docs_error"] == 0
        st = kg_agent.run_once(s)
        assert st["summary"]["docs_unchanged"] == 1 and srv.count() == 2          # no fingerprint: ordinary conditional GET each run
    finally:
        srv.close()


def test_without_a_key_adams_is_off(tmp_path, monkeypatch, aps):
    srv = Server()
    try:
        srv.add("/docs/ML99990A001.pdf", make_pdf("Letter", "Text."))
        s, url = adams_env(tmp_path, srv, aps, monkeypatch)
        monkeypatch.delenv("NRC_ADAMS_API_KEY")
        st = kg_agent.run_once(s)
        assert st["summary"]["docs_created"] == 1 and st["summary"]["adams_requests"] == 0 and not aps.hits
    finally:
        srv.close()


def test_adamscheck_prints_redacted_summary(tmp_path, monkeypatch, aps):
    aps.records["ML18019A922"] = record(content="SECRET-BODY-TEXT " * 10)
    s = make_env(tmp_path, [])
    lines = []
    assert kg_agent.adamscheck(s, "ML18019A922", out=lines.append) == 2            # no key
    assert "NRC_ADAMS_API_KEY" in lines[0]
    monkeypatch.setenv("NRC_ADAMS_API_KEY", KEY)
    monkeypatch.setenv("NRC_ADAMS_BASE", aps.base)
    lines.clear()
    assert kg_agent.adamscheck(s, "ML18019A922", out=lines.append) == 0
    text = "\n".join(lines)
    assert "ML18019A922" in text and "DateAddedTimestamp" in text and "fingerprint" in text and "top-level" in text
    assert KEY not in text and "SECRET-BODY-TEXT" not in text
