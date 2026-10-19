"""End-to-end test of the KG update agent against a local mock site (three 'days')."""
import json
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
