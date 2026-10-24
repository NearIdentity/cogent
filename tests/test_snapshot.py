"""Snapshots: publish from the agent home, pull on a server, atomic switch, safety checks."""
import io
import json
import os
import sys
import tarfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_snapshot as SN  # noqa: E402
import kg_store  # noqa: E402


def make_home(tmp, text="v1"):
    home = tmp / "home"
    for rel, body in {"staging/standards/1/kg.json": '{"nodes": [], "edges": [], "directed": true, "multigraph": true, "graph": {}}',
                      "staging/standards/1/meta.json": json.dumps({"built": text, "documents": []}),
                      "staging/documents/abc/assets/h/tables/t1.csv": "a,b\n1,2\n", "staging/documents/abc/assets/h/manifest.json": "{}",
                      "staging/documents/abc/.kg.tmp.json": "half written", "tabledb/abc/data_dictionary.json": "{}", "state/alerts.json": "{}",
                      "logs/runs.jsonl": '{"finished": "x"}\n', "logs/report.md": "report", "local/standards/1/purchased.pdf": "SECRET", "archive/entries/x/old.json": "{}",
                      "mcp/tokens.json": '{"tokens": []}', "mcp/oauth.json": "{}"}.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return home


def names(tgz):
    with tarfile.open(tgz) as t:
        return sorted(t.getnames())


def test_publish_contains_what_servers_need_and_nothing_secret(tmp_path):
    home = make_home(tmp_path)
    (tmp_path / "s.json").write_text('{"standards": []}')
    r = SN.publish(home, tmp_path / "dest", tmp_path / "s.json", tmp_path / "missing.json")
    n = names(r["path"])
    assert "staging/standards/1/kg.json" in n and "tabledb/abc/data_dictionary.json" in n and "state/alerts.json" in n and "logs/runs.jsonl" in n
    assert "config/standards.json" in n and "config/reports.json" not in n and "SNAPSHOT.json" in n
    assert "staging/documents/abc/assets/h/tables/t1.csv" in n
    for banned in ("local/", "archive/", "mcp/", "logs/report.md", ".kg.tmp.json"):
        assert not any(banned in x for x in n), banned
    latest = json.loads((tmp_path / "dest" / "latest.json").read_text())
    assert latest["name"] == Path(r["path"]).name and latest["sha256"] == SN._sha256(Path(r["path"]))
    lean = SN.publish(home, tmp_path / "dest2", assets=False)
    assert not any("/assets/" in x for x in names(lean["path"]))


def test_pull_switches_atomically_and_keeps_serving_the_old_tree(tmp_path):
    home = make_home(tmp_path, "v1")
    dest, link = tmp_path / "dest", tmp_path / "srv" / "current"
    SN.publish(home, dest)
    r1 = SN.pull(str(dest), link)
    assert r1["updated"] and link.is_symlink() and (link / "staging/standards/1/meta.json").exists()
    assert SN.current(link)["name"] == r1["name"].removesuffix(".tar.gz")
    assert SN.pull(str(dest), link) == {"updated": False, "name": r1["name"]}                 # nothing new
    # a server reading through the link sees the new graph after the switch
    (home / "staging/standards/1/meta.json").write_text(json.dumps({"built": "v2", "documents": []}))
    import time
    time.sleep(1.1)                                                                         # a distinct bundle name (second resolution)
    SN.publish(home, dest)
    old_target = link.resolve()
    r2 = SN.pull(str(dest), link)
    assert r2["updated"] and r2["previous"] == r1["name"].removesuffix(".tar.gz") and link.resolve() != old_target
    assert json.loads((link / "staging/standards/1/meta.json").read_text())["built"] == "v2"
    assert old_target.exists()                                                              # kept (keep=3), a request in flight could still read it
    for i in range(4):                                                                      # pruning keeps the newest few
        time.sleep(1.1)
        SN.publish(home, dest)
        SN.pull(str(dest), link, keep=2)
    assert len([p for p in (tmp_path / "srv" / "snapshots").iterdir() if p.name.startswith(SN.PREFIX)]) == 2


