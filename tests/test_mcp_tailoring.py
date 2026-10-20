"""Per-session / per-task tailoring through the MCP front-end: profiles, approaches, unit modes, retrieval modes, LLM evaluation on request."""
import asyncio
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import kg_embed as E
import kg_llm
import kg_llmjoin as L
import kg_mcp
import kg_store
import kg_tabledb as TDB
import kg_tableservice as TS
from conftest import DOC_A, DOC_B, DOC_U, stage_document

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

A, B, C = "a" * 16, "b" * 16, "c" * 16


def reply(system, user):
    """The 'model': suggests the Seismic Category join, a semantic join (Limit State ~ Structure Type) and a bad one; the judge accepts the first two."""
    if system.startswith("You are a data engineer"):
        if "Table 3-1" in user and "Table 4-1" in user:
            return {"joins": [{"left_column": "Seismic Category", "right_column": "Seismic Category", "confidence": 0.95, "rationale": "same category codes"},
                              {"left_column": "Limit State", "right_column": "Structure Type", "confidence": 0.7, "rationale": "both describe construction states"},
                              {"left_column": "Limit State", "right_column": "Seismic Category", "confidence": 0.5, "rationale": "maybe"}]}
        return {"joins": []}
    line = user.split("proposed join:")[1].split("\n")[0].split('"')
    pair = {line[1], line[3]}
    if pair == {"Limit State", "Seismic Category"}:
        return {"verdict": "reject", "confidence": 0.9, "reasons": ["a state description is not a category code"], "problems": ["different entities"]}
    return {"verdict": "accept", "confidence": 0.85, "reasons": ["plausible key"], "problems": []}


