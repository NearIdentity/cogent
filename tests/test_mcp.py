"""MCP server: store meta-data, bearer-token auth, and a real streamable-HTTP client round trip."""
import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_agent  # noqa: E402
import kg_ingest  # noqa: E402
import kg_mcp  # noqa: E402
import kg_store  # noqa: E402
import kg_tools as T  # noqa: E402
from mock_site import Site  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    """An agent home produced by a real agent run against the mock site (day 1 -> RG Rev 0)."""
    tmp = tmp_path_factory.mktemp("mcp")
    site = Site()
    b = site.base
    mp = pytest.MonkeyPatch()
    mp.setattr(T, "ECFR_HOSTS", ("127.0.0.1",))
    mp.setattr(T, "FR_HOSTS", ("127.0.0.1",))
    kg_ingest.set_pdf_engine("basic")
    std = {"standards": [
        {"id": 2, "document": "NRC RG 1.233", "links": [{"text": "RG 1.233 Rev. 0", "url": f"{b}/docs/rg1233_rev0.pdf"}],
         "update_tracking_urls": [f"{b}/index/rg.html"]},
        {"id": 5, "document": "ASCE/SEI 43-19 (paywalled)", "format_of_data": "paywalled PDF",
         "links": [{"text": "store", "url": f"{b}/store/asce43.html"}], "update_tracking_urls": [f"{b}/index/asce.html"]}]}
    rep = {"reports": [{"id": 1, "report": "Licensing report", "standards_to_comply_with_ids": [2],
                        "template_links": [], "update_tracking_urls": []}]}
    sp, rp = tmp / "standards.json", tmp / "reports.json"
    sp.write_text(json.dumps(std))
    rp.write_text(json.dumps(rep))
    s = kg_agent.Settings(home=tmp / "home", standards=sp, reports=rp, min_interval=0)
    kg_agent.run_once(s)
    site.day = 3                                    # a newer RG revision appears
    kg_agent.run_once(kg_agent.Settings(home=tmp / "home", standards=sp, reports=rp, min_interval=0, only=["standard:5"]))
    yield tmp / "home", sp, rp
    site.close()
    mp.undo()


def test_store_metadata(home):
    h, sp, rp = home
    st = kg_store.Store(h, sp, rp)
    cat = {r["entry"]: r for r in st.catalog()}
    assert set(cat) == {"standard:2", "standard:5", "report:1"}
    assert cat["standard:2"]["has_graph"] and cat["standard:2"]["nodes"] > 5
    m = st.entry_metadata("standard:2")
    assert m["graph"]["built"] and m["documents"][0]["last_checked"] and m["capture"]["age_days"] is not None
    assert m["version"]["summary"] and len(m["disclaimers"]) >= 3
    # the paywalled entry has no local copy -> warning + disclaimers
    w = st.entry_metadata("standard:5")
    assert w["paywalled"] and any(a["kind"] == "no_local_copy" for a in w["warnings"])
    assert any("Paywalled source" in d for d in w["disclaimers"])
    assert st.overview()["open_warnings"] >= 1
    # graph views
    hits = st.search("standard:2", "NEI 18-04", ["Paragraph"])
    assert hits and "NEI 18-04" in hits[0]["label"]
    nb = st.neighbors("standard:2", hits[0]["id"], "CONTAINS", "in")
    assert nb["total"] == 1
    sg = st.subgraph("standard:2", "entry:standard:2", depth=2)
    assert len(sg["nodes"]) > 3 and sg["edges"]
    assert st.outline("standard:2")[0]["title"] == "Regulatory Guide 1.233"
    with pytest.raises(kg_store.NotFound):
        st.graph("standard:99")
    with pytest.raises(kg_store.NotFound):
        st.graph("../../etc")


def test_token_store(tmp_path):
    ts = kg_mcp.TokenStore(tmp_path / "t.json")
    tok = ts.create("a")
    assert tok not in (tmp_path / "t.json").read_text()          # only the hash is stored
    assert (tmp_path / "t.json").stat().st_mode & 0o077 == 0
    assert ts.verify(tok) == "a" and ts.verify("nope") is None
    ts.revoke("a")
    assert ts.verify(tok) is None
    exp = kg_mcp.TokenStore(tmp_path / "e.json")
    t2 = exp.create("short", days=-1)
    assert exp.verify(t2) is None
    assert kg_mcp.TokenStore(tmp_path / "x.json", env_token="s3").verify("s3") == "env"


