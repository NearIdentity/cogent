"""Browser-download prompt after a refused download, and verification of local copies against their ADAMS record."""
import hashlib
import json
import os
import sys
import time
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

BODY = " ".join(f"alpha{i} beta{i * 7 % 13} gamma{i}" for i in range(120))          # ~360 words, synthetic
OTHER = " ".join(f"delta{i} epsilon{i * 5 % 11} zeta{i}" for i in range(120))


def adams_record(acc, content=BODY, **over):
    r = record(acc, content=content)
    r["document"].update({"DocumentTitle": "Synthetic letter", "EstimatedPageCount": 1, "DateAddedTimestamp": "2099-01-01T00:00:00Z", **over})
    return r


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    for v in ("NRC_ADAMS_API_KEY", "KG_USER_AGENT", "KG_BROWSER_FALLBACK"):
        monkeypatch.delenv(v, raising=False)
    kg_hosts.configure(tmp_path / "hosts.json", default_interval=0)
    kg_ingest.set_pdf_engine("basic")
    yield
    kg_hosts.configure(None, persist=False)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    srv, aps = Server(), Aps()
    monkeypatch.setenv("NRC_ADAMS_API_KEY", KEY)
    monkeypatch.setenv("NRC_ADAMS_BASE", aps.base)
    monkeypatch.setattr(kg_adams, "_DOCS_HOSTS", (srv.host,))
    yield srv, aps
    srv.close()
    aps.close()


def alerts_of(st, kind):
    return [a for a in st["alerts"] if a["kind"] == kind]


def entry(srv, acc, mark=None, manual=False):
    link = {"text": "pdf", "url": f"{srv.base}/docs/ML9999/{acc}.pdf"}
    if manual:
        link["manual"] = True
    return {"id": 5, "document": "Letter", "links": [link], "update_tracking_urls": []}


def local_dir(s):
    d = s.home / "local" / "standards" / "5"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------- the prompt

