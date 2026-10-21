"""Shared fixtures: a throw-away PostgreSQL (+ pgvector) server for the table-database tests, and a staged home built from
a real HTML document through the real KG pipeline."""
import glob
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _pg_bin():
    for d in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if (Path(d) / "initdb").exists():
            return Path(d)
    return None


@pytest.fixture(scope="session")
def pg_server():
    """DSN of a PostgreSQL server with the pgvector extension. $COGENT_TEST_PG_DSN wins; otherwise a private cluster is started
    from the installed PostgreSQL binaries (as the 'postgres' user when running as root). Skips when neither is possible."""
    pytest.importorskip("psycopg")
    import psycopg
    env = os.environ.get("COGENT_TEST_PG_DSN")
    if env:
        yield env
        return
    bind = _pg_bin()
    if bind is None:
        pytest.skip("no PostgreSQL available (set COGENT_TEST_PG_DSN or install postgresql + pgvector)")
    root = Path(tempfile.mkdtemp(prefix="cogent_pg_", dir="/tmp"))
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    as_root = os.geteuid() == 0
    if as_root:
        shutil.chown(root, user="postgres")

    def run(cmd):
        cmd = " ".join(shlex.quote(str(c)) for c in cmd)
        return subprocess.run(["su", "postgres", "-c", cmd] if as_root else ["sh", "-c", cmd], capture_output=True, text=True)

    data = root / "data"
    r = run([bind / "initdb", "-D", data, "-A", "trust", "-U", "postgres"])
    if r.returncode:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"initdb failed: {r.stderr[-200:]}")
    r = run([bind / "pg_ctl", "-D", data, "-o", f"-p {port} -k {root} -c listen_addresses=127.0.0.1", "-l", root / "log", "-w", "start"])
    if r.returncode:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"pg_ctl start failed: {r.stderr[-200:]}")
    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        run([bind / "pg_ctl", "-D", data, "-m", "fast", "stop"])
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"pgvector is not available: {exc}")
    yield dsn
    run([bind / "pg_ctl", "-D", data, "-m", "fast", "stop"])
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def pg_schema(pg_server):
    """A fresh schema name in the test database (dropped afterwards)."""
    import psycopg
    name = "t_" + uuid.uuid4().hex[:10]
    yield name
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


DOC_A = """<html><body>
<h1>Seismic Design Criteria for Nuclear Facilities</h1>
<h2>3 Seismic Categories</h2>
<p>Table 3-1 lists the seismic design categories and their limit state definitions.</p>
<table><caption>Table 3-1: Seismic design categories</caption>
<tr><th>Seismic Category</th><th>Limit State</th><th>Annual Probability (per year)</th></tr>
<tr><td>SDC-3</td><td>Limited permanent distortion</td><td>4.0E-4</td></tr>
<tr><td>SDC-4</td><td>Moderate permanent distortion</td><td>1.0E-4</td></tr>
<tr><td>SDC-5</td><td>Essentially elastic</td><td>1.0E-5</td></tr></table>
<p>Note: probabilities are annual frequencies of exceedance.</p>
<h2>4 Damping</h2>
<p>Damping values for the categories in Table 3-1 are given in Table 4-1.</p>
<table><caption>Table 4-1: Damping values by seismic category</caption>
<tr><th>Seismic Category</th><th>Structure Type</th><th>Damping (%)</th></tr>
<tr><td>SDC-3</td><td>Welded steel</td><td>4</td></tr>
<tr><td>SDC-3</td><td>Reinforced concrete</td><td>7</td></tr>
<tr><td>SDC-4</td><td>Welded steel</td><td>5</td></tr>
<tr><td>SDC-4</td><td>Reinforced concrete</td><td>7</td></tr>
<tr><td>SDC-5</td><td>Welded steel</td><td>5</td></tr></table>
</body></html>"""

DOC_B = """<html><body>
<h1>Quality Assurance Requirements</h1>
<h2>2 Quality levels</h2>
<table><caption>Table 2-1: Quality levels</caption>
<tr><th>Quality Level</th><th>Applies To</th><th>Audit Interval (months)</th></tr>
<tr><td>QL-1</td><td>Safety-related structures</td><td>12</td></tr>
<tr><td>QL-2</td><td>Important to safety components</td><td>24</td></tr>
<tr><td>QL-3</td><td>Commercial grade items</td><td>36</td></tr></table>
</body></html>"""


def stage_document(home: Path, docid: str, html: str, url: str, entries: list[str]) -> Path:
    """Run a real HTML document through the real KG pipeline into <home>/staging/documents/<docid>/ (kg.json, state.json, assets)."""
    import kg_ingest
    ddir = home / "staging" / "documents" / docid
    ddir.mkdir(parents=True, exist_ok=True)
    src = ddir.parent.parent.parent / "src_html" / f"{docid}.html"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(html, encoding="utf-8")
    g = kg_ingest.file_to_knowledge_graph(str(src), kg_ingest.make_extractor("rules", None, None), assets_dir=str(ddir / "assets"))
    kg_ingest.export_graph(g, str(ddir / "kg.json"))
    import json
    (ddir / "state.json").write_text(json.dumps({"url": url, "content_kind": "html", "last_checked": "2026-10-01T00:00:00Z"}))
    for key in entries:
        kind, n = key.split(":")
        e = home / "staging" / f"{kind}s" / n
        e.mkdir(parents=True, exist_ok=True)
        meta = json.loads((e / "meta.json").read_text()) if (e / "meta.json").exists() else {"documents": [], "name": key}
        meta["documents"].append({"url": url, "docid": docid})
        (e / "meta.json").write_text(json.dumps(meta))
    return ddir


@pytest.fixture()
def table_home(tmp_path):
    """A staged home: document A (public NRC page, 2 tables, entry standard:1) and B (purchased local copy, 1 table, standard:2)."""
    home = tmp_path / "home"
    stage_document(home, "a" * 16, DOC_A, "https://www.nrc.gov/docs/seismic.html", ["standard:1"])
    stage_document(home, "b" * 16, DOC_B, "file:///data/local/NQA-1.html", ["standard:2"])
    return home


DOC_U = """<html><body>
<h1>Structural Design Limits</h1>
<h2>5 Material limits</h2>
<p>All dimensions in this section are in mm unless otherwise noted. Stress limits are discussed in terms of ksi for the U.S. practice.</p>
<table><caption>Table 5-1: Plate limits (units: ksi)</caption>
<tr><th>Material</th><th>Allowable Stress</th><th>Thickness</th><th>Damping</th><th>Temperature (°C)</th><th>Count</th></tr>
<tr><td>A36</td><td>21.6</td><td>12</td><td>4</td><td>20</td><td>1</td></tr>
<tr><td>A572</td><td>30.0</td><td>16</td><td>5</td><td>40</td><td>2</td></tr>
<tr><td>A992</td><td>32.4</td><td>20</td><td>7</td><td>60</td><td>3</td></tr></table>
<h2>6 Other</h2>
<p>Pressure test values were 200 kPa and 300 kPa, and 250 kPa for the third series.</p>
<table><caption>Table 6-1: Test pressures</caption>
<tr><th>Test</th><th>Pressure</th><th>Weight 5 kg</th></tr>
<tr><td>T1</td><td>200</td><td>5 kg</td></tr><tr><td>T2</td><td>300</td><td>6 kg</td></tr><tr><td>T3</td><td>250</td><td>7 kg</td></tr></table>
</body></html>"""
