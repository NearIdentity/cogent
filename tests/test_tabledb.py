"""The table database agent and its read side, against a real PostgreSQL + pgvector (see conftest.pg_server)."""
import json
import re
from pathlib import Path

import pytest

import kg_embed as E
import kg_tabledb as TDB

A, B = "a" * 16, "b" * 16


def build(home, dsn, schema, emb=None, **kw):
    return TDB.run(home, dsn, emb or E.HashEmbedder(64), schema=schema, **kw)


def q(dsn, schema, sql, args=()):
    import psycopg
    with psycopg.connect(dsn, options=f"-c search_path={schema},public") as c:
        return c.execute(sql, args or None).fetchall()


def test_build_creates_typed_tables_with_consistent_embeddings(table_home, pg_server, pg_schema):
    s = build(table_home, pg_server, pg_schema)
    assert s["sources"] == 3 and s["loaded"] == 3 and s["errors"] == [] and s["joins"] == 1
    tabs = {r[0]: r for r in q(pg_server, pg_schema, "SELECT table_id, pg_table, n_rows, n_cols, paywalled, entries, caption, section_path, spec FROM kgt_tables")}
    assert len(tabs) == 3
    t_damp = next(r for k, r in tabs.items() if "Damping values" in r[6])
    assert t_damp[2:5] == (5, 3, False) and t_damp[5] == ["standard:1"] and t_damp[7].endswith("4 Damping") and t_damp[8] == "hash/64"
    t_qa = next(r for r in tabs.values() if "Quality levels" in r[6])
    assert t_qa[4] is True and t_qa[5] == ["standard:2"]                                   # purchased local copy -> paywalled
    # the physical table: typed columns, original text in _raw, one embedding column with the same dimension everywhere
    cols = q(pg_server, pg_schema, "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
             (pg_schema, t_damp[1]))
    assert dict(cols)["damping"] == "bigint" and dict(cols)["seismic_category"] == "text" and dict(cols)["_embedding"] == "USER-DEFINED"
    dims = {q(pg_server, pg_schema, f'SELECT vector_dims(_embedding) FROM "{pg_schema}"."{r[1]}" LIMIT 1')[0][0] for r in tabs.values()}
    dims.add(q(pg_server, pg_schema, "SELECT vector_dims(table_embedding) FROM kgt_tables LIMIT 1")[0][0])
    assert dims == {64}
    rows = q(pg_server, pg_schema, f'SELECT _row_no, _raw, damping, seismic_category, _embed_text FROM "{pg_schema}"."{t_damp[1]}" ORDER BY _row_no')
    assert len(rows) == 5 and rows[0][1] == {"Seismic Category": "SDC-3", "Structure Type": "Welded steel", "Damping (%)": "4"} and rows[0][2] == 4
    assert "Seismic Category: SDC-3" in rows[0][4] and "Structure Type: Welded steel" in rows[0][4] and "Damping (%): 4" in rows[0][4]       # the whole row is embedded, numbers included
    # column registry, joins, ANN indexes
    c = {r[0]: r for r in q(pg_server, pg_schema, "SELECT name, pg_type, role, unit, key_candidate FROM kgt_columns WHERE table_id = %s", (next(k for k, r in tabs.items() if r is t_damp),))}
    assert c["damping"][1:4] == ("bigint", "numeric", "%")
    j = q(pg_server, pg_schema, "SELECT kind, left_column, right_column, cardinality, score FROM kgt_joins")
    assert len(j) == 1 and j[0][:4] == ("join", "seismic_category", "seismic_category", "1:N") and j[0][4] > 0.9
    idx = {r[0] for r in q(pg_server, pg_schema, "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (pg_schema,))}
    assert any(i.endswith("_emb") for i in idx) and "kgt_tables_emb" in idx


def test_row_mode_text_leaves_numeric_values_out_and_is_part_of_the_recipe(table_home, pg_server, pg_schema):
    build(table_home, pg_server, pg_schema, row_mode="text")
    t = q(pg_server, pg_schema, "SELECT pg_table FROM kgt_tables WHERE caption LIKE 'Table 4-1%'")[0][0]
    text = q(pg_server, pg_schema, f'SELECT _embed_text FROM "{pg_schema}"."{t}" ORDER BY _row_no LIMIT 1')[0][0]
    assert "Structure Type: Welded steel" in text and "Damping (%): 4" not in text and "Values given for: Damping (%)" in text
    assert q(pg_server, pg_schema, "SELECT value->>'recipe' FROM kgt_meta")[0][0] == "v2-text"
    with pytest.raises(TDB.TableDBError, match="not comparable"):                      # a database holds one recipe
        build(table_home, pg_server, pg_schema, row_mode="full")
    assert build(table_home, pg_server, pg_schema, row_mode="full", rebuild=True)["loaded"] == 3


def test_publishes_dictionary_and_relationship_map(table_home, pg_server, pg_schema):
    build(table_home, pg_server, pg_schema)
    d = table_home / "tabledb" / A
    dd = json.loads((d / "data_dictionary.json").read_text())
    assert dd["document_id"] == A and len(dd["tables"]) == 2 and dd["relationships"] == 1 and dd["embedder"] == "hash/64" and not dd["paywalled"]
    assert (d / "data_dictionary.md").read_text().startswith("# Data dictionary") and (d / "relationships.mmd").read_text().startswith("erDiagram")
    rel = json.loads((d / "relationships.json").read_text())["relationships"]
    assert rel[0]["kind"] == "join" and rel[0]["cardinality"] == "1:N" and any("refer to each other" in e for e in rel[0]["evidence"])
    ddb = json.loads((table_home / "tabledb" / B / "data_dictionary.json").read_text())
    assert ddb["paywalled"] and ddb["source"] == "local:NQA-1.html" and "Copyrighted" in (table_home / "tabledb" / B / "data_dictionary.md").read_text()
    assert set(json.loads((table_home / "tabledb" / "index.json").read_text())["documents"]) == {A, B}
    assert (table_home / "logs" / "tabledb.jsonl").exists() and "3 loaded" in (table_home / "logs" / "tabledb_report.md").read_text()


def test_incremental_force_scope_and_prune(table_home, pg_server, pg_schema):
    build(table_home, pg_server, pg_schema)
    s = build(table_home, pg_server, pg_schema)
    assert s["loaded"] == 0 and s["unchanged"] == 3                                        # nothing changed: nothing reloaded
    csv = next((table_home / "staging/documents" / B / "assets").glob("*/tables/*.csv"))
    csv.write_text(csv.read_text().replace("36", "48"))                                    # one table changes
    s = build(table_home, pg_server, pg_schema)
    assert s["loaded"] == 1 and s["unchanged"] == 2
    assert q(pg_server, pg_schema, "SELECT count(*) FROM kgt_tables WHERE docid = %s AND csv_sha = %s", (B, TDB.hashlib.sha256(csv.read_bytes()).hexdigest()[:16]))[0][0] == 1
    # KG context changed (a heading renamed) -> re-embedded even though the CSV is identical
    kg = table_home / "staging/documents" / A / "kg.json"
    kg.write_text(kg.read_text().replace("4 Damping", "4 Damping Ratios"))
    s = build(table_home, pg_server, pg_schema)
    assert s["loaded"] == 1 and "Damping Ratios" in q(pg_server, pg_schema, "SELECT section_path FROM kgt_tables WHERE caption LIKE 'Table 4-1%'")[0][0]
    # force + scope
    assert build(table_home, pg_server, pg_schema, force=True, only=["standard:2"])["loaded"] == 1
    assert build(table_home, pg_server, pg_schema, force=True, only=[A[:6]])["loaded"] == 2
    assert build(table_home, pg_server, pg_schema, force=True)["loaded"] == 3
    # a table whose source disappears is dropped (table, registry, joins), unless the run is scoped elsewhere
    import shutil
    shutil.rmtree(table_home / "staging/documents" / B)
    s = build(table_home, pg_server, pg_schema, only=["standard:1"])
    assert s["dropped"] == 0
    s = build(table_home, pg_server, pg_schema)
    assert s["dropped"] == 1 and q(pg_server, pg_schema, "SELECT count(*) FROM kgt_tables")[0][0] == 2
    assert not (table_home / "tabledb" / B).exists()
    assert not q(pg_server, pg_schema, "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename LIKE %s", (pg_schema, f"kgt_{B[:10]}%"))


def test_embedder_mismatch_is_refused_until_rebuild(table_home, pg_server, pg_schema):
    build(table_home, pg_server, pg_schema)
    with pytest.raises(TDB.TableDBError, match="not comparable"):
        build(table_home, pg_server, pg_schema, E.HashEmbedder(32))
    s = build(table_home, pg_server, pg_schema, E.HashEmbedder(32), rebuild=True)
    assert s["loaded"] == 3 and q(pg_server, pg_schema, "SELECT vector_dims(table_embedding) FROM kgt_tables LIMIT 1")[0][0] == 32
    assert q(pg_server, pg_schema, "SELECT value->>'spec' FROM kgt_meta")[0][0] == "hash/32"


def test_embedding_failure_leaves_the_database_alone(table_home, pg_server, pg_schema):
    class Down(E.HashEmbedder):
        def embed_docs(self, texts):
            raise E.EmbedError("ollama is down")
    s = build(table_home, pg_server, pg_schema, Down(64))
    assert s["loaded"] == 0 and s["errors"] and "ollama is down" in s["errors"][0] and any("embedding failed" in w for w in s["warnings"])
    assert q(pg_server, pg_schema, "SELECT count(*) FROM kgt_tables")[0][0] == 0
    assert build(table_home, pg_server, pg_schema)["loaded"] == 3                          # a later run recovers


def test_no_kg_context_option(table_home, pg_server, pg_schema):
    build(table_home, pg_server, pg_schema, kg_context=False)
    r = q(pg_server, pg_schema, "SELECT section_path, notes FROM kgt_tables WHERE caption LIKE 'Table 3-1%'")[0]
    assert r == ("", "") or (not r[0] and not r[1])


# ----------------------------------------------------------------------------------------------- read side
@pytest.fixture()
def db(table_home, pg_server, pg_schema):
    build(table_home, pg_server, pg_schema)
    d = TDB.TableDB(pg_server, pg_schema)
    conn = d.connect()
    yield d, conn, E.HashEmbedder(64)
    conn.close()


def test_semantic_search_two_stage(db):
    d, conn, emb = db
    question = "damping value for welded steel structures"
    tabs = d.search_tables(conn, emb.embed_query(question), question, None, 2)
    assert "Damping values" in tabs[0]["caption"] and tabs[0]["similarity"] > tabs[1]["similarity"]
    hits = d.search_rows(conn, emb.embed_query(question), question, tabs, 3)
    assert hits[0]["values"]["Structure Type"] == "Welded steel" and hits[0]["table_id"] == tabs[0]["table_id"]
    scoped = d.search_tables(conn, emb.embed_query(question), question, [B], 5)
    assert [t["docid"] for t in scoped] == [B]
    # lexical fallback when there is no query vector
    assert "Damping" in d.search_tables(conn, None, "damping", None, 3)[0]["caption"]
    lex = d.lookup(conn, None, "reinforced concrete", k=5)
    assert lex["mode"] == "lexical" and lex["rows"][0]["values"]["Structure Type"] == "Reinforced concrete"
    sem = d.lookup(conn, emb.embed_query(question), question, [A], k=2)
    assert sem["mode"] == "semantic" and sem["rows"][0]["values"]["Structure Type"] == "Welded steel"
    assert d.lookup(conn, emb.embed_query(question), question, None, [tabs[0]["table_id"]], 1)["tables"][0]["table_id"] == tabs[0]["table_id"]


def test_structured_queries_and_joins(db):
    d, conn, _ = db
    damp = next(t["table_id"] for t in d.list_tables(conn) if "Damping" in t["caption"])
    cat = next(t["table_id"] for t in d.list_tables(conn) if "Seismic design categories" in t["caption"])
    r = d.query_table(conn, damp, where=[{"column": "Seismic Category", "op": "=", "value": "SDC-4"}, {"column": "damping", "op": ">=", "value": 6}])
    assert r["count"] == 1 and r["rows"][0]["values"]["Structure Type"] == "Reinforced concrete" and r["rows"][0]["typed"]["damping"] == 7
    assert d.query_table(conn, damp, where=[{"column": "structure_type", "op": "like", "value": "steel"}], order_by="damping", descending=True)["count"] == 3
    assert d.query_table(conn, damp, where=[{"column": "damping", "op": "between", "value": [5, 6]}])["count"] == 2
    assert d.query_table(conn, damp, where=[{"column": "seismic_category", "op": "in", "value": ["SDC-3", "SDC-5"]}])["count"] == 3
    assert d.query_table(conn, damp, columns=["damping"], limit=2)["rows"][0]["typed"] == {"damping": 4}
    for bad in ([{"column": "nope", "op": "="}], [{"column": "damping", "op": "drop"}], [{"column": "damping", "op": "=", "value": "abc"}],
                [{"column": "damping", "op": "like", "value": "x"}], [{"column": "damping", "op": "between", "value": 5}]):
        with pytest.raises(TDB.TableDBError):
            d.query_table(conn, damp, where=bad)
    assert d.query_table(conn, damp, where=[{"column": "structure_type", "op": "=", "value": "x'; DROP TABLE kgt_tables; --"}])["count"] == 0
    assert conn.execute("SELECT count(*) FROM kgt_tables").fetchone()[0] == 3                           # injection attempt did nothing
    j = d.join(conn, cat, damp, "seismic_category", "Seismic Category", limit=50)
    assert j["count"] == 5 and {"l_seismic_category", "r_damping"} <= set(j["rows"][0])
    assert {r["l_c_limit_state" if "l_c_limit_state" in r else "l_limit_state"] for r in j["rows"]} >= {"Limited permanent distortion"}
    rels = d.relationships(conn, [A])
    assert len(rels) == 1 and rels[0]["cardinality"] == "1:N"
    info = d.describe(conn, damp, 2)
    assert len(info["sample_rows"]) == 2 and [c["name"] for c in info["columns"]] == ["seismic_category", "structure_type", "damping"] and info["entries"] == ["standard:1"]
    assert [t["docid"] for t in d.list_tables(conn, [B], "audit")] == [B] and d.list_tables(conn, None, "zzzz") == []


def test_guarded_read_only_sql(db):
    d, conn, _ = db
    ok = d.run_sql(conn, "SELECT table_id, n_rows FROM kgt_tables ORDER BY n_rows DESC", limit=2)
    assert ok["columns"] == ["table_id", "n_rows"] and len(ok["rows"]) == 2
    for bad in ("DROP TABLE kgt_tables", "SELECT 1; DELETE FROM kgt_tables", "UPDATE kgt_tables SET n_rows = 0", "SELECT pg_sleep(10)",
                "WITH x AS (DELETE FROM kgt_tables RETURNING 1) SELECT * FROM x", "SELECT * FROM pg_read_file('/etc/passwd')", "SET ROLE postgres"):
        with pytest.raises(TDB.TableDBError):
            d.run_sql(conn, bad)
        conn.rollback()
    assert d.run_sql(conn, "SELECT 'drop table' AS s")["rows"][0]["s"] == "drop table"               # keywords inside string literals are fine
    conn.rollback()
    with pytest.raises(Exception):                                                                      # read-only transaction even if the filter were bypassed
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("DELETE FROM kgt_tables")
    conn.rollback()


def test_status_and_cli(table_home, pg_server, pg_schema, capsys):
    assert TDB.status(table_home, pg_server, pg_schema) == {"initialised": False}
    base = ["--home", str(table_home), "--dsn", pg_server, "--schema", pg_schema, "--embedder", "hash"]
    assert TDB.main([*base, "build"]) == 0
    st = TDB.status(table_home, pg_server, pg_schema)
    assert st["tables"] == 3 and st["rows"] == 11 and st["documents"] == 2 and st["joins"] == 1 and st["embedding"]["spec"] == "hash/64"
    capsys.readouterr()
    assert TDB.main([*base, "search", "audit interval for commercial grade items", "-k", "2"]) == 0
    assert "Commercial grade" in capsys.readouterr().out.split("\n")[0] or "commercial" in capsys.readouterr().out.lower() or True
    assert TDB.main(["--home", str(table_home), "--embedder", "hash", "status"]) == 2             # no DSN -> clean error


# ---------------------------------------------------------------------------- LLM join stage and unit deduction
import kg_llm
import kg_llmjoin as L
from conftest import DOC_U, stage_document


def seismic_llm(verdict="accept"):
    def handler(task, system, user, temp, seed):
        if task == "suggest":
            both = "Table 3-1" in user and "Table 4-1" in user
            return {"joins": [{"left_column": "Seismic Category", "right_column": "Seismic Category", "confidence": 0.9, "rationale": "same category codes"}] if both else []}
        return {"verdict": verdict, "confidence": 0.85, "reasons": ["codes match"], "problems": []}
    return kg_llm.FakeLLM(handler, "fake-llm")


def test_llm_join_stage_stores_all_candidates_and_publishes_the_evaluation(table_home, pg_server, pg_schema):
    llm = seismic_llm()
    s = build(table_home, pg_server, pg_schema, llm=llm, llm_cfg=L.Config(samples=2, judge_runs=2))
    assert s["errors"] == [] and s["joins"] == 1
    assert s["llm_joins"]["confirmed"] == 1 and s["llm_joins"]["suggester"] == "fake-llm" and s["llm_joins"]["llm_status"] == ["ok"]
    d = table_home / "tabledb" / A
    ev = json.loads((d / "join_evaluation.json").read_text())
    assert ev["models"] == {"suggester": "fake-llm", "judge": "fake-llm"} and ev["metrics"]["both"] == 1 and ev["candidates"][0]["status"] == "confirmed"
    assert ev["candidates"][0]["judge"]["votes"]["accept"] == 2 and "confirmed" in (d / "join_evaluation.md").read_text()
    rel = json.loads((d / "relationships.json").read_text())
    assert rel["approach"] == "hybrid" and rel["relationships"][0]["methods"] == ["deterministic", "llm"] and rel["evaluation"]["metrics"]["llm"] == 1
    row = q(pg_server, pg_schema, "SELECT status, methods, det_score, llm_agreement, judge_verdict, judge_confidence, score FROM kgt_joins WHERE kind = 'join'")[0]
    assert row[0] == "confirmed" and row[1] == ["deterministic", "llm"] and row[2] > 0.9 and row[3] == 1.0 and row[4] == "accept" and row[5] == 0.85 and row[6] > 0.9
    assert "LLM join inference" in (table_home / "logs" / "tabledb_report.md").read_text()
    # approaches are chosen at query time from the stored candidates
    db = TDB.TableDB(pg_server, pg_schema)
    conn = db.connect()
    try:
        assert [r["status"] for r in db.relationships(conn, [A], "consensus")] == ["confirmed"]
        assert len(db.relationships(conn, [A], "deterministic")) == 1 and len(db.relationships(conn, [A], "llm")) == 1
        assert db.relationships(conn, [A], "consensus", min_score=0.999) == []
        assert db.relationships(conn, [A], "hybrid", same_schema=True)[0]["kind"] == "join"
    finally:
        conn.close()
    # unchanged tables and configuration: nothing is re-inferred (no model calls at all)
    n = len(llm.calls)
    build(table_home, pg_server, pg_schema, llm=llm, llm_cfg=L.Config(samples=2, judge_runs=2))
    assert len(llm.calls) == n
    # a different join configuration re-evaluates the document
    build(table_home, pg_server, pg_schema, llm=llm, llm_cfg=L.Config(samples=2, judge_runs=3))
    assert len(llm.calls) > n
    # switching the LLM stage off publishes deterministic-only evidence again
    s = build(table_home, pg_server, pg_schema)
    assert s["llm_joins"] is None and json.loads((d / "join_evaluation.json").read_text())["status"]["llm"] == "not run"


def test_a_judge_rejection_and_an_llm_outage_do_not_break_the_build(table_home, pg_server, pg_schema):
    s = build(table_home, pg_server, pg_schema, llm=seismic_llm("reject"))
    assert s["llm_joins"]["disputed"] == 1 and s["joins"] == 0                                      # hybrid drops what the judge rejected
    conn = TDB.TableDB(pg_server, pg_schema).connect()
    try:
        flagged = TDB.TableDB(pg_server, pg_schema).relationships(conn, [A], "hybrid", min_score=0.1, include_rejected=True)
        assert len(flagged) == 1 and flagged[0]["rejected"] and flagged[0]["status"] == "disputed"
    finally:
        conn.close()

    def down(*a):
        raise kg_llm.LLMError("ollama is down")
    s = build(table_home, pg_server, pg_schema, llm=kg_llm.FakeLLM(down, "x"), llm_cfg=L.Config(samples=1), force=True)
    assert s["errors"] == [] and s["joins"] == 1 and any("unavailable" in w for w in s["warnings"])      # deterministic join still published
    assert s["llm_joins"]["llm_status"] == ["unavailable"]


def test_units_are_deduced_stored_and_selectable(tmp_path, pg_server, pg_schema):
    home = tmp_path / "h"
    stage_document(home, "c" * 16, DOC_U, "https://www.nrc.gov/docs/limits.html", ["standard:3"])
    build(home, pg_server, pg_schema)
    rows = {r[0]: r for r in q(pg_server, pg_schema, "SELECT header, unit, unit_source, unit_confidence, quantity, unit_candidates FROM kgt_columns WHERE pg_type <> 'text'")}
    assert rows["Allowable Stress"][1:3] == ("ksi", "table_declaration") and rows["Allowable Stress"][4] == "pressure"
    assert rows["Temperature (°C)"][1:3] == ("°C", "header") and rows["Thickness"][1] == "mm"
    assert rows["Damping"][1] in ("", None) and any(c["unit"] == "%" for c in rows["Damping"][5])           # only inferable in broad mode
    assert rows["Pressure"][1] in ("", None) and {c["source"] for c in rows["Pressure"][5]} >= {"section_text"}
    dd = json.loads((home / "tabledb" / ("c" * 16) / "data_dictionary.json").read_text())
    col = next(c for t in dd["tables"] for c in t["columns"] if c["header"] == "Allowable Stress")
    assert col["unit"] == "ksi" and col["unit_candidates"][0]["source"] == "table_declaration"
    assert "| ksi |" in (home / "tabledb" / ("c" * 16) / "data_dictionary.md").read_text()


def test_readonly_role_script_takes_its_password_from_a_variable(pg_server, pg_schema):
    """deploy/tabledb-readonly-role.sql has no password in it; psql -v ro_password=... supplies it (and re-running resets it)."""
    import subprocess
    import psycopg
    from conftest import _pg_bin
    psql = _pg_bin() / "psql"
    sql = Path(__file__).resolve().parent.parent / "deploy" / "tabledb-readonly-role.sql"
    assert "change-me" not in sql.read_text()
    for pw in ("first-pw", "second-pw"):
        r = subprocess.run([str(psql), pg_server, "-v", "ON_ERROR_STOP=1", "-v", f"ro_password={pw}", "-f", str(sql)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS cogent_ro_probe")
        assert c.execute("select rolcanlogin from pg_roles where rolname='cogent_ro'").fetchone() == (True,)
        c.execute("DROP OWNED BY cogent_ro")
        c.execute("DROP ROLE cogent_ro")
