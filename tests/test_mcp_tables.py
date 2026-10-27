"""MCP query front-end of the table database + published data dictionaries / relationship maps."""
import asyncio
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

import kg_embed as E
import kg_mcp
import kg_store
import kg_tabledb as TDB
import kg_tableservice as TS
from conftest import DOC_A, DOC_B, stage_document

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

A, B = "a" * 16, "b" * 16


@pytest.fixture(scope="module")
def built(pg_server, tmp_path_factory):
    import psycopg
    home = tmp_path_factory.mktemp("tdb") / "home"
    stage_document(home, A, DOC_A, "https://www.nrc.gov/docs/seismic.html", ["standard:1"])
    stage_document(home, B, DOC_B, "file:///data/local/NQA-1.html", ["standard:2"])
    schema = "m_" + uuid.uuid4().hex[:10]
    TDB.run(home, pg_server, E.HashEmbedder(64), schema=schema)
    cfg = home.parent / "s.json"
    cfg.write_text(json.dumps({"standards": [{"id": 1, "document": "Seismic Design Criteria", "links": []}, {"id": 2, "document": "ASME NQA-1-2026", "paywalled": True, "links": []}]}))
    (home.parent / "r.json").write_text(json.dumps({"reports": []}))
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "alerts.json").write_text(json.dumps({"k": {"kind": "new_edition_for_purchase", "entry": "standard:2", "identity": "NQA 1", "severity": "warning",
                                                                  "message": "standard:2: a new edition is on sale", "details": {}}}))
    yield home, schema
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class Srv:
    def __init__(self, url, secret):
        self.url, self.secret = url, secret

    def call(self, name, args=None, expect_error=False):
        async def go():
            async with streamablehttp_client(self.url + "/mcp", headers={"Authorization": f"Bearer {self.secret}"}) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    if name == "__tools__":
                        return {t.name for t in (await s.list_tools()).tools}
                    if name.startswith("kg://"):
                        return (await s.read_resource(name)).contents[0].text
                    res = await s.call_tool(name, args or {})
                    if expect_error:
                        assert res.isError, res
                        return res.content[0].text
                    assert not res.isError, res.content
                    return res.structuredContent
        return asyncio.run(go())


def serve(built, pg_server, tmp_path, embedder="hash", policy="watermark", allow_sql=False, dim=64):
    import uvicorn
    home, schema = built
    store = kg_store.Store(home, home.parent / "s.json", home.parent / "r.json", paywalled_assets=policy)
    emb = None if embedder == "none" else E.HashEmbedder(dim)
    svc = TS.TableService(store, pg_server, schema, emb, allow_sql)
    tokens = kg_mcp.TokenStore(tmp_path / f"tok_{uuid.uuid4().hex[:6]}.json")
    secret = tokens.create("t")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    app = kg_mcp.build_app(store, tokens, port=port, tables=svc)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return Srv(f"http://127.0.0.1:{port}", secret), srv


@pytest.fixture()
def server(built, pg_server, tmp_path):
    s, srv = serve(built, pg_server, tmp_path)
    yield s
    srv.should_exit = True


def test_tools_registered_and_listing(server):
    names = server.call("__tools__")
    assert {"list_tables", "describe_table", "search_tables", "lookup_values", "query_table", "join_tables", "get_data_dictionary", "get_relationship_map"} <= names
    assert "run_sql" not in names                                                                       # off unless --allow-sql
    r = server.call("list_tables", {"ref": "standard:1"})
    assert r["count"] == 2 and all(t["docid"] == A and not t["paywalled"] for t in r["tables"]) and "paywalled" not in r
    assert "extracted automatically" in r["provenance"]["disclaimer"] and r["provenance"]["embedder"] == "hash/64"
    t = r["tables"][0]
    assert t["source"]["url"] == "https://www.nrc.gov/docs/seismic.html" and t["source"]["caption"].startswith("Table")
    assert server.call("list_tables", {"query": "audit"})["count"] == 1