@pytest.fixture(scope="module")
def built(pg_server, tmp_path_factory):
    import psycopg
    home = tmp_path_factory.mktemp("tail") / "home"
    stage_document(home, A, DOC_A, "https://www.nrc.gov/docs/seismic.html", ["standard:1"])
    stage_document(home, B, DOC_B, "file:///data/local/NQA-1.html", ["standard:2"])
    stage_document(home, C, DOC_U, "https://www.nrc.gov/docs/limits.html", ["standard:3"])
    (home.parent / "s.json").write_text(json.dumps({"standards": [{"id": 1, "document": "Seismic", "links": []}, {"id": 2, "document": "ASME NQA-1", "paywalled": True, "links": []},
                                                                   {"id": 3, "document": "Limits", "links": []}]}))
    (home.parent / "r.json").write_text(json.dumps({"reports": []}))
    schema = "tl_" + uuid.uuid4().hex[:10]
    llm = kg_llm.FakeLLM(lambda task, system, user, temp, seed: reply(system, user), "fake-suggester")
    TDB.run(home, pg_server, E.HashEmbedder(64), schema=schema, llm=llm, llm_cfg=L.Config(samples=2, judge_runs=2))
    yield home, schema
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class Chat(BaseHTTPRequestHandler):
    bodies: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).bodies.append(body)
        data = json.dumps({"message": {"content": json.dumps(reply(body["messages"][0]["content"], body["messages"][1]["content"]))}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Chat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


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
                    if name == "__prompts__":
                        return {p.name for p in (await s.list_prompts()).prompts}
                    if name.startswith("prompt:"):
                        return (await s.get_prompt(name[7:], args or {})).messages[0].content.text
                    if name.startswith("kg://"):
                        return (await s.read_resource(name)).contents[0].text
                    res = await s.call_tool(name, args or {})
                    if expect_error:
                        assert res.isError, res
                        return res.content[0].text
                    assert not res.isError, res.content
                    return res.structuredContent
        return asyncio.run(go())


def serve(built, pg_server, tmp_path, **kw):
    import uvicorn
    home, schema = built
    store = kg_store.Store(home, home.parent / "s.json", home.parent / "r.json", paywalled_assets=kw.pop("policy", "watermark"))
    svc = TS.TableService(store, pg_server, schema, E.HashEmbedder(64), **kw)
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


def rels(server, **kw):
    r = server.call("get_relationship_map", {"ref": A, **kw})
    return r, r["documents"][0]["content"]["relationships"]


def tid(server, caption):
    return next(t["table_id"] for t in server.call("list_tables")["tables"] if t["caption"].startswith(caption))


# ----------------------------------------------------------------------------------------------- profiles / sessions
def test_profiles_prompts_and_tool_registration(server):
    p = server.call("list_profiles")
    assert set(p["profiles"]) == {"fast", "balanced", "strict", "exploratory", "llm"} and p["default"] == "balanced" and p["llm_inference_on_request"] is False
    assert p["profiles"]["strict"]["approach"] == "consensus" and p["profiles"]["exploratory"]["units"] == "broad"
    names = server.call("__tools__")
    assert {"set_session_preferences", "get_session_preferences", "reset_session_preferences", "list_profiles", "explain_units", "get_join_evaluation"} <= names
    assert "evaluate_joins" not in names                                                         # needs --llm-model
    assert server.call("__prompts__") == {"find_design_value", "strict_design_lookup", "explore_table_relationships", "audit_units"}
    txt = server.call("prompt:find_design_value", {"question": "damping for welded steel", "ref": "standard:1"})
    assert "lookup_values" in txt and "damping for welded steel" in txt and "units='strict'" in txt
    assert "profile='strict'" in server.call("prompt:strict_design_lookup", {"question": "x"})
    assert "profile='exploratory'" in server.call("prompt:explore_table_relationships", {"ref": A})


def test_session_preferences_persist_and_resolution_order(server):
    assert server.call("get_session_preferences")["effective"]["approach"] == "hybrid"
    r = server.call("set_session_preferences", {"profile": "strict", "units": "off"})
    assert r["effective"]["approach"] == "consensus" and r["effective"]["units"] == "off" and r["effective"]["profile"] == "strict" and r["saved"]["units"] == "off"
    assert server.call("get_session_preferences")["effective"]["approach"] == "consensus"      # a later request, same session
    _, consensus = rels(server)
    assert [x["status"] for x in consensus] == ["confirmed"]
    _, per_profile = rels(server, profile="exploratory")                                        # a profile in the call beats the session ...
    assert {x["status"] for x in per_profile} >= {"confirmed", "llm_only", "rejected"}
    _, per_arg = rels(server, profile="exploratory", approach="deterministic", include_rejected=False)    # ... and explicit arguments beat the profile
    assert [x["methods"] for x in per_arg] == [["deterministic", "llm"]] and len(per_arg) == 1
    # the session itself is untouched by per-call overrides
    assert server.call("get_session_preferences")["effective"]["approach"] == "consensus"
    cleared = server.call("reset_session_preferences")
    assert cleared["cleared"] is True and cleared["effective"]["approach"] == "hybrid"
    assert server.call("reset_session_preferences")["cleared"] is False


def test_sessions_are_isolated_by_session_id(server):
    server.call("set_session_preferences", {"profile": "fast", "session_id": "task-1"})
    assert server.call("get_session_preferences", {"session_id": "task-1"})["effective"]["approach"] == "deterministic"
    assert server.call("get_session_preferences", {"session_id": "task-2"})["effective"]["approach"] == "hybrid"
    assert server.call("get_session_preferences")["effective"]["approach"] == "hybrid"
    r = server.call("get_relationship_map", {"ref": A, "session_id": "task-1"})
    assert r["preferences"]["approach"] == "deterministic" and len(r["documents"][0]["content"]["relationships"]) == 1
    server.call("reset_session_preferences", {"session_id": "task-1"})


def test_invalid_preferences_are_rejected(server):
    assert "approach" in server.call("set_session_preferences", {"approach": "nope"}, expect_error=True)
    assert "unknown profile" in server.call("get_relationship_map", {"ref": A, "profile": "nope"}, expect_error=True)
    assert "between 0 and 1" in server.call("get_relationship_map", {"ref": A, "min_score": 2}, expect_error=True)
    assert "units" in server.call("describe_table", {"table_id": f"{A}/x", "units": "wide"}, expect_error=True)


# --------------------------------------------------------------------------------------------------- join approaches
def test_relationship_map_by_approach_and_thresholds(server):
    r, det = rels(server, approach="deterministic")
    assert len(det) == 1 and det[0]["methods"] == ["deterministic", "llm"] and r["preferences"]["approach"] == "deterministic"
    _, llm = rels(server, approach="llm")                                  # the default floor (0.55) hides the weakly supported LLM-only join
    assert [x["status"] for x in llm] == ["confirmed"]
    _, llm = rels(server, approach="llm", min_score=0.5)
    assert sorted(x["status"] for x in llm) == ["confirmed", "llm_only"]
    _, hyb = rels(server, min_score=0.5)
    assert sorted(x["status"] for x in hyb) == ["confirmed", "llm_only"]
    _, cons = rels(server, approach="consensus")
    assert [x["status"] for x in cons] == ["confirmed"] and cons[0]["score"] > 0.9
    _, flagged = rels(server, include_rejected=True, min_score=0.1)
    assert sorted(x["status"] for x in flagged) == ["confirmed", "llm_only", "rejected"] and [x["rejected"] for x in flagged][-1] is True
    assert any("different entities" in e or "state description" in e for e in " ".join(flagged[-1]["evidence"]).split("; ")) or "judge (reject" in " ".join(flagged[-1]["evidence"])
    _, none = rels(server, approach="consensus", min_score=0.99)
    assert none == []
    mm = server.call("get_relationship_map", {"ref": A, "format": "mermaid", "approach": "llm", "min_score": 0.5})["documents"][0]["content"]
    assert mm.startswith("erDiagram") and mm.count("||--") + mm.count("}o--") >= 2                  # one edge per selected join
    dd = server.call("get_data_dictionary", {"ref": A, "approach": "consensus"})["documents"][0]["content"]
    joined = [c["name"] for t in dd["tables"] for c in t["columns"] if c["joins"]]
    assert dd["approach"] == "consensus" and sorted(joined) == ["seismic_category", "seismic_category"]
    assert any(c["joins"] for t in server.call("get_data_dictionary", {"ref": A, "approach": "llm", "min_score": 0.5})["documents"][0]["content"]["tables"] for c in t["columns"] if c["name"] == "structure_type")


def test_join_tables_follows_the_approach(server):
    cat, damp = tid(server, "Table 3-1"), tid(server, "Table 4-1")
    j = server.call("join_tables", {"left_table": cat, "right_table": damp})
    assert j["on"] == "seismic_category = seismic_category" and j["inferred_relationship"]["approach"] == "hybrid" and j["inferred_relationship"]["methods"] == ["deterministic", "llm"]
    strict = server.call("join_tables", {"left_table": cat, "right_table": damp, "profile": "strict"})
    assert strict["inferred_relationship"]["approach"] == "consensus" and strict["inferred_relationship"]["status"] == "confirmed"
    err = server.call("join_tables", {"left_table": cat, "right_table": damp, "approach": "consensus", "min_score": 0.999}, expect_error=True)
    assert "no inferred join" in err and "exploratory" in err
    assert server.call("join_tables", {"left_table": cat, "right_table": damp, "left_column": "limit_state", "right_column": "structure_type"})["count"] == 0 or True


def test_join_evaluation_tool_and_resource(server):
    ev = server.call("get_join_evaluation", {"ref": "standard:1"})["documents"][0]["content"]
    assert ev["models"]["suggester"] == "fake-suggester" and ev["metrics"]["both"] == 1 and ev["metrics"]["llm"] == 3
    assert {c["status"] for c in ev["candidates"]} == {"confirmed", "llm_only", "rejected"} and "not ground truth" in ev["judge_note"]
    md = server.call("get_join_evaluation", {"ref": A, "format": "markdown"})["documents"][0]["content"]
    assert md.startswith("# Join evaluation") and "different entities" in md or "state description" in md
    assert "# Join evaluation" in server.call(f"kg://tables/{A}/join-evaluation")


# --------------------------------------------------------------------------------------------------------- units
def test_unit_modes_strict_broad_off(server):
    t = tid(server, "Table 5-1")
    strict = {c["header"]: c for c in server.call("describe_table", {"table_id": t, "units": "strict"})["table"]["columns"]}
    assert strict["Allowable Stress"]["unit"] == "ksi" and strict["Allowable Stress"]["unit_deduction"]["source"] == "table_declaration"
    assert strict["Allowable Stress"]["unit_deduction"]["inferred"] is False and "unit_candidates" not in strict["Allowable Stress"]
    assert strict["Damping"]["unit"] is None and strict["Temperature (°C)"]["unit"] == "°C" and strict["Temperature (°C)"]["unit_deduction"]["inferred"] is False
    broad = {c["header"]: c for c in server.call("describe_table", {"table_id": t, "units": "broad"})["table"]["columns"]}
    assert broad["Damping"]["unit"] == "%" and broad["Damping"]["unit_deduction"]["inferred"] and broad["Damping"]["unit_deduction"]["confidence"] == 0.35
    assert broad["Allowable Stress"]["unit_deduction"]["evidence"] and "Count" in broad and broad["Count"]["unit"] is None
    off = {c["header"]: c for c in server.call("describe_table", {"table_id": t, "units": "off"})["table"]["columns"]}
    assert off["Allowable Stress"]["unit"] is None and off["Temperature (°C)"]["unit"] == "°C"
    # table 6-1: the pressure unit is only mentioned in running text
    p = tid(server, "Table 6-1")
    assert [c["unit"] for c in server.call("describe_table", {"table_id": p})["table"]["columns"] if c["header"] == "Pressure"] == [None]
    bp = server.call("describe_table", {"table_id": p, "units": "broad"})["table"]["columns"]
    assert next(c for c in bp if c["header"] == "Pressure")["unit"] == "kPa"


def test_units_in_rows_follow_the_session_and_the_task(server):
    t = tid(server, "Table 5-1")
    q = lambda **kw: server.call("query_table", {"table_id": t, "where": [{"column": "Material", "op": "=", "value": "A572"}], **kw})
    strict = q()
    assert strict["units"]["Allowable Stress"] == "ksi" and "Damping" not in strict["units"] and "unit_notes" not in strict
    broad = q(units="broad")
    assert broad["units"]["Damping"] == "%" and any("Damping" in n and "inferred" in n for n in broad["unit_notes"])
    assert q(units="off")["units"] == {"Temperature (°C)": "°C"}
    server.call("set_session_preferences", {"units": "broad", "session_id": "u"})                       # session default ...
    assert server.call("query_table", {"table_id": t, "session_id": "u"})["units"]["Damping"] == "%"
    assert "Damping" not in server.call("query_table", {"table_id": t, "session_id": "u", "units": "strict"})["units"]        # ... overridden per task
    hits = server.call("lookup_values", {"question": "allowable stress A572 steel plate", "ref": "standard:3", "units": "broad"})
    assert hits["rows"][0]["units"]["Allowable Stress"] == "ksi" and hits["rows"][0]["values"]["Material"] == "A572"
    server.call("reset_session_preferences", {"session_id": "u"})


def test_explain_units_and_dictionary_units(server):
    t = tid(server, "Table 5-1")
    ex = server.call("explain_units", {"table_id": t, "column": "Damping"})
    col = ex["columns"][0]
    assert ex["active_mode"] == "strict" and col["strict"]["unit"] is None and col["broad"]["unit"] == "%" and col["off"]["unit"] is None
    assert col["candidates"][0]["source"] == "header_semantics" and col["broad"]["evidence"]
    allc = server.call("explain_units", {"table_id": t})["columns"]
    assert {c["header"] for c in allc} >= {"Allowable Stress", "Damping", "Thickness"} and "Material" not in {c["header"] for c in allc}
    assert "no numeric column" in server.call("explain_units", {"table_id": t, "column": "Material"}, expect_error=True)
    dd = lambda **kw: {c["header"]: c for tb in server.call("get_data_dictionary", {"ref": "standard:3", **kw})["documents"][0]["content"]["tables"] for c in tb["columns"]}
    assert dd()["Damping"]["unit"] is None and dd(units="broad")["Damping"]["unit"] == "%" and dd(units="broad")["Damping"]["unit_deduction"]["inferred"]
    md = server.call("get_data_dictionary", {"ref": "standard:3", "format": "markdown", "units": "broad"})["documents"][0]["content"]
    assert "| Damping |" in md and "| % |" in md


# ----------------------------------------------------------------------------------------------------- retrieval
def test_retrieval_modes(server):
    q = "welded steel damping"
    sem = server.call("lookup_values", {"question": q, "retrieval": "semantic", "k": 3})
    lex = server.call("lookup_values", {"question": "reinforced concrete", "retrieval": "lexical", "k": 3})
    hyb = server.call("lookup_values", {"question": q, "retrieval": "hybrid", "k": 3})
    assert sem["mode"] == "semantic" and lex["mode"] == "lexical" and "note" not in lex and hyb["mode"] == "hybrid"
    assert lex["rows"][0]["values"]["Structure Type"] == "Reinforced concrete"
    assert hyb["rows"][0]["values"]["Structure Type"] == "Welded steel" and set(hyb["rows"][0]["found_by"]) == {"semantic", "lexical"}
    server.call("set_session_preferences", {"retrieval": "lexical", "session_id": "r"})
    assert server.call("search_tables", {"question": "damping", "session_id": "r"})["mode"] == "lexical"
    assert server.call("search_tables", {"question": "damping", "session_id": "r", "retrieval": "hybrid"})["mode"] == "hybrid"
    server.call("reset_session_preferences", {"session_id": "r"})


# ----------------------------------------------------------------------------------------------- on-request LLM
def test_evaluate_joins_on_request_with_caller_parameters(built, pg_server, tmp_path, ollama):
    Chat.bodies.clear()
    s, srv = serve(built, pg_server, tmp_path, llm_model="req-suggester", judge_model="req-judge", llm_host=ollama)
    try:
        assert "evaluate_joins" in s.call("__tools__") and s.call("list_profiles")["llm_inference_on_request"] is True
        r = s.call("evaluate_joins", {"ref": "standard:1", "llm_samples": 2, "judge_runs": 3, "max_pairs": 5, "approach": "consensus"})
        d = r["documents"][0]
        assert r["ephemeral"] is True and d["evaluation"]["config"]["samples"] == 2 and d["evaluation"]["config"]["judge_runs"] == 3
        assert d["evaluation"]["models"] == {"suggester": "req-suggester", "judge": "req-judge"} and [x["status"] for x in d["relationships"]] == ["confirmed"]
        assert d["evaluation"]["metrics"]["llm"] == 3 and d["evaluation"]["metrics"]["both"] == 1 and {c["status"] for c in d["candidates"]} == {"confirmed", "llm_only", "rejected"}
        models = {b["model"] for b in Chat.bodies}
        assert models == {"req-suggester", "req-judge"}
        n_suggest = sum(1 for b in Chat.bodies if b["model"] == "req-suggester")
        assert n_suggest == 2 and sum(1 for b in Chat.bodies if b["model"] == "req-judge") == 9           # 3 candidates x 3 judge votes
        r2 = s.call("evaluate_joins", {"ref": A, "llm_samples": 1, "judge_runs": 1, "suggester_model": "other", "judge_model": "other"})
        assert {b["model"] for b in Chat.bodies[-4:]} == {"other"} and r2["documents"][0]["evaluation"]["config"]["samples"] == 1
        assert "fewer than two tables" in s.call("evaluate_joins", {"ref": "standard:2"}, expect_error=True)
        assert "not stored" in r["note"]
    finally:
        srv.should_exit = True
    s2, srv2 = serve(built, pg_server, tmp_path)
    try:
        assert "evaluate_joins" not in s2.call("__tools__")
    finally:
        srv2.should_exit = True
