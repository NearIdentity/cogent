"""ADAMS watch for manual NRC links, and the 'not checked for days' warning for hosts that keep refusing the agent."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_adams  # noqa: E402
import kg_agent  # noqa: E402
import kg_hosts  # noqa: E402
import kg_ingest  # noqa: E402
from mock_site import make_pdf  # noqa: E402
from test_adams import KEY, Aps, record  # noqa: E402
from test_fetch_blocking import Server, make_env  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    for v in ("NRC_ADAMS_API_KEY", "KG_USER_AGENT", "KG_BROWSER_FALLBACK"):
        monkeypatch.delenv(v, raising=False)
    kg_hosts.configure(tmp_path / "hosts.json", default_interval=0)
    yield
    kg_hosts.configure(None, persist=False)


def alerts_of(st, kind):
    return [a for a in st["alerts"] if a["kind"] == kind]


def test_manual_nrc_link_is_watched_through_adams(tmp_path, monkeypatch):
    srv, aps = Server(), Aps()
    try:
        kg_ingest.set_pdf_engine("basic")
        monkeypatch.setenv("NRC_ADAMS_API_KEY", KEY)
        monkeypatch.setenv("NRC_ADAMS_BASE", aps.base)
        monkeypatch.setattr(kg_adams, "_DOCS_HOSTS", (srv.host,))
        aps.records["ML99990A003"] = record("ML99990A003")
        url = f"{srv.base}/docs/ML9999/ML99990A003.pdf"
        s = make_env(tmp_path, [{"id": 8, "document": "Guide", "links": [{"text": "pdf", "url": url, "manual": True}], "update_tracking_urls": []}])

        st = kg_agent.run_once(s)                                           # baseline; no file yet
        assert srv.count() == 0 and not alerts_of(st, "manual_document_changed")
        need = alerts_of(st, "manual_download_needed")
        assert len(need) == 1
        save_to = Path(need[0]["details"]["save_to"])
        save_to.write_bytes(make_pdf("Guide", "Edition one."))              # the user downloads it
        st = kg_agent.run_once(s)
        assert st["summary"]["docs_created"] == 1 and not alerts_of(st, "manual_document_changed") and not alerts_of(st, "manual_download_needed")

        aps.records["ML99990A003"] = record("ML99990A003", date="2026-03-03", content="Edition two, revised.")
        st = kg_agent.run_once(s)                                           # the record changed in ADAMS
        ch = alerts_of(st, "manual_document_changed")
        assert len(ch) == 1 and ch[0]["severity"] == "warning" and ch[0]["details"]["accession"] == "ML99990A003"
        assert url in ch[0]["message"] and str(save_to) in ch[0]["message"]
        assert "MANUAL DOCUMENT CHANGED" in st["report"]
        st = kg_agent.run_once(s)                                           # still open until the file is replaced
        assert len(alerts_of(st, "manual_document_changed")) == 1

        save_to.write_bytes(make_pdf("Guide", "Edition two."))              # the user downloads the new file
        later = time.time() + 120
        os.utime(save_to, (later, later))
        st = kg_agent.run_once(s)
        assert not alerts_of(st, "manual_document_changed") and st["summary"]["docs_updated"] == 1
        st = kg_agent.run_once(s)
        assert not alerts_of(st, "manual_document_changed")
        assert srv.count() == 0                                             # the document host was never contacted
        assert KEY not in (s.home / "state" / "adams_manual.json").read_text()
    finally:
        srv.close()
        aps.close()


def test_manual_watch_is_off_without_a_key(tmp_path, monkeypatch):
    srv = Server()
    try:
        monkeypatch.setattr(kg_adams, "_DOCS_HOSTS", (srv.host,))
        s = make_env(tmp_path, [{"id": 8, "document": "Guide", "links": [{"text": "pdf", "url": f"{srv.base}/docs/ML9999/ML99990A003.pdf", "manual": True}],
                                 "update_tracking_urls": []}])
        st = kg_agent.run_once(s)
        assert st["summary"]["adams_requests"] == 0 and not alerts_of(st, "manual_document_changed")
        assert not (s.home / "state" / "adams_manual.json").exists()
    finally:
        srv.close()


def test_blocked_for_days_raises_a_warning(tmp_path):
    srv = Server()
    try:
        srv.block_all = 403
        s = make_env(tmp_path, [{"id": 3, "document": "D", "links": [{"text": "p", "url": srv.base + "/d.pdf"}], "update_tracking_urls": []}])
        st = kg_agent.run_once(s)
        assert st["summary"]["docs_deferred"] == 1 and not alerts_of(st, "deferred_too_long")      # blocked today: no warning yet
        hp = s.home / "state" / "hosts.json"
        book = json.loads(hp.read_text())
        old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        book[srv.host + "/"]["first_blocked"] = old
        hp.write_text(json.dumps(book))
        st = kg_agent.run_once(s)
        w = alerts_of(st, "deferred_too_long")
        assert len(w) == 1 and w[0]["severity"] == "warning" and w[0]["details"]["days"] >= 9.9
        assert "NOT CHECKED FOR DAYS" in st["report"] and srv.count() == 1                           # and still no new request
        s2 = make_env(tmp_path, [{"id": 3, "document": "D", "links": [{"text": "p", "url": srv.base + "/d.pdf"}], "update_tracking_urls": []}])
        s2.deferred_warn_days = 30
        assert not alerts_of(kg_agent.run_once(s2), "deferred_too_long")                             # the threshold is configurable
    finally:
        srv.close()