def test_store_reads_through_the_link(tmp_path):
    home = make_home(tmp_path)
    (home / "staging/standards/1/kg.json").write_text(json.dumps({"nodes": [{"id": "n1", "type": "Paragraph", "label": "hello", "text": "hello"}], "edges": [],
                                                                  "directed": True, "multigraph": True, "graph": {}}))
    SN.publish(home, tmp_path / "dest")
    link = tmp_path / "srv" / "current"
    SN.pull(str(tmp_path / "dest"), link)
    st = kg_store.Store(link, standards=None)
    assert [n["id"] for n in st.search("standard:1", "hello")] == ["n1"]


def test_bad_checksum_and_unsafe_archives_are_refused(tmp_path):
    home = make_home(tmp_path)
    dest, link = tmp_path / "dest", tmp_path / "srv" / "current"
    r = SN.publish(home, dest)
    latest = json.loads((dest / "latest.json").read_text())
    (dest / "latest.json").write_text(json.dumps({**latest, "sha256": "0" * 64}))
    with pytest.raises(ValueError, match="checksum mismatch"):
        SN.pull(str(dest), link)
    assert not link.exists() and not list((tmp_path / "srv" / "snapshots").glob(".incoming*"))
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        t.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="unsafe path"):
        SN._safe_extract(evil, tmp_path / "out")
    link_tar = tmp_path / "link.tar.gz"
    with tarfile.open(link_tar, "w:gz") as t:
        info = tarfile.TarInfo("a")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(info)
    with pytest.raises(ValueError, match="not a plain file"):
        SN._safe_extract(link_tar, tmp_path / "out2")


def test_a_real_directory_is_never_replaced_by_the_link(tmp_path):
    home = make_home(tmp_path)
    SN.publish(home, tmp_path / "dest")
    (tmp_path / "srv" / "current").mkdir(parents=True)
    with pytest.raises(ValueError, match="not a symlink"):
        SN.pull(str(tmp_path / "dest"), tmp_path / "srv" / "current")


def test_pull_over_http(tmp_path):
    home = make_home(tmp_path)
    SN.publish(home, tmp_path / "www")
    handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(tmp_path / "www"), **k)       # noqa: E731
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = SN.pull(f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "srv" / "current")
        assert r["updated"] and (tmp_path / "srv" / "current" / "staging/standards/1/kg.json").exists()
    finally:
        srv.shutdown()


def test_cli_publish_pull_status(tmp_path, capsys):
    home = make_home(tmp_path)
    assert SN.main(["publish", "--home", str(home), "--dest", str(tmp_path / "d"), "--standards", str(tmp_path / "nope.json")]) == 0
    assert SN.main(["pull", "--src", str(tmp_path / "d"), "--link", str(tmp_path / "s" / "cur")]) == 0
    capsys.readouterr()
    assert SN.main(["status", "--link", str(tmp_path / "s" / "cur")]) == 0 and "cogent-snapshot-" in capsys.readouterr().out
    assert SN.main(["status", "--link", str(tmp_path / "s" / "none")]) == 1
    assert SN.main(["pull", "--src", str(tmp_path / "missing"), "--link", str(tmp_path / "s" / "cur")]) == 1


def test_agent_publishes_a_snapshot_after_a_run(tmp_path):
    import kg_agent
    from mock_site import Site
    site = Site()
    try:
        sp, rp = tmp_path / "s.json", tmp_path / "r.json"
        sp.write_text(json.dumps({"standards": [{"id": 2, "document": "NRC RG 1.233", "links": [{"text": "RG 1.233 Rev. 0", "url": f"{site.base}/docs/rg1233_rev0.pdf"}],
                                                 "update_tracking_urls": []}]}))
        rp.write_text(json.dumps({"reports": []}))
        s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0, publish_snapshot=str(tmp_path / "pub"))
        st = kg_agent.run_once(s)
        assert st["snapshot"]["files"] > 3 and (tmp_path / "pub" / "latest.json").exists()
        link = tmp_path / "srv" / "current"
        SN.pull(str(tmp_path / "pub"), link)
        store = kg_store.Store(link, sp, rp)
        assert store.overview()["entries"] == 1 and store.graph_summary("standard:2")["nodes"] > 0
        bad = kg_agent.run_once(kg_agent.Settings(**{**s.model_dump(), "publish_snapshot": str(tmp_path / "s.json" / "x")}))       # a file is in the way
        assert "error" in bad["snapshot"]
    finally:
        site.close()