def test_paywalled_tables_carry_warnings_and_copyright_notice(server):
    r = server.call("list_tables", {"ref": "standard:2"})
    assert r["paywalled"] and r["tables"][0]["paywalled"] and r["tables"][0]["source"]["url"] == "local:NQA-1.html"
    assert "Reproduced for reference only" in r["copyright_notice"]
    assert any("new edition is on sale" in w for w in r["warnings"]) and any("Server policy" in w for w in r["warnings"])
    mixed = server.call("list_tables")
    assert mixed["paywalled"] and {t["paywalled"] for t in mixed["tables"]} == {True, False}


def test_lookup_values_semantic(server):
    r = server.call("lookup_values", {"question": "damping for welded steel structures", "k": 3})
    assert r["mode"] == "semantic" and "note" not in r
    top = r["rows"][0]
    assert top["values"]["Structure Type"] == "Welded steel" and top["table"]["caption"].startswith("Table 4-1") and top["similarity"] > r["rows"][-1]["similarity"] - 1e-9
    assert top["table"]["source"]["page"] is None and top["table"]["paywalled"] is False and "paywalled" not in r
    q = server.call("lookup_values", {"question": "audit interval commercial grade items", "ref": "standard:2"})
    assert q["rows"][0]["values"]["Quality Level"] == "QL-3" and q["paywalled"] and q["warnings"]
    s = server.call("search_tables", {"question": "limit state annual probability seismic categories", "k": 2})
    assert s["tables"][0]["caption"].startswith("Table 3-1") and s["mode"] == "semantic"
    only = server.call("lookup_values", {"question": "damping", "table_ids": [s["tables"][0]["table_id"]], "k": 2})
    assert {h["table_id"] for h in only["rows"]} == {s["tables"][0]["table_id"]}


def test_lookup_of_an_identifier_no_table_mentions_is_no_confident_match(server):
    r = server.call("lookup_values", {"question": "allowable stress for ASTM A572 steel", "k": 3})
    assert r["no_confident_match"] is True and r["match_quality"] == "none" and r["rows"] == []
    assert "A572" in r["message"] and r["nearest_neighbours"] and r["missing_terms"] == ["A572"]
    ok = server.call("lookup_values", {"question": "damping for welded steel structures", "k": 3})
    assert ok["match_quality"] == "semantic" and ok["rows"] and "no_confident_match" not in ok


def test_query_describe_and_join(server):
    tabs = {t["caption"][:9]: t["table_id"] for t in server.call("list_tables")["tables"]}
    damp, cat = tabs["Table 4-1"], tabs["Table 3-1"]
    q = server.call("query_table", {"table_id": damp, "where": [{"column": "Seismic Category", "op": "=", "value": "SDC-4"}, {"column": "Damping (%)", "op": ">=", "value": 6}]})
    assert q["count"] == 1 and q["rows"][0]["values"]["Structure Type"] == "Reinforced concrete" and q["rows"][0]["typed"]["damping"] == 7
    assert q["table"]["caption"].startswith("Table 4-1") and "disclaimer" in q["provenance"]
    assert "unknown column" in server.call("query_table", {"table_id": damp, "where": [{"column": "nope", "op": "="}]}, expect_error=True)
    assert "unknown table" in server.call("describe_table", {"table_id": A + "/nope"}, expect_error=True)
    d = server.call("describe_table", {"table_id": damp, "sample_rows": 2})
    assert [c["name"] for c in d["table"]["columns"]] == ["seismic_category", "structure_type", "damping"] and len(d["table"]["sample_rows"]) == 2
    j = server.call("join_tables", {"left_table": cat, "right_table": damp})
    assert j["count"] == 5 and j["inferred_relationship"]["cardinality"] == "1:N" and j["on"] == "seismic_category = seismic_category"
    assert server.call("join_tables", {"left_table": damp, "right_table": cat})["count"] == 5                  # either order
    assert "no inferred join" in server.call("join_tables", {"left_table": cat, "right_table": f"{B}/{[t for t in tabs.values() if t.startswith(B)][0].split('/')[1]}"}, expect_error=True)