def test_prompt_shows_the_adams_form_and_stages_the_saved_file(tmp_path, world, monkeypatch, capsys):
    srv, aps = world
    srv.block_all = 403
    aps.records["ML99990A004"] = adams_record("ML99990A004")
    s = make_env(tmp_path, [entry(srv, "ML99990A004")])
    s.manual_prompt = "always"
    target = local_dir(s) / "ML99990A004.pdf"
    answers = iter(["y", ""])
    asked, opened = [], []
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": (asked.append(prompt), next(answers))[1])
    monkeypatch.setattr(kg_agent, "_OPEN", lambda url: (opened.append(url), target.write_bytes(make_pdf("Letter", BODY)))[0])
    st = kg_agent.run_once(s)
    out = capsys.readouterr().out
    url = f"{srv.base}/docs/ML9999/ML99990A004.pdf"
    assert "Synthetic letter" in out and "ML99990A004" in out and f"full URL:  {url}" in out and f"save as:   {target}" in out
    assert "2099-01-01" in out and "Matches the ADAMS record" in out
    assert opened == [url] and len(asked) == 2
    sm = st["summary"]
    assert sm["docs_created"] == 1 and sm["docs_deferred"] == 0 and sm["docs_error"] == 0, st["report"]
    assert not alerts_of(st, "local_copy_mismatch") and srv.count() == 1                 # exactly one refused request
    book = json.loads((s.home / "state" / "local_verification.json").read_text())       # hash and metadata are recorded
    rec = book[str(target.resolve())]
    assert rec["ok"] and rec["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest() and rec["pdf"]["pages"] == 1
    # the next run: the file is there, the link is satisfied, nothing is requested and nobody is asked
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": pytest.fail("no prompt expected"))
    st = kg_agent.run_once(s)
    assert srv.count() == 1 and st["summary"]["docs_deferred"] == 0 and st["summary"]["docs_error"] == 0


def test_declined_prompt_lists_the_download_in_the_report(tmp_path, world, monkeypatch):
    srv, aps = world
    srv.block_all = 403
    aps.records["ML99990A004"] = adams_record("ML99990A004")
    s = make_env(tmp_path, [entry(srv, "ML99990A004")])
    s.manual_prompt = "always"
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": "n")
    st = kg_agent.run_once(s)
    assert st["summary"]["docs_deferred"] == 1 and st["summary"]["docs_created"] == 0
    assert "### Could not be downloaded automatically" in st["report"]
    assert str(local_dir(s) / "ML99990A004.pdf") in st["report"] and f"{srv.base}/docs/ML9999/ML99990A004.pdf" in st["report"]


def test_skip_all_asks_only_once(tmp_path, world, monkeypatch):
    srv, aps = world
    srv.block_all = 403
    s = make_env(tmp_path, [entry(srv, "ML99990A004"), {**entry(srv, "ML99990A005"), "id": 6}])
    s.manual_prompt = "always"
    calls = []
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": (calls.append(prompt), "s")[1])
    kg_agent.run_once(s)
    assert len(calls) == 1


def test_no_prompt_without_a_terminal_or_when_never(tmp_path, world, monkeypatch):
    srv, aps = world
    srv.block_all = 403
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": pytest.fail("must not prompt"))
    s = make_env(tmp_path, [entry(srv, "ML99990A004")])                                  # "auto": pytest has no terminal
    st = kg_agent.run_once(s)
    assert st["summary"]["docs_deferred"] == 1 and "### Could not be downloaded automatically" in st["report"]
    (tmp_path / "b").mkdir()
    s2 = make_env(tmp_path / "b", [entry(srv, "ML99990A004")])
    s2.manual_prompt = "never"
    assert kg_agent.run_once(s2)["summary"]["docs_deferred"] == 1


# ---------------------------------------------------------------------------- verification of local copies

def run_with_file(tmp_path, world, content_in_adams, text_in_file, mode="warn", **over):
    srv, aps = world
    aps.records["ML99990A006"] = adams_record("ML99990A006", content=content_in_adams, **over)
    s = make_env(tmp_path, [{"id": 5, "document": "Letter", "links": [], "update_tracking_urls": []}])
    s.verify_local = mode
    (local_dir(s) / "ML99990A006.pdf").write_bytes(make_pdf("Letter", text_in_file))
    return s, kg_agent.run_once(s)


def test_matching_copy_is_clean(tmp_path, world):
    s, st = run_with_file(tmp_path, world, BODY, BODY)
    assert not alerts_of(st, "local_copy_mismatch") and st["summary"]["docs_created"] == 1


def test_different_text_is_a_warning_and_still_ingested_in_warn_mode(tmp_path, world):
    s, st = run_with_file(tmp_path, world, BODY, OTHER)
    a = alerts_of(st, "local_copy_mismatch")
    assert len(a) == 1 and a[0]["severity"] == "warning" and "occurs in the file" in " ".join(a[0]["details"]["failures"])
    assert "LOCAL COPY DOES NOT MATCH ADAMS" in st["report"] and st["summary"]["docs_created"] == 1


def test_strict_mode_does_not_ingest_a_mismatch(tmp_path, world):
    s, st = run_with_file(tmp_path, world, BODY, OTHER, mode="strict")
    assert alerts_of(st, "local_copy_mismatch") and st["summary"]["docs_created"] == 0
    assert any("does not match its ADAMS record" in a["detail"] for a in st["doc_actions"])


def test_verification_can_be_switched_off(tmp_path, world):
    s, st = run_with_file(tmp_path, world, BODY, OTHER, mode="off")
    assert not alerts_of(st, "local_copy_mismatch") and not (s.home / "state" / "local_verification.json").exists()


def test_file_changed_without_a_new_modification_time_is_flagged(tmp_path, world):
    s, st = run_with_file(tmp_path, world, BODY, BODY)
    f = local_dir(s) / "ML99990A006.pdf"
    mtime = int(f.stat().st_mtime)
    f.write_bytes(make_pdf("Letter", BODY + " injected"))
    os.utime(f, (mtime, mtime))                                                          # content replaced, timestamp restored
    st = kg_agent.run_once(s)
    a = alerts_of(st, "local_copy_mismatch")
    assert a and "modification time did not" in " ".join(a[0]["details"]["failures"])


def test_verify_checks_name_pages_and_dates(tmp_path):
    p = tmp_path / "ML99990A006.pdf"
    p.write_bytes(make_pdf("Letter", BODY))
    ok = kg_adams.AdamsResult(True, 200, "ML99990A006", "fp", {}, content=BODY, info={"EstimatedPageCount": 1, "DateAddedTimestamp": "2099-01-01T00:00:00Z"})
    assert kg_adams.verify_local_copy(p, ok)["ok"]
    wrong_name = kg_adams.AdamsResult(True, 200, "ML99990A007", "fp", {}, content=BODY, info=ok.info)
    assert "file name says ML99990A006" in " ".join(kg_adams.verify_local_copy(p, wrong_name)["failures"])
    pages = kg_adams.AdamsResult(True, 200, "ML99990A006", "fp", {}, content=BODY, info={**ok.info, "EstimatedPageCount": 40})
    assert "ADAMS estimates 40" in " ".join(kg_adams.verify_local_copy(p, pages)["failures"])
    old = kg_adams.AdamsResult(True, 200, "ML99990A006", "fp", {}, content=BODY, info={**ok.info, "DateAddedTimestamp": "2018-01-19T10:11:12Z"})
    assert "after the record was added" in " ".join(kg_adams.verify_local_copy(p, old)["failures"])
    empty = kg_adams.AdamsResult(True, 200, "ML99990A006", "fp", {}, content="", info=ok.info)
    res = kg_adams.verify_local_copy(p, empty)
    assert res["ok"] and any("not compared" in n for n in res["notes"])
    assert kg_adams.text_overlap(BODY, BODY + " more") == 1.0 and kg_adams.text_overlap(BODY, OTHER) < 0.1


def test_daemon_children_have_no_stdin():
    assert "stdin=subprocess.DEVNULL" in (Path(__file__).resolve().parents[1] / "src" / "kg_daemon.py").read_text()


# ---------------------------------------------------------------------------- the suggested file name

def _entry(fmt, *urls):
    return kg_agent.DocEntry.from_raw("standard", {"id": 1, "document": "D", "format_of_data": fmt, "links": [{"text": "t", "url": u} for u in urls]})


def test_suggested_name_gets_the_extension_the_ingest_needs():
    f = kg_agent.suggest_filename
    pdf = "https://id.energy.gov/Home/DownloadDocument/5633"
    assert f(_entry("PDF (download endpoint, inferred from a matching public copy)", pdf), pdf) == "5633.pdf"        # standard 14
    assert f(_entry("HTML (eCFR web); XML/JSON through the eCFR API", "https://www.ecfr.gov/current/title-10/part-50"),
             "https://www.ecfr.gov/current/title-10/part-50") == "part-50.html"
    assert f(_entry("whatever", "https://www.nrc.gov/docs/ML1801/ML18019A922.pdf"), "https://www.nrc.gov/docs/ML1801/ML18019A922.pdf") == "ML18019A922.pdf"   # URL suffix wins
    assert f(_entry("", pdf), pdf) == "5633.pdf"                                                                      # nothing stated: PDF
    # one segment per link when every segment starts with a format (standard 18)
    a, b = "https://www.nrc.gov/docs/ML1801/ML18019A922", "https://www.energy.gov/ehss/doe-technical-standards-program"
    e18 = _entry("PDF (NRC copy of 3009); HTML program page for 1104", a, b)
    assert f(e18, a) == "ML18019A922.pdf" and f(e18, b) == "doe-technical-standards-program.html"
    # semicolons inside parentheses do not split (standard 15), and a non-format segment falls back to the first format
    u2 = "https://www.standards.doe.gov/standards-documents/1000/1027-astd-2018/@@images/file"
    e15 = _entry("PDF (inferred; download endpoint and standards site)", pdf, u2)
    assert f(e15, pdf) == "5633.pdf" and f(e15, u2) == "file.pdf"
    assert Path(f(_entry("PDF", pdf), pdf)).suffix in kg_agent.LOCAL_SUFFIXES
    # standard 29: two PDFs and an index page; standard 25: a page, a PDF and a store page; standard 17: a PDF behind a redirect note
    p1, p2, idx = "https://www.nrc.gov/docs/ML0706/ML070630003.pdf", "https://www.nrc.gov/docs/ML1513/ML15134A484.pdf", "https://www.nrc.gov/reactors/regs-guides-comm"
    e29 = _entry("PDF (RG 1.206, NUREG-1537 Part 1); HTML index page", p1, p2, idx)
    assert f(e29, idx) == "regs-guides-comm.html" and f(e29, p1) == "ML070630003.pdf"
    page, ml, store = "https://www.nrc.gov/reactors/modernizing/guidance/non-lwr", "https://www.nrc.gov/docs/ML2231/ML22318A284.pdf", "https://www.asme.org/codes/pra-standard"
    e25 = _entry("HTML (NRC page); PDF (RG 1.247); the RA-S-1.4 standard itself is paywalled (ASME landing page, sold as PDF and print)", page, ml, store)
    assert f(e25, page) == "non-lwr.html" and f(e25, store) == "pra-standard.html"
    u17 = "https://www.standards.doe.gov/standards-documents/1200/1237-astd-2021"
    assert f(_entry("PDF on the DOE standards site; the link redirected to an HTML program page", u17), u17) == "1237-astd-2021.pdf"
    two = _entry("PDF (a); HTML (b)", "https://x.org/a/one", "https://x.org/a/two")                                   # nothing set aside: one part per link
    assert f(two, "https://x.org/a/one") == "one.pdf" and f(two, "https://x.org/a/two") == "two.html"


def test_prompt_suggests_a_name_the_ingest_accepts_and_the_saved_file_is_used(tmp_path, world, monkeypatch, capsys):
    srv, aps = world
    srv.block_all = 403
    url = f"{srv.base}/Home/DownloadDocument/5633"
    s = make_env(tmp_path, [{"id": 14, "document": "DOE-STD", "format_of_data": "PDF (download endpoint, inferred from a matching public copy)",
                             "links": [{"text": "pdf", "url": url}], "update_tracking_urls": []}])
    s.manual_prompt = "always"
    d = s.home / "local" / "standards" / "14"
    d.mkdir(parents=True)
    target = d / "5633.pdf"
    answers = iter(["y", ""])
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": next(answers))
    monkeypatch.setattr(kg_agent, "_OPEN", lambda u: target.write_bytes(make_pdf("Standard", BODY)))
    st = kg_agent.run_once(s)
    assert f"save as:   {target}" in capsys.readouterr().out
    assert st["summary"]["docs_created"] == 1 and st["summary"]["docs_deferred"] == 0, st["report"]
    monkeypatch.setattr(kg_agent, "_INPUT", lambda prompt="": pytest.fail("no prompt expected"))
    before = srv.count()
    st = kg_agent.run_once(s)                                                    # the saved file satisfies the link: nothing requested
    assert srv.count() == before and st["summary"]["docs_deferred"] == 0
    # a file saved without the extension is not a local copy (the ingest ignores it)
    target.rename(d / "5633")
    rt = kg_agent.Runtime(s)
    assert not kg_agent.local_files(rt, rt.entries[0]) if rt.entries else not [p for p in d.iterdir() if p.suffix in kg_agent.LOCAL_SUFFIXES]
