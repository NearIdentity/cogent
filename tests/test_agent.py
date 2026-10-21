"""End-to-end test of the KG update agent against a local mock site (three 'days')."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))   # repo_root/src
sys.path.insert(0, str(Path(__file__).resolve().parent))                # repo_root/tests

import kg_agent  # noqa: E402
import kg_ingest  # noqa: E402
import kg_tools as T  # noqa: E402
from mock_site import Site  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    site = Site()
    b = site.base
    monkeypatch.setattr(T, "ECFR_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(T, "FR_HOSTS", ("127.0.0.1",))
    kg_ingest.set_pdf_engine("basic")
    standards = {"meta": {"count": 3}, "standards": [
        {"id": 1, "document": "10 CFR 50.69", "links": [{"text": "eCFR", "url": f"{b}/current/title-10/section-50.69"}],
         "update_tracking_urls": [f"{b}/api/versioner/v1/versions/title-10.json?part=50", f"{b}/agencies/nuclear-regulatory-commission"]},
        {"id": 2, "document": "NRC RG 1.233", "links": [{"text": "RG 1.233 Rev. 0", "url": f"{b}/docs/rg1233_rev0.pdf"}],
         "update_tracking_urls": [f"{b}/index/rg.html"]},
        {"id": 3, "document": "DOE-STD-1271-2025, Authorization Pathway", "links": [{"text": "PDF", "url": f"{b}/docs/doe1271_2025.pdf"}],
         "update_tracking_urls": [f"{b}/index/doe.html"]}]}
    reports = {"meta": {"count": 2}, "reports": [
        {"id": 1, "report": "Licensing basis report", "standards_to_comply_with_ids": [1, 2],
         "template_or_format_guide": "RG 1.233", "template_links": [{"text": "RG 1.233 Rev. 0", "url": f"{b}/docs/rg1233_rev0.pdf"}],
         "update_tracking_urls": []},
        {"id": 2, "report": "General technical report", "standards_to_comply_with_ids": [3],
         "template_links": [], "update_tracking_urls": []}]}
    sp, rp = tmp_path / "standards.json", tmp_path / "reports.json"
    sp.write_text(json.dumps(standards, indent=2))
    rp.write_text(json.dumps(reports, indent=2))
    s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0, assets=True)
    yield site, s, sp, rp
    site.close()


def test_three_days(env):
    site, s, sp, rp = env
    home = s.home

    # ---- day 1: bootstrap -------------------------------------------------
    st = kg_agent.run_once(s)
    sm = st["summary"]
    assert sm["docs_created"] == 3 and sm["docs_error"] == 0 and sm["errors"] == 0, st["report"]
    for key in ("standards/1", "standards/2", "standards/3", "reports/1", "reports/2"):
        assert (home / "staging" / key / "kg.json").exists(), key
    # eCFR section was read through the XML API and cross-references were added
    g = kg_agent.load_graph_file(next(home.glob("staging/documents/*/kg.json")))
    docs = {p.parent.name: kg_agent.load_graph_file(p) for p in home.glob("staging/documents/*/kg.json")}
    ecfr = next(x for x in docs.values() if "title-10.xml" in x.graph["fetch_url"])
    assert any(d.get("relation") == "references" for _, _, d in ecfr.edges(data=True))
    # report 1 complies with standards 1 and 2
    r1 = kg_agent.load_graph_file(home / "staging/reports/1/kg.json")
    assert {v for _, v, d in r1.edges(data=True) if d.get("relation") == "complies_with"} == {"entry:standard:1", "entry:standard:2"}
    # figures/tables exported next to the document KG
    assert list(home.glob("staging/documents/*/assets/*/manifest.json"))
    assert (home / "logs/report.md").read_text().count("## Run ") == 1

    # ---- day 2: nothing changed ------------------------------------------
    st = kg_agent.run_once(s)
    sm = st["summary"]
    assert sm["docs_unchanged"] == 3 and sm["docs_updated"] == 0 and sm["entries_rebuilt"] == 0, st["report"]
    assert sm["config_changes"] == 0
    assert (home / "logs/report.md").read_text().count("## Run ") == 2     # appended, not overwritten

    # ---- day 3: new RG revision, new DOE edition, eCFR amendment, new FR rule
    site.day = 3
    # the eCFR/FR trackers need a baseline first: they were recorded on days 1-2
    st = kg_agent.run_once(s)
    sm, rep = st["summary"], st["report"]
    cfg = json.loads(sp.read_text())["standards"]
    rg = cfg[1]
    assert rg["links"][0]["url"].endswith("rg1233_rev1.pdf") and rg["links"][0]["text"] == "RG 1.233 Rev. 1", rg
    doe = cfg[2]
    assert doe["document"].startswith("DOE-STD-1271-2026") and doe["links"][0]["url"].endswith("doe1271_2026.pdf")
    # shared link propagated to the report
    rpt = json.loads(rp.read_text())["reports"][0]
    assert rpt["template_links"][0]["url"].endswith("rg1233_rev1.pdf") and rpt["template_links"][0]["text"].endswith("Rev. 1")
    assert sm["config_changes"] >= 5
    assert sm["docs_created"] == 2          # the two new PDFs
    assert sm["docs_updated"] == 1          # the eCFR section text changed
    # audit trail + snapshots + archive
    log = [json.loads(l) for l in (home / "config/changes.jsonl").read_text().splitlines()]
    assert any(c["path"] == "document" and c["entry"] == "standard:3" for c in log)
    assert list((home / "config/history").glob("*_standards.json"))
    assert list(home.glob("archive/documents/*/*/kg.json")), "superseded document KG archived"
    assert list(home.glob("archive/entries/*/*/kg.json")), "superseded entry KG archived"
    # report content
    assert "Configuration changes" in rep and "version_bump" in rep and "new_rulemaking" in rep
    assert "modified sections" in rep
    # entry KGs follow the new documents
    meta = json.loads((home / "staging/standards/2/meta.json").read_text())
    assert any(d["url"].endswith("rg1233_rev1.pdf") for d in meta["documents"])
    assert (home / "logs/report.md").read_text().count("## Run ") == 3

    # ---- manual edit of the config between runs is logged -------------------
    c = json.loads(sp.read_text())
    c["standards"][0]["document"] = "10 CFR 50.69 (edited)"
    sp.write_text(json.dumps(c, indent=2))
    st = kg_agent.run_once(s)
    assert any(x["source"] == "external" and x["new"] == "10 CFR 50.69 (edited)" for x in st["config_changes"])


def test_identity_tools():
    ids = {i.label: i for i in T.parse_identities("NRC RG 1.233, DOE-STD-1271-2025, DOE O 420.1D, NEI 21-07 Rev. 1 and NRC RG 1.253",
                                                  "NE-STD-1027-2025 DOE-STD-1027-2018 ASCE/SEI 43-19 10 CFR 50.69")}
    assert ids["RG 1.233"].edition is None and ids["NEI 21-07"].edition == "1"
    assert ids["DOE-STD 1271"].edition == "2025" and ids["DOE-O 420.1"].edition == "D"
    assert ids["DOE-STD 1027"].edition == "2025" and ids["ASCE 43"].edition == "19" and ids["CFR 50.69"]
    i = ids["DOE-STD 1271"]
    assert T.retarget_text("DOE-STD-1271-2025, Authorization", i, "NE-STD-1271-2026") == "NE-STD-1271-2026, Authorization"
    assert T.FAMILIES["DOE-O"].rank("E") > T.FAMILIES["DOE-O"].rank("D")


def test_nqa_identity_and_paywall_inference():
    ids = {i.label: i.edition for i in T.parse_identities("ASME NQA-1-2026, NQA-1-2024 and NQA-1b-2011")}
    assert ids == {"NQA 1": "2026"}
    from kg_models import DocEntry
    raw = lambda **kw: {"id": 1, "document": "X", "links": [], **kw}
    assert DocEntry.from_raw("standard", raw(format_of_data="Paywalled PDF")).paywalled
    assert DocEntry.from_raw("standard", raw(links=[{"text": "t", "url": "https://store.asme.org/x"}])).paywalled
    assert not DocEntry.from_raw("standard", raw(links=[{"text": "t", "url": "https://www.nrc.gov/x.pdf"}])).paywalled
    assert DocEntry.from_raw("standard", raw(paywalled=False, format_of_data="paywalled")).paywalled is False   # explicit wins
    assert [i.label for i in T.entry_identities(DocEntry.from_raw("standard", raw(document="NRC RG 1.28 and 10 CFR 50")))] == ["RG 1.28", "CFR 50"]


def test_overlap_annotation_becomes_kg_edge():
    from kg_models import DocEntry
    a = DocEntry.from_raw("standard", {"id": 28, "document": "A", "links": [], "overlaps_with": [{"id": 1, "note": "x"}]})
    assert a.overlaps == [1]
    g = T.merge_graphs([], a, {1: "Standard one"})
    assert ("entry:standard:28", "entry:standard:1", "overlaps_with") in list(g.edges(keys=True))
    assert g.nodes["entry:standard:1"]["label"] == "Standard one"


def test_json_diff():
    a = {"standards": [{"id": 1, "links": [{"url": "a"}]}]}
    b = {"standards": [{"id": 1, "links": [{"url": "b"}]}, {"id": 2}]}
    d = T.json_diff(a, b)
    assert {"path": "standards[id=1].links[0].url", "old": "a", "new": "b"} in d
    assert any(x["path"] == "standards[id=2]" for x in d)


def test_dry_run_writes_nothing(env):
    site, s, sp, rp = env
    s2 = s.model_copy(update={"dry_run": True})
    before = sp.read_text()
    st = kg_agent.run_once(s2)
    assert not s.home.exists() or not list(s.home.rglob("*.json"))
    assert sp.read_text() == before and "DRY RUN" in st["report"]


def test_year_editions_are_normalised(env):
    """'ASCE/SEI 43-19' in the config and '43-2019' (or a 2020 printing) on a page are the same edition."""
    from kg_models import DocEntry, TrackerState
    site, s, sp, rp = env
    e = DocEntry.from_raw("standard", {"id": 20, "document": "ASCE/SEI 43-19, Seismic Design Criteria",
                                      "links": [{"text": "ASCE", "url": site.base + "/x"}],
                                      "update_tracking_urls": [site.base + "/index/asce.html"]})
    tr = T.PageIdentityTracker()
    ctx = T.TrackerContext(min_interval=0)
    res = tr.check(e.tracking_urls[0], None, [e], ctx)
    assert not [f for f in res.findings if f.type == "new_edition"], res.findings
    site.day = 3
    res = tr.check(e.tracking_urls[0], res.state, [e], ctx)
    f = [f for f in res.findings if f.type == "new_edition"]
    assert len(f) == 1 and f[0].new == "24" and f[0].new_text == "ASCE/SEI 43-24" and f[0].new_url.endswith("asce43_24.pdf")


def test_paywalled_warnings(tmp_path, monkeypatch):
    from mock_site import Site, make_pdf
    site = Site()
    try:
        b = site.base
        kg_ingest.set_pdf_engine("basic")
        std = {"meta": {}, "standards": [{
            "id": 20, "document": "ASCE/SEI 43-19, Seismic Design Criteria", "paywalled": True,
            "links": [{"text": "ASCE Store", "url": f"{b}/store/asce43.html"}],
            "update_tracking_urls": [f"{b}/index/asce.html"]}]}
        sp, rp = tmp_path / "s.json", tmp_path / "r.json"
        sp.write_text(json.dumps(std))
        rp.write_text(json.dumps({"meta": {}, "reports": []}))
        s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0)
        kinds = lambda st: sorted((a["kind"], a["severity"]) for a in st["alerts"])

        # (a) nothing local -> warning, and the folder to use is created
        st = kg_agent.run_once(s)
        assert ("no_local_copy", "warning") in kinds(st) and "NO LOCAL COPY" in st["report"]
        assert (s.home / "local/standards/20").is_dir()

        # purchased copy appears -> warning gone, copy is processed into the entry KG
        (s.home / "local/standards/20/asce43-19.pdf").write_bytes(make_pdf("ASCE/SEI 43-19 Seismic Design Criteria", "Purchased text."))
        st = kg_agent.run_once(s)
        assert not [a for a in st["alerts"] if a["severity"] == "warning"], st["alerts"]
        meta = json.loads((s.home / "staging/standards/20/meta.json").read_text())
        assert any(d["url"].startswith("file://") for d in meta["documents"])

        # (b) a new edition goes on sale
        site.day = 3
        st = kg_agent.run_once(s)
        new = [a for a in st["alerts"] if a["kind"] == "new_edition_for_purchase"]
        assert len(new) == 1 and new[0]["is_new"] and new[0]["details"]["new"] == "24", st["alerts"]
        assert "NEW EDITION FOR PURCHASE" in st["report"]
        # ... and the warning stays open (not new any more) until a copy of the new edition is placed
        st = kg_agent.run_once(s)
        new = [a for a in st["alerts"] if a["kind"] == "new_edition_for_purchase"]
        assert len(new) == 1 and not new[0]["is_new"] and new[0]["runs"] == 2
        (s.home / "local/standards/20/asce43-24.pdf").write_bytes(make_pdf("ASCE/SEI 43-24", "New edition text."))
        st = kg_agent.run_once(s)
        assert not [a for a in st["alerts"] if a["severity"] == "warning"], st["alerts"]
    finally:
        site.close()


def test_version_tracking_block(env):
    """Each configuration entry records the latest edition available and what is staged."""
    site, s, sp, rp = env
    s = s.model_copy(update={"auto_apply": False})          # keep the configuration as is: latest != staged
    kg_agent.run_once(s)
    cfg = json.loads(sp.read_text())["standards"]
    vt = cfg[1]["version_tracking"]                          # RG 1.233 Rev. 0
    assert vt["status"] == "current"
    assert vt["latest"][0]["label"] == "RG 1.233 Rev. 0"
    assert vt["staged"]["documents"][0]["url"].endswith("rg1233_rev0.pdf") and vt["staged"]["kg"] == "staging/standards/2/kg.json"
    assert vt["staged"]["editions"]["RG 1.233"] == {"edition": "0", "source": "configuration"}
    ecfr = cfg[0]["version_tracking"]                         # eCFR section: staged as of the API date
    assert ecfr["staged"]["documents"][0]["as_of"] == "2024-05-01"
    assert json.loads(rp.read_text())["reports"][1]["version_tracking"]["status"] == "no_documents"

    site.day = 3
    kg_agent.run_once(s)
    cfg = json.loads(sp.read_text())["standards"]
    vt = cfg[1]["version_tracking"]
    assert vt["status"] == "update_available"
    assert vt["latest"][0]["edition"] == "1" and vt["latest"][0]["url"].endswith("rg1233_rev1.pdf")
    assert vt["staged"]["editions"]["RG 1.233"]["edition"] == "0"          # still the old edition
    assert cfg[1]["links"][0]["url"].endswith("rg1233_rev0.pdf")           # configuration untouched
    ec = cfg[0]["version_tracking"]
    assert ec["status"] == "current" and ec["staged"]["documents"][0]["as_of"] == "2026-09-01"
    assert any(l["identity"].startswith("eCFR") and l["edition"] == "2026-09-01" for l in ec["latest"])
    log = [json.loads(l) for l in (s.home / "config/changes.jsonl").read_text().splitlines()]
    assert any(c["path"] == "version_tracking" and "update_available" in c["new"] for c in log)

    # accept the update: configuration follows, staged catches up in the same run
    kg_agent.run_once(s.model_copy(update={"auto_apply": True}))
    vt = json.loads(sp.read_text())["standards"][1]["version_tracking"]
    assert vt["status"] == "current" and vt["staged"]["editions"]["RG 1.233"]["edition"] == "1"
    assert vt["staged"]["documents"][0]["url"].endswith("rg1233_rev1.pdf")


def test_add_local_copy_of_paywalled_standard(tmp_path):
    from mock_site import make_pdf
    kg_ingest.set_pdf_engine("basic")
    sp, rp = tmp_path / "s.json", tmp_path / "r.json"
    sp.write_text(json.dumps({"standards": [{"id": 7, "document": "ASCE/SEI 43-19", "format_of_data": "paywalled PDF",
                                             "links": [], "update_tracking_urls": []}]}))
    rp.write_text(json.dumps({"reports": []}))
    s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0)
    old, new = tmp_path / "asce43-19.pdf", tmp_path / "asce43-24.pdf"
    old.write_bytes(make_pdf("ASCE/SEI 43-19 Seismic Design Criteria", "Edition 2019 text."))
    new.write_bytes(make_pdf("ASCE/SEI 43-24 Seismic Design Criteria", "Edition 2024 text."))

    res = kg_agent.add_local(s, "standard:7", [old], stage=True)
    assert res["added"] == ["asce43-19.pdf"] and not any(a["kind"] == "no_local_copy" for a in res["alerts"])
    assert list((s.home / "staging/standards/7").glob("kg.json"))
    assert kg_agent.add_local(s, "standard:7", [old])["skipped_identical"] == ["asce43-19.pdf"]
    with pytest.raises(ValueError):
        kg_agent.add_local(s, "standard:7", [tmp_path / "x.txt"])
    # a new edition replaces the old one (old kept aside, not staged)
    res = kg_agent.add_local(s, "standard:7", [new], replace=True, stage=True)
    folder = s.home / "local/standards/7"
    assert res["superseded"] == ["asce43-19.pdf"] and [p.name for p in folder.iterdir() if p.is_file()] == ["asce43-24.pdf"]
    assert list((folder / ".superseded").glob("*/asce43-19.pdf"))
    meta = json.loads((s.home / "staging/standards/7/meta.json").read_text())
    assert len(meta["documents"]) == 1
    with pytest.raises(ValueError):
        kg_agent.add_local(s, "standard:99", [new])


def test_combined_graph_and_alert_impact(env):
    import kg_combine
    site, s, sp, rp = env
    kg_agent.run_once(s)
    home = s.home
    d = home / "staging/combined"
    dep = json.loads((d / "dependents.json").read_text())
    # report 1 declares compliance with standards 1 and 2; report 2 with 3
    assert dep["report:1"]["depends_on"] == ["standard:1", "standard:2"]
    assert dep["standard:2"]["declared_dependents"] == ["report:1"]
    assert dep["standard:2"]["affects_reports"]["declared"] == ["report:1"]
    assert dep["standard:3"]["affects_reports"]["declared"] == ["report:2"]
    g = kg_agent.load_graph_file(d / "kg.json")
    rels = {(u, v, k) for u, v, k in g.edges(keys=True)}
    assert ("entry:report:1", "entry:standard:2", "complies_with") in rels
    assert all(a.get("basis") in ("config", "text") for *_, a in g.edges(data=True))
    assert all("text" not in a for _, a in g.nodes(data=True))          # no document text is merged
    # report 1 reuses the RG 1.233 PDF and cites 'RG 1.233' -> inferred 'cites' edge to standard 2
    assert ("entry:report:1", "entry:standard:2", "cites") in rels
    meta = json.loads((d / "meta.json").read_text())
    assert meta["run_id"] and not meta["entries_without_graph"]
    built = meta["built"]
    kg_agent.run_once(s)                                                  # unchanged inputs -> not rewritten
    assert json.loads((d / "meta.json").read_text())["built"] == built
    # curated vs inferred are distinguishable in the store/MCP layer
    import kg_store
    st = kg_store.Store(home, sp, rp)
    r = st.dependents("standard:2")
    assert [x["entry"] for x in r["declared_dependents"]] == ["report:1"] and r["combined"]["derived"]
    assert st.graph_summary("combined")["nodes"] == 5
    # an alert on a standard names the reports it affects
    rt = kg_agent.Runtime(s)
    rt.dependents = dep
    assert "report:1" in json.dumps(dep["standard:1"]["affects_reports"])


def test_alert_names_affected_reports(tmp_path, monkeypatch):
    site = Site()
    try:
        b = site.base
        kg_ingest.set_pdf_engine("basic")
        sp, rp = tmp_path / "s.json", tmp_path / "r.json"
        sp.write_text(json.dumps({"standards": [{"id": 5, "document": "ASCE/SEI 43-19", "format_of_data": "paywalled PDF",
                                                 "links": [{"text": "store", "url": f"{b}/store/asce43.html"}],
                                                 "update_tracking_urls": [f"{b}/index/asce.html"]}]}))
        rp.write_text(json.dumps({"reports": [{"id": 9, "report": "Seismic report", "standards_to_comply_with_ids": [5],
                                               "template_links": [], "update_tracking_urls": []}]}))
        s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0)
        kg_agent.run_once(s)
        site.day = 3
        st = kg_agent.run_once(s)
        msgs = [a["message"] for a in st["alerts"] if a["entry"] == "standard:5"]
        assert msgs and all("Reports affected: report:9 (declared)" in m for m in msgs), msgs
        assert "report:9" in (s.home / "logs/report.md").read_text()
    finally:
        site.close()


def test_forced_update(env):
    """--force: unconditional re-read of every tracker page and source, rebuilt KGs, logged and reported."""
    site, s, sp, rp = env
    home = s.home
    kg_agent.run_once(s)                                           # bootstrap
    st = kg_agent.run_once(s)                                      # normal run: everything unchanged (304 / same text)
    assert st["summary"]["docs_unchanged"] == 3 and not st["summary"]["forced"]
    assert not (home / "logs/forced.jsonl").exists()
    kg_file = next(home.glob("staging/standards/2/kg.json"))
    before = kg_file.read_text()
    n_archive = len(list(home.glob("archive/**/kg.json")))
    site.hits.clear()

    f = kg_agent.Settings(**{**s.model_dump(), "force": True})
    st = kg_agent.run_once(f)
    sm = st["summary"]
    assert sm["forced"] and sm["docs_refreshed"] == 3 and sm["docs_unchanged"] == 0 and sm["docs_updated"] == 0, st["report"]
    assert sm["entries_refreshed"] >= 3 and sm["errors"] == 0
    # every tracking page and document was fetched again (no conditional request was answered with 304)
    assert any(h.startswith("/index/rg.html") for h in site.hits) and any(h.endswith("rg1233_rev0.pdf") for h in site.hits)
    # nothing really changed -> no new archive copies, no new KG version
    assert len(list(home.glob("archive/**/kg.json"))) == n_archive
    assert json.loads(next(home.glob("staging/documents/*/state.json")).read_text())["versions"] == 1
    # audit trail
    recs = [json.loads(l) for l in (home / "logs/forced.jsonl").read_text().splitlines()]
    docs = [r for r in recs if r["kind"] == "document"]
    assert len(docs) == 3 and {r["result"] for r in docs} == {"refreshed"} and all(r["text_sha_before_after"][0] == r["text_sha_before_after"][1] for r in docs)
    assert any(r["kind"] == "tracker" for r in recs) and recs[-1]["kind"] == "run" and recs[-1]["summary"]["forced"]
    for state_file in home.glob("staging/documents/*/state.json"):
        d = json.loads(state_file.read_text())
        assert d["last_forced"] and d["forced_runs"] == 1 and d["last_forced_result"] == "refreshed"
    assert all(t["last_forced"] and t["forced_runs"] == 1 for t in json.loads((home / "state/trackers.json").read_text()).values())
    # report + runs.jsonl
    rep = (home / "logs/report.md").read_text()
    assert "(FORCED UPDATE)" in rep and "### Forced update: 3 source document(s)" in rep and "refreshed" in rep
    assert json.loads((home / "logs/runs.jsonl").read_text().splitlines()[-1])["summary"]["forced"] is True
    import kg_store
    meta = kg_store.Store(home, sp, rp).entry_metadata("standard:2")
    assert meta["documents"][0]["forced_rereads"] == 1 and meta["documents"][0]["last_forced_result"] == "refreshed"
    assert kg_store.Store(home, sp, rp).overview()["agent_last_forced_run"]["summary"]["docs_refreshed"] == 3

    # a forced run also picks up the latest edition when one is available (and reads it)
    site.day = 3
    st = kg_agent.run_once(f)
    cfg = json.loads(sp.read_text())["standards"]
    assert cfg[1]["links"][0]["url"].endswith("rg1233_rev1.pdf")
    new = [a for a in st["doc_actions"] if a["url"].endswith("rg1233_rev1.pdf")]
    assert new and new[0]["action"] == "created" and new[0]["forced"]
    assert any(a["action"] == "updated" and a["forced"] and "source text changed" in a["detail"] for a in st["doc_actions"])   # eCFR section
    assert json.loads(next(home.glob("staging/documents/*/state.json")).read_text())["forced_runs"] >= 1


def test_forced_dry_run_writes_nothing(env):
    site, s, sp, rp = env
    kg_agent.run_once(s)
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "force": True, "dry_run": True}))
    assert st["summary"]["docs_refreshed"] == 3 and "FORCED UPDATE" in st["report"]
    assert not (s.home / "logs/forced.jsonl").exists()
    assert all(json.loads(p.read_text()).get("forced_runs", 0) == 0 for p in s.home.glob("staging/documents/*/state.json"))


def test_force_flag_on_the_command_line(env, capsys):
    site, s, sp, rp = env
    base = ["--home", str(s.home), "--standards", str(sp), "--reports", str(rp), "--min-interval", "0"]
    kg_agent.main(["run", *base])
    kg_agent.main(["run", "--force", *base])
    out = capsys.readouterr().out
    assert "'forced': True" in out and "'docs_refreshed': 3" in out
    kg_agent.main(["run", *base])                                   # the flag is per run, not sticky
    assert "'forced': False" in capsys.readouterr().out


def test_prune_assets_keeps_listed_files_only(tmp_path):
    root = tmp_path / "assets"
    keep = root / ("a" * 64)
    (keep / "figures").mkdir(parents=True)
    (keep / "tables").mkdir()
    (keep / "figures" / "fig1.png").write_bytes(b"1" * 10)
    (keep / "figures" / "stale.png").write_bytes(b"2" * 20)
    (keep / "tables" / "t1.csv").write_text("a,b\n")
    old = root / ("b" * 64)
    (old / "figures").mkdir(parents=True)
    (old / "figures" / "x.png").write_bytes(b"3" * 5)
    (keep / "manifest.json").write_text(json.dumps({"assets": [{"file": "figures/fig1.png"}, {"file": "tables/t1.csv"}]}))
    out = kg_agent._prune_assets(root, keep)
    assert out["files"] == 2 and out["bytes"] == 25 and out["dirs"] == 1
    assert (keep / "figures" / "fig1.png").exists() and (keep / "tables" / "t1.csv").exists()
    assert not (keep / "figures" / "stale.png").exists() and not old.exists()
    (keep / "manifest.json").unlink()                                  # no trustworthy manifest -> purge nothing
    (keep / "figures" / "stale2.png").write_bytes(b"x")
    assert kg_agent._prune_assets(root, keep)["files"] == 0 and (keep / "figures" / "stale2.png").exists()


def test_forced_update_purges_stale_assets_and_archives_orphans(env):
    import tarfile
    site, s, sp, rp = env
    home = s.home
    kg_agent.run_once(s)
    doc3 = kg_agent.docid_of(next(x for x in json.loads(sp.read_text())["standards"] if x["id"] == 3)["links"][0]["url"])
    doc2 = kg_agent.docid_of(next(x for x in json.loads(sp.read_text())["standards"] if x["id"] == 2)["links"][0]["url"])
    # stale assets next to a live document, and an old fetch-URL folder
    a2 = next((home / "staging/documents" / doc2 / "assets").iterdir())
    (a2 / "figures").mkdir(exist_ok=True)
    (a2 / "figures" / "stale.png").write_bytes(b"x" * 30)
    older = home / "staging/documents" / doc2 / "assets" / ("e" * 64) / "figures"
    older.mkdir(parents=True)
    (older / "old.png").write_bytes(b"y" * 40)
    # the configuration drops standard 3 -> its document and entry KG become orphans
    cfg = json.loads(sp.read_text())
    cfg["standards"] = [x for x in cfg["standards"] if x["id"] != 3]
    sp.write_text(json.dumps(cfg, indent=2))
    rcfg = json.loads(rp.read_text())
    for r in rcfg["reports"]:
        r["standards_to_comply_with_ids"] = [i for i in r["standards_to_comply_with_ids"] if i != 3]
    rp.write_text(json.dumps(rcfg, indent=2))

    # normal run: detected and tracked, left in place; stale assets are not touched
    st = kg_agent.run_once(s)
    track = json.loads((home / "state/orphans.json").read_text())
    assert {k: v["status"] for k, v in track.items()} == {f"document:{doc3}": "orphaned", "entry:standard:3": "orphaned"}
    assert (home / "staging/documents" / doc3 / "kg.json").exists() and (a2 / "figures" / "stale.png").exists()
    assert "### Orphaned staging data (2" in st["report"] and "Still in staging" in st["report"] and st["summary"]["orphans"] == 2
    assert not (home / "archive/orphans").exists()

    # forced + --only: scope warning, nothing archived, nothing outside scope refreshed
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "force": True, "only": ["standard:2"]}))
    assert any("scope limited by --only" in w for w in st["forced_warnings"]) and any("orphan archiving skipped" in w for w in st["forced_warnings"])
    assert (home / "staging/documents" / doc3 / "kg.json").exists() and st["summary"]["docs_refreshed"] == 1
    assert "Forced-update warnings" in st["report"]
    assert st["summary"]["assets_purged_files"] == 2 and not (a2 / "figures" / "stale.png").exists()   # in-scope document is pruned
    (a2 / "figures" / "stale.png").write_bytes(b"x" * 30)                                               # put the stale files back
    older.mkdir(parents=True)
    (older / "old.png").write_bytes(b"y" * 40)

    # forced + --no-prune: neither purge nor archive
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "force": True, "prune": False}))
    assert (a2 / "figures" / "stale.png").exists() and (home / "staging/documents" / doc3).exists()
    assert any("--no-prune" in w for w in st["forced_warnings"])

    # forced: stale assets purged, orphans archived (compressed, verified) and removed from staging
    f = kg_agent.Settings(**{**s.model_dump(), "force": True})
    st = kg_agent.run_once(f)
    sm = st["summary"]
    assert sm["assets_purged_files"] == 2 and sm["assets_purged_bytes"] == 70 and sm["orphans_archived"] == 2, st["report"]
    assert not (a2 / "figures" / "stale.png").exists() and not older.parent.exists()
    assert not (home / "staging/documents" / doc3).exists() and not (home / "staging/standards/3").exists()
    track = json.loads((home / "state/orphans.json").read_text())
    rec = track[f"document:{doc3}"]
    assert rec["status"] == "archived" and rec["archive"].startswith("archive/orphans/document_") and rec["archive"].endswith(".tar.gz")
    import hashlib
    arc = home / rec["archive"]
    assert hashlib.sha256(arc.read_bytes()).hexdigest() == rec["archive_sha256"] and rec["archive_bytes"] == arc.stat().st_size
    with tarfile.open(arc) as tf:
        assert any(n.endswith("kg.json") for n in tf.getnames())
    recs = [json.loads(l) for l in (home / "logs/forced.jsonl").read_text().splitlines()]
    last = max(i for i, r in enumerate(recs) if r["kind"] == "run")
    prev = max(i for i, r in enumerate(recs[:last]) if r["kind"] == "run")
    assert {r["result"] for r in recs[prev + 1:last] if r["kind"] == "orphan"} == {"archived"}
    assert any(r["kind"] == "document" and r.get("assets_purged", {}).get("files") == 2 for r in recs)
    assert "Stale asset files purged: 2 file(s)" in st["report"] and "archive/orphans/" in st["report"]
    # restore from the archive brings the document back
    with tarfile.open(arc) as tf:
        tf.extractall(home / "staging/documents")
    assert (home / "staging/documents" / doc3 / "kg.json").exists()
    # the restored folder is an orphan again; archiving it a second time is tracked as a new archive of the same item
    kg_agent.run_once(f)
    assert not (home / "staging/documents" / doc3).exists()


def test_forced_failed_fetch_keeps_previous_kg_with_warning(env):
    site, s, sp, rp = env
    home = s.home
    kg_agent.run_once(s)
    kg_file = next(home.glob("staging/standards/2/kg.json"))
    stamp = lambda t: re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", "<time>", t)    # a forced rebuild refreshes the build time (second resolution)
    before = stamp(kg_file.read_text())
    orig = site.route
    site.route = lambda path, q: None if path.endswith("rg1233_rev0.pdf") else orig(path, q)      # the source now 404s
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "force": True}))
    assert stamp(kg_file.read_text()) == before and st["summary"]["docs_error"] == 1
    w = [x for x in st["forced_warnings"] if "rg1233_rev0.pdf" in x]
    assert w and "FAILED" in w[0] and "previous KG" in w[0] and "may be out of date" in w[0]
    assert "**WARNING** forced re-read FAILED" in st["report"]
    recs = [json.loads(l) for l in (home / "logs/forced.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "warning" for r in recs) and any(r["kind"] == "document" and r["result"] == "error" for r in recs)
    d = next(p for p in home.glob("staging/documents/*/state.json") if "rg1233_rev0" in p.read_text())
    assert json.loads(d.read_text())["last_forced_result"] == "error"


def test_orphans_not_archived_when_configuration_is_invalid(env):
    site, s, sp, rp = env
    kg_agent.run_once(s)
    cfg = json.loads(sp.read_text())
    cfg["standards"].append({"id": 9})                              # invalid entry (no "document")
    cfg["standards"] = [x for x in cfg["standards"] if x["id"] != 3]
    sp.write_text(json.dumps(cfg))
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "force": True}))
    assert any("validation errors" in w for w in st["forced_warnings"]) and not (s.home / "archive/orphans").exists()
    assert any(o["status"] == "orphaned" for o in json.loads((s.home / "state/orphans.json").read_text()).values())


def test_table_database_stage_runs_after_staging_and_never_fails_the_run(env, pg_server, pg_schema):
    """kg_agent.py run --tables: builds the table database as the last stage; a problem there is reported, not raised."""
    site, s, sp, rp = env
    on = kg_agent.Settings(**{**s.model_dump(), "tables": True, "tabledb_dsn": pg_server, "tabledb_schema": pg_schema, "embedder": "hash"})
    st = kg_agent.run_once(on)
    td = st["summary"]["tabledb"]
    assert td["errors"] == [] and td["sources"] == 0 and "no staged tables" in " ".join(td["warnings"])        # the mock site serves text-only PDFs
    assert "### Table database" in st["report"] and (s.home / "logs/tabledb.jsonl").exists()
    assert json.loads((s.home / "logs/runs.jsonl").read_text().splitlines()[-1])["summary"]["tabledb"]["sources"] == 0
    # unreachable database: the staging run still succeeds, the problem is an error in the report
    bad = kg_agent.Settings(**{**s.model_dump(), "tables": True, "tabledb_dsn": "postgresql://nobody@127.0.0.1:9/x", "embedder": "hash"})
    st = kg_agent.run_once(bad)
    assert st["summary"]["docs_unchanged"] == 3 and "table database stage failed" in st["summary"]["tabledb"]["error"] and st["summary"]["errors"] >= 1
    # no DSN configured at all
    st = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "tables": True}))
    assert "no DSN" in st["summary"]["tabledb"]["error"]
    # off by default / skipped in a dry run
    assert kg_agent.run_once(s)["summary"]["tabledb"] is None
    assert kg_agent.run_once(kg_agent.Settings(**{**on.model_dump(), "dry_run": True}))["summary"]["tabledb"] == {"not_run": "dry run"}


def test_full_pipeline_html_tables_to_database(tmp_path, pg_server, pg_schema):
    """Staging agent -> exported tables -> table database -> data dictionary, on a page served by the mock site."""
    import kg_tabledb
    site = Site()
    try:
        kg_ingest.set_pdf_engine("basic")
        std = {"standards": [{"id": 1, "document": "Seismic Design Criteria", "links": [{"text": "html", "url": f"{site.base}/docs/seismic_tables.html"}],
                              "update_tracking_urls": []}]}
        sp, rp = tmp_path / "s.json", tmp_path / "r.json"
        sp.write_text(json.dumps(std))
        rp.write_text(json.dumps({"reports": []}))
        s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0, tables=True, tabledb_dsn=pg_server,
                              tabledb_schema=pg_schema, embedder="hash")
        td = kg_agent.run_once(s)["summary"]["tabledb"]
        assert td["errors"] == [] and td["loaded"] == 2 and td["joins"] == 1
        dd = json.loads(next((s.home / "tabledb").glob("*/data_dictionary.json")).read_text())
        assert [t["caption"][:9] for t in dd["tables"]] == ["Table 3-1", "Table 4-1"]
        td2 = kg_agent.run_once(s)["summary"]["tabledb"]                                    # nothing changed anywhere: nothing reloaded
        assert td2["loaded"] == 0 and td2["unchanged"] == 2
        forced = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "force": True}))["summary"]["tabledb"]
        assert forced["loaded"] == 2                                                         # --force reaches the table stage too
        st = kg_tabledb.status(s.home, pg_server, pg_schema)
        assert st["tables"] == 2 and st["rows"] == 8 and st["joins"] == 1
    finally:
        site.close()


def test_report_renders_with_skipped_tables(tmp_path, monkeypatch):
    """Regression: a non-zero `skipped` count in the table stage summary crashed the report (it was mistaken for a skip reason)."""
    import kg_tabledb
    out = {"embedder": "hash", "sources": 3, "loaded": 1, "unchanged": 1, "skipped": 1, "dropped": 0, "joins": 0, "documents_with_joins": 0,
           "llm_joins": None, "errors": [], "warnings": []}
    monkeypatch.setattr(kg_tabledb, "run", lambda *a, **k: out)
    sp, rp = tmp_path / "s.json", tmp_path / "r.json"
    sp.write_text(json.dumps({"standards": []}))
    rp.write_text(json.dumps({"reports": []}))
    s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0, tables=True, tabledb_dsn="postgresql://x@127.0.0.1:9/x", embedder="hash")
    td = kg_agent.run_once(s)["summary"]["tabledb"]
    assert td["skipped"] == 1
    report = next((tmp_path / "home").rglob("report*.md")).read_text()
    assert "### Table database" in report and "1 skipped" in report