def test_data_dictionary_and_relationship_map(server):
    dd = server.call("get_data_dictionary", {"ref": "standard:1"})
    doc = dd["documents"][0]
    assert doc["document_id"] == A and len(doc["content"]["tables"]) == 2 and doc["content"]["relationships"] == 1 and not doc["paywalled"]
    md = server.call("get_data_dictionary", {"ref": A, "format": "markdown"})["documents"][0]["content"]
    assert md.startswith("# Data dictionary") and "## Relationship map" in md
    rm = server.call("get_relationship_map", {"ref": "standard:1"})["documents"][0]["content"]["relationships"]
    assert rm[0]["cardinality"] == "1:N" and rm[0]["score"] > 0.9 and any("refer to each other" in e for e in rm[0]["evidence"])
    assert server.call("get_relationship_map", {"ref": A, "format": "mermaid"})["documents"][0]["content"].startswith("erDiagram")
    pw = server.call("get_data_dictionary", {"ref": "standard:2"})["documents"][0]
    assert pw["paywalled"] and "Reproduced for reference only" in pw["copyright_notice"] and pw["warnings"]
    assert server.call("kg://tables/" + A + "/dictionary").startswith("# Data dictionary")
    assert server.call("kg://tables/" + A + "/relationships").startswith("erDiagram")
    assert "no staged entry" in server.call("get_data_dictionary", {"ref": "standard:9"}, expect_error=True)


def test_lexical_fallback_without_embedder_or_with_a_different_one(built, pg_server, tmp_path):
    for kw, why in (({"embedder": "none"}, "no embedder configured"), ({"dim": 32}, "vector spaces differ")):
        s, srv = serve(built, pg_server, tmp_path, **kw)
        try:
            r = s.call("lookup_values", {"question": "reinforced concrete"})
            assert r["mode"] == "lexical" and why in r["note"] and r["rows"][0]["values"]["Structure Type"] == "Reinforced concrete"
            assert s.call("search_tables", {"question": "damping"})["mode"] == "lexical"
        finally:
            srv.should_exit = True


def test_paywall_block_policy_hides_paywalled_tables(built, pg_server, tmp_path):
    s, srv = serve(built, pg_server, tmp_path, policy="block", allow_sql=True)
    try:
        r = s.call("list_tables")
        assert {t["docid"] for t in r["tables"]} == {A} and "paywalled" not in r
        assert s.call("list_tables", {"ref": "standard:2"})["count"] == 0
        assert "paywalled content" in s.call("describe_table", {"table_id": f"{B}/tbl0004"}, expect_error=True)
        assert "paywalled content" in s.call("query_table", {"table_id": f"{B}/tbl0004"}, expect_error=True)
        assert all(h["table_id"].startswith(A) for h in s.call("lookup_values", {"question": "audit interval quality level"})["rows"])
        assert "not to serve them" in s.call("get_data_dictionary", {"ref": "standard:2"}, expect_error=True)
        assert "unavailable while paywalled" in s.call("run_sql", {"query": "SELECT 1"}, expect_error=True)
    finally:
        srv.should_exit = True


def test_run_sql_is_opt_in_and_read_only(built, pg_server, tmp_path):
    s, srv = serve(built, pg_server, tmp_path, allow_sql=True)
    try:
        ok = s.call("run_sql", {"query": "SELECT docid, count(*) AS n FROM kgt_tables GROUP BY docid ORDER BY docid"})
        assert ok["columns"] == ["docid", "n"] and [r["n"] for r in ok["rows"]] == [2, 1] and ok["paywalled"]
        assert "only a single read-only SELECT" in s.call("run_sql", {"query": "DELETE FROM kgt_tables"}, expect_error=True)
        assert "SQL error" in s.call("run_sql", {"query": "SELECT * FROM nonexistent"}, expect_error=True)
    finally:
        srv.should_exit = True


def test_empty_or_unreachable_database_gives_clear_errors(built, tmp_path):
    home, schema = built
    store = kg_store.Store(home, home.parent / "s.json", home.parent / "r.json")
    with pytest.raises(kg_store.NotFound, match="not reachable"):
        TS.TableService(store, "postgresql://nobody@127.0.0.1:9/none", schema, E.HashEmbedder(8)).list_tables()


def test_overview_reports_table_database_status(server):
    td = server.call("get_overview")["table_database"]
    assert td["configured"] and td["reachable"] and td["built"] and td["can_select"] and td["tables"] >= 1