@pytest.fixture(scope="module")
def server(home):
    import uvicorn
    h, sp, rp = home
    tokens = kg_mcp.TokenStore(h / "mcp" / "tokens.json")
    secret = tokens.create("test")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    app = kg_mcp.build_app(kg_store.Store(h, sp, rp), tokens, port=port, public_hosts=["kg.example.com"])
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", secret
    srv.should_exit = True
    th.join(5)


def test_auth_gate(server):
    url, secret = server
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    assert httpx.get(url + "/healthz").status_code == 200                       # open for probes
    r = httpx.post(url + "/mcp", json=init, headers=hdr)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert httpx.post(url + "/mcp", json=init, headers={**hdr, "Authorization": "Bearer wrong"}).status_code == 401
    assert httpx.post(url + "/mcp", json=init, headers={**hdr, "Authorization": f"Bearer {secret}"}).status_code == 200
    assert httpx.post(url + "/mcp", json=init, headers={**hdr, "X-API-Key": secret}).status_code == 200
    # DNS-rebinding protection: an unknown Host header is refused even with a valid token
    bad = httpx.post(url + "/mcp", json=init, headers={**hdr, "Authorization": f"Bearer {secret}", "Host": "evil.test"})
    assert bad.status_code in (400, 421)


def test_client_round_trip(server):
    url, secret = server

    async def go():
        async with streamablehttp_client(url + "/mcp", headers={"Authorization": f"Bearer {secret}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"get_overview", "list_entries", "get_entry_metadata", "search_graph", "get_node",
                        "get_neighbors", "get_table", "export_graph", "get_warnings", "get_disclaimers"} <= names
                ov = (await s.call_tool("get_overview", {})).structuredContent
                assert ov["entries"] == 3
                res = (await s.call_tool("search_graph", {"ref": "standard:2", "query": "NEI 18-04"})).structuredContent
                prov = res["provenance"]
                assert res["matches"] and prov["entry"] == "standard:2" and prov["graph_built"] and prov["disclaimer"]
                assert "version_status" in prov and "warnings" in prov
                bad = await s.call_tool("get_node", {"ref": "standard:2", "node_id": "nope"})
                assert bad.isError
                res = (await s.call_tool("get_warnings", {"entry": "standard:5"})).structuredContent
                assert {"no_local_copy", "new_edition_for_purchase"} <= {w["kind"] for w in res["warnings"]}
                rs = {str(x.uri) for x in (await s.list_resources()).resources}
                assert "kg://catalog" in rs
                txt = (await s.read_resource("kg://standard/2/metadata")).contents[0].text
                assert json.loads(txt)["entry"] == "standard:2"

    asyncio.run(go())


def test_oauth_flow(home):
    """Dynamic registration -> consent with an operator token -> code+PKCE -> access/refresh tokens -> /mcp."""
    import base64
    import hashlib
    import threading
    from urllib.parse import parse_qs, urlparse

    import uvicorn
    h, sp, rp = home
    tokens = kg_mcp.TokenStore(h / "mcp" / "tokens2.json")
    operator = tokens.create("operator")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = kg_mcp.build_app(kg_store.Store(h, sp, rp), tokens, issuer_url=base, oauth_path=h / "mcp" / "oauth.json", port=port)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        c = httpx.Client(base_url=base, follow_redirects=False)
        # unauthenticated /mcp points at the resource metadata
        r = c.post("/mcp", json={})
        assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
        md = c.get("/.well-known/oauth-authorization-server").json()
        assert md["registration_endpoint"].endswith("/register") and "S256" in md["code_challenge_methods_supported"]
        assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
        reg = c.post("/register", json={"client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"]}).json()
        verifier = "v" * 64
        chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
             "code_challenge": chal, "code_challenge_method": "S256", "state": "xyz", "scope": "kg:read"}
        r = c.get("/authorize", params=q)
        assert r.status_code == 302 and "/consent?pid=" in r.headers["location"]
        pid = parse_qs(urlparse(r.headers["location"]).query)["pid"][0]
        assert "Authorize MCP client" in c.get("/consent", params={"pid": pid}).text
        bad = c.post("/consent", data={"pid": pid, "token": "wrong", "action": "allow"})
        assert bad.status_code == 200 and "not valid" in bad.text
        ok = c.post("/consent", data={"pid": pid, "token": operator, "action": "allow"})
        assert ok.status_code == 302
        loc = urlparse(ok.headers["location"])
        cb = parse_qs(loc.query)
        assert loc.netloc == "claude.ai" and cb["state"] == ["xyz"]
        form = {"grant_type": "authorization_code", "code": cb["code"][0], "redirect_uri": q["redirect_uri"],
                "client_id": reg["client_id"], "code_verifier": verifier}
        assert c.post("/token", data={**form, "code_verifier": "x" * 64}).status_code == 400      # wrong PKCE verifier
        tok = c.post("/token", data=form).json()
        assert tok["access_token"] and tok["refresh_token"]
        assert c.post("/token", data=form).status_code == 400                                       # code is single use
        assert "cga_" in tok["access_token"] and tok["access_token"] not in (h / "mcp/oauth.json").read_text()

        async def call(token):
            async with streamablehttp_client(base + "/mcp", headers={"Authorization": f"Bearer {token}"}) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    return (await s.call_tool("get_overview", {})).structuredContent["entries"]
        assert asyncio.run(call(tok["access_token"])) == 3
        assert asyncio.run(call(operator)) == 3                                                       # static token still works
        new = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                     "client_id": reg["client_id"]}).json()
        assert new["access_token"] != tok["access_token"]
        assert c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                      "client_id": reg["client_id"]}).status_code == 400            # refresh tokens rotate
        # revoking the operator token that approved the client kills its OAuth tokens
        tokens.revoke("operator")
        assert c.post("/mcp", json={}, headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 401
        # deny path
        pid2 = parse_qs(urlparse(c.get("/authorize", params=q).headers["location"]).query)["pid"][0]
        d = c.post("/consent", data={"pid": pid2, "action": "deny"})
        assert "access_denied" in d.headers["location"]
    finally:
        srv.should_exit = True


# ----------------------------------------------------------------------- figures and tables
@pytest.fixture(scope="module")
def assets_home(tmp_path_factory):
    """A hand-made agent home: entry 1 is public (NRC), entry 2 is paywalled (purchased local copy), both with a
    figure and a table, so the asset code is tested independently of the PDF parser."""
    import io

    from PIL import Image
    t = tmp_path_factory.mktemp("assets")
    home = t / "home"
    cfg = {"standards": [
        {"id": 1, "document": "NRC RG 1.233", "links": [{"text": "pdf", "url": "https://www.nrc.gov/docs/rg1233.pdf"}]},
        {"id": 2, "document": "ASME NQA-1-2026", "paywalled": True, "format_of_data": "paywalled PDF",
         "links": [{"text": "store", "url": "https://www.asme.org/nqa1"}]}]}
    (t / "s.json").write_text(json.dumps(cfg))
    (t / "r.json").write_text(json.dumps({"reports": []}))
    sources = {"aaaa1111": ("https://www.nrc.gov/docs/rg1233.pdf", 1), "bbbb2222": ("file:///data/local/NQA-1-2026.pdf", 2)}
    for docid, (url, n) in sources.items():
        d = home / "staging" / "documents" / docid
        a = d / "assets" / "h1"
        (a / "figures").mkdir(parents=True)
        (a / "tables").mkdir()
        (d / "state.json").write_text(json.dumps({"url": url, "content_kind": "pdf", "last_checked": "2026-10-01T00:00:00Z"}))
        Image.new("RGB", (640, 480), (30, 120, 200)).save(a / "figures" / "f0001_p2_flow.png")
        (a / "tables" / "t0002_p3_limits.csv").write_text("a,b\n1,2\n3,4\n")
        (a / "manifest.json").write_text(json.dumps({"source": url, "assets": [
            {"id": "f0001", "kind": "figure", "page": 2, "caption": "Flow", "file": "figures/f0001_p2_flow.png", "bytes": 1},
            {"id": "t0002", "kind": "table", "page": 3, "caption": "Limits", "file": "tables/t0002_p3_limits.csv", "rows": 3, "cols": 2}]}))
        e = home / "staging" / "standards" / str(n)
        e.mkdir(parents=True)
        (e / "meta.json").write_text(json.dumps({"built": "2026-10-01T00:00:00Z", "documents": [{"url": url, "docid": docid}]}))
    (home / "state").mkdir()
    (home / "state" / "alerts.json").write_text(json.dumps({"k": {
        "kind": "new_edition_for_purchase", "entry": "standard:2", "identity": "NQA 1", "severity": "warning",
        "message": "standard:2: a new edition is on sale", "details": {}}}))
    return home, t / "s.json", t / "r.json"


def _png(b):
    import io

    from PIL import Image
    return Image.open(io.BytesIO(b)).convert("RGB")


def test_public_figure_is_unmodified_and_paywalled_one_is_stamped(assets_home):
    home, sp, rp = assets_home
    st = kg_store.Store(home, sp, rp)
    pub = st.figure("standard:1", "aaaa1111/f0001")
    assert not pub["info"]["watermarked"] and pub["info"]["warnings"] == []
    assert _png(pub["png"]).size == (640, 480) and _png(pub["png"]).getpixel((5, 5)) == (30, 120, 200)
    pay = st.figure("standard:2", "bbbb2222/f0001")
    im = _png(pay["png"])
    assert pay["info"]["watermarked"] and im.height > 480 and im.width == 640          # banner appended
    body = im.crop((0, 0, 640, 480))
    assert len({body.getpixel((x, y)) for x in range(0, 640, 7) for y in range(0, 480, 7)}) > 1                 # tiled mark over the picture
    assert any("Reproduced for reference only" in w for w in pay["info"]["warnings"])
    assert any("new edition is on sale" in w for w in pay["info"]["warnings"])         # entry alerts travel along
    # downscaling works and still watermarks
    small = st.figure("standard:2", "bbbb2222/f0001", max_px=300)
    assert _png(small["png"]).width == 300 and small["info"]["watermarked"]


def test_svg_figures_are_rasterised_and_stamped(assets_home):
    home, sp, rp = assets_home
    a = home / "staging/documents/bbbb2222/assets/h1"
    (a / "figures" / "f0009.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="blue"/></svg>')
    m = json.loads((a / "manifest.json").read_text())
    m["assets"].append({"id": "f0009", "kind": "figure", "page": 1, "caption": "svg", "file": "figures/f0009.svg"})
    (a / "manifest.json").write_text(json.dumps(m))
    r = kg_store.Store(home, sp, rp).figure("standard:2", "bbbb2222/f0009")
    assert r["info"]["watermarked"] and _png(r["png"]).height > 100


def test_tables_lists_and_policies(assets_home):
    home, sp, rp = assets_home
    st = kg_store.Store(home, sp, rp)
    t = st.table_csv("standard:2", "bbbb2222/t0002")
    assert t["csv"].startswith("a,b") and t["paywalled"] and t["warnings"] and t["rows_total"] == 3
    assert st.table_csv("standard:1", "aaaa1111/t0002", max_rows=1)["truncated"]
    lst = st.list_assets("standard:2")
    assert lst["documents"][0]["paywalled"] and lst["documents"][0]["warnings"] and len(lst["documents"][0]["assets"]) >= 2
    assert [a["kind"] for a in st.list_assets("standard:1", "table")["documents"][0]["assets"]] == ["table"]
    # a document id works as ref too
    assert st.list_assets("aaaa1111")["documents"][0]["document_id"] == "aaaa1111"
    # validation: wrong entry, traversal, wrong kind
    for bad in (("standard:1", "bbbb2222/f0001"), ("standard:1", "aaaa1111/../../x"), ("standard:1", "aaaa1111/t0002")):
        with pytest.raises(kg_store.NotFound):
            st.figure(*bad)
    # policy: block / allow
    blk = kg_store.Store(home, sp, rp, paywalled_assets="block")
    with pytest.raises(kg_store.NotFound):
        blk.figure("standard:2", "bbbb2222/f0001")
    with pytest.raises(kg_store.NotFound):
        blk.table_csv("standard:2", "bbbb2222/t0002")
    assert blk.list_assets("standard:2")["documents"][0]["assets"][0]["available"] is False
    assert blk.figure("standard:1", "aaaa1111/f0001")["png"]                              # public content unaffected
    alw = kg_store.Store(home, sp, rp, paywalled_assets="allow")
    assert not alw.figure("standard:2", "bbbb2222/f0001")["info"]["watermarked"]
    with pytest.raises(ValueError):
        kg_store.Store(home, sp, rp, paywalled_assets="nope")


def test_assets_over_mcp(assets_home):
    import base64
    import threading

    import uvicorn
    home, sp, rp = assets_home
    tokens = kg_mcp.TokenStore(home / "mcp" / "tokens.json")
    secret = tokens.create("t")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    app = kg_mcp.build_app(kg_store.Store(home, sp, rp), tokens, port=port)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)

    async def go():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {secret}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                lst = (await s.call_tool("list_assets", {"ref": "standard:2"})).structuredContent
                assert lst["documents"][0]["paywalled"] and lst["documents"][0]["warnings"]
                res = await s.call_tool("get_figure", {"ref": "standard:2", "asset_id": "bbbb2222/f0001"})
                kinds = [c.type for c in res.content]
                assert "image" in kinds and "text" in kinds
                info = json.loads(next(c.text for c in res.content if c.type == "text"))
                assert info["watermarked"] and any("reference only" in x for x in info["warnings"])
                img = next(c for c in res.content if c.type == "image")
                assert img.mimeType == "image/png" and base64.b64decode(img.data)[:4] == b"\x89PNG"
                tb = (await s.call_tool("get_table_csv", {"ref": "standard:2", "asset_id": "bbbb2222/t0002"})).structuredContent
                assert tb["csv"].startswith("a,b") and tb["warnings"] and tb["provenance"]["entry"] == "standard:2"
                bad = await s.call_tool("get_figure", {"ref": "standard:1", "asset_id": "bbbb2222/f0001"})
                assert bad.isError
                txt = (await s.read_resource("kg://table/bbbb2222/t0002")).contents[0].text
                assert txt.startswith("# WARNING") and "a,b" in txt
                blob = (await s.read_resource("kg://figure/bbbb2222/f0001")).contents[0]
                assert base64.b64decode(blob.blob)[:4] == b"\x89PNG"

    try:
        asyncio.run(go())
    finally:
        srv.should_exit = True


def test_wildcard_public_host_disables_host_check(home):
    h, sp, rp = home
    from starlette.testclient import TestClient
    tokens = kg_mcp.TokenStore(h / "mcp" / "tokens3.json")
    tok = tokens.create("t")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {tok}", "Host": "random-name.trycloudflare.com"}
    for hosts, want_ok in ((["*"], True), ([], False)):
        app = kg_mcp.build_app(kg_store.Store(h, sp, rp), tokens, public_hosts=hosts)
        with TestClient(app) as c:
            r = c.post("/mcp", json=init, headers=hdr)
            assert (r.status_code == 200) == want_ok, (hosts, r.status_code)
            assert c.post("/mcp", json=init, headers={**hdr, "Authorization": "Bearer nope"}).status_code == 401


def test_unconvertible_figure_gives_a_clear_error(assets_home):
    home, sp, rp = assets_home
    a = home / "staging/documents/aaaa1111/assets/h1"
    (a / "figures" / "f0077.avif").write_bytes(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20)
    m = json.loads((a / "manifest.json").read_text())
    m["assets"].append({"id": "f0077", "kind": "figure", "page": 1, "caption": "x", "file": "figures/f0077.avif"})
    (a / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(kg_store.NotFound, match="cannot be converted"):
        kg_store.Store(home, sp, rp).figure("standard:1", "aaaa1111/f0077")


def test_asset_ids_accept_short_prefixes_and_graph_node_ids(assets_home):
    """T-02: graph node ids show only a prefix of the document id; the asset tools must resolve it and explain misses."""
    home, sp, rp = assets_home
    st = kg_store.Store(home, sp, rp)
    full = st.table_csv("standard:1", "aaaa1111/t0002")
    assert st.resolve_docid("aaaa11") == "aaaa1111" and st.resolve_docid("AAAA1111") == "aaaa1111"
    assert st.table_csv("standard:1", "aaaa1111:t0002")["csv"] == full["csv"]                   # a graph node id
    assert st.table_csv("standard:1", "aaaa11/t0002")["csv"] == full["csv"]                     # a unique prefix
    assert st.list_assets("aaaa11")["documents"][0]["document_id"] == "aaaa1111"
    with pytest.raises(kg_store.NotFound, match="unknown document id 'cccc3333'"):
        st.list_assets("cccc3333")
    with pytest.raises(kg_store.NotFound, match="belongs to standard:2"):                        # exists, but another entry owns it
        st.table_csv("standard:1", "bbbb2222/t0002")
    with pytest.raises(kg_store.NotFound, match="unknown document id"):
        st.table_csv("standard:1", "zzzz9999/t0002")


def test_no_response_leaks_local_paths(tmp_path):
    """S-01: file:// URLs and the agent / user home directories never reach a client."""
    home = tmp_path / "agent home"
    home.mkdir()
    kg_mcp.set_private_roots(home)
    try:
        h = str(home.resolve())
        out = kg_mcp.scrub({"url": f"file://{h}/local/standards/30/ASME_NQA-1_1989.pdf", "fetched_from": f"file://{h}/elsewhere/x.pdf",
                            "msg": f"place it in {h}/local/standards/30/", "other": "file:///home/someone/Documents/y.pdf",
                            "nested": [{"p": str(Path.home().resolve()) + "/notes"}], "n": 3})
        assert out["url"] == "local:standards/30/ASME_NQA-1_1989.pdf" and out["fetched_from"] == "<agent-home>/elsewhere/x.pdf"
        assert out["msg"] == "place it in local:standards/30/" and out["other"] == "local:y.pdf" and out["n"] == 3
        assert out["nested"][0]["p"] == "~/notes"
        assert "file:///" not in json.dumps(out) and h not in json.dumps(out)
    finally:
        kg_mcp.set_private_roots(tmp_path / "unused")
