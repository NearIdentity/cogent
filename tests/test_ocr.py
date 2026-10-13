"""OCR for scanned PDF pages: text first, OCR only where there is no text layer."""
import json
import logging
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kg_agent  # noqa: E402
import kg_ingest  # noqa: E402
import kg_ocr  # noqa: E402
import kg_tools as T  # noqa: E402
from mock_site import Site, make_pdf, make_scan  # noqa: E402


class FakeEngine(kg_ocr.OCREngine):
    """Returns canned lines (page points) and counts its calls."""
    name = "fake"

    def __init__(self, lines=None):
        self.calls = []
        self.canned = lines or [
            kg_ocr.OCRLine("SCANNED STANDARD", 200, 60, 420, 90, 0.9),                 # tall -> heading
            kg_ocr.OCRLine("1. Scope", 72, 120, 150, 136, 0.9),
            kg_ocr.OCRLine("This text exists only as a picture and is read by", 72, 150, 500, 162, 0.9),
            kg_ocr.OCRLine("optical character recognition on the page.", 72, 164, 400, 176, 0.9),
            kg_ocr.OCRLine("Page 7", 280, 815, 330, 827, 0.9),                         # page number -> dropped
        ]

    def lines(self, page, dpi, lang):
        self.calls.append(page.number + 1)
        return list(self.canned)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    kg_ingest.set_pdf_engine("basic")
    kg_ingest.set_ocr("auto")
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", None)
    yield
    kg_ingest.set_ocr("auto")


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_text_pdf_is_never_ocred(tmp_path, monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", eng)
    els = kg_ingest.load_pdf(_write(tmp_path, "t.pdf", make_pdf("Real Text", "A paragraph with a text layer. " * 5)))
    assert eng.calls == [] and els and not any(e.meta.get("ocr") for e in els)


def test_scanned_pdf_is_read_by_ocr(tmp_path, monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", eng)
    path = _write(tmp_path, "s.pdf", make_scan("Scan", "Some words on the page. " * 5))
    els = kg_ingest.load_pdf(path)
    assert eng.calls == [1]
    kinds = [(e.kind, e.text) for e in els]
    assert ("heading", "SCANNED STANDARD") in kinds and ("heading", "1. Scope") in kinds
    para = next(e for e in els if e.kind == "paragraph")
    assert para.text.startswith("This text exists only as a picture") and "recognition on the page." in para.text
    assert all(e.meta["ocr"] and e.meta["ocr_engine"] == "fake" for e in els) and all("Page 7" not in e.text for e in els)
    g = kg_ingest.file_to_knowledge_graph(path)
    assert g.graph["ocr_pages"] == [1] and g.graph["ocr_engine"] == "fake"
    assert any(d.get("ocr") == "true" for _, d in g.nodes(data=True))


def test_mixed_document_only_ocrs_the_empty_page(tmp_path, monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", eng)
    a = pymupdf.open(stream=make_pdf("Text Page", "Real text layer here. " * 6), filetype="pdf")
    b = pymupdf.open(stream=make_scan("x", "picture page. " * 6), filetype="pdf")
    a.insert_pdf(b, from_page=0, to_page=0)
    els = kg_ingest.load_pdf(_write(tmp_path, "m.pdf", a.tobytes()))
    assert eng.calls == [2]                                          # page 1 has text: untouched
    assert [e.page for e in els] == sorted(e.page for e in els) and {e.page for e in els} == {1, 2}
    assert len({e.id for e in els}) == len(els)
    assert not any(e.meta.get("ocr") for e in els if e.page == 1) and all(e.meta.get("ocr") for e in els if e.page == 2)


def test_modes_and_missing_engine(tmp_path, monkeypatch, caplog):
    eng = FakeEngine()
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", eng)
    scan = _write(tmp_path, "s.pdf", make_scan("Scan", "Some words on the page. " * 5))
    text = _write(tmp_path, "t.pdf", make_pdf("Real Text", "A paragraph with a text layer. " * 5))
    kg_ingest.set_ocr("off")
    no_text = lambda els: [e for e in els if e.kind in ("paragraph", "heading")]
    assert no_text(kg_ingest.load_pdf(scan)) == [] and eng.calls == []
    kg_ingest.set_ocr("force")
    kg_ingest.load_pdf(text)
    assert eng.calls == [1]                                          # forced even though it has text
    kg_ingest.set_ocr("auto")
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", None)
    monkeypatch.setattr(kg_ocr.TesseractEngine, "available", staticmethod(lambda: False))
    monkeypatch.setattr(kg_ocr.RapidOCREngine, "available", staticmethod(lambda: False))
    with caplog.at_level(logging.WARNING):
        assert no_text(kg_ingest.load_pdf(scan)) == []
    assert "no OCR engine is installed" in caplog.text


def test_ocr_table_reconstruction():
    cells = [("Unit", 230, 336), ("Status", 269, 336), ("Power", 330, 336),
             ("Unit 1", 230, 353), ("Operating", 269, 353), ("1311", 330, 353),
             ("Unit 2", 230, 372), ("Repair", 267, 372), ("0", 330, 372)]
    lines = [kg_ocr.OCRLine(t, x, y, x + 30, y + 11, 0.9) for t, x, y in cells]
    lines.append(kg_ocr.OCRLine("Table 1: Unit status", 78, 318, 164, 330, 0.9))
    els = kg_ocr.lines_to_elements(lines, 612, 792, 1, lambda p: "tmp", kg_ingest.Element, "fake")
    tbl = next(e for e in els if e.kind == "table")
    assert tbl.table == [["Unit", "Status", "Power"], ["Unit 1", "Operating", "1311"], ["Unit 2", "Repair", "0"]]
    assert tbl.caption == "Table 1: Unit status" and not any(e.text == "Table 1: Unit status" for e in els if e.kind == "paragraph")


def test_two_column_reading_order():
    left = [kg_ocr.OCRLine(f"left line {i}", 72, 100 + 14 * i, 280, 111 + 14 * i) for i in range(6)]
    right = [kg_ocr.OCRLine(f"right line {i}", 330, 100 + 14 * i, 540, 111 + 14 * i) for i in range(6)]
    order = [l.text for l in kg_ocr.order_lines(right + left, 612)]
    assert order == [f"left line {i}" for i in range(6)] + [f"right line {i}" for i in range(6)]


def test_scanned_files_are_fingerprinted_by_content():
    a, b = make_scan("One", "first scan " * 10), make_scan("Two", "second scan " * 10)
    assert T.fingerprint(a, "pdf")[1] != T.fingerprint(b, "pdf")[1]       # empty text layer must not make them equal
    again = pymupdf.open(stream=a, filetype="pdf")
    again.set_metadata({"title": "re-saved"})
    resaved = again.tobytes(garbage=4)
    assert resaved != a and T.fingerprint(resaved, "pdf")[1] == T.fingerprint(a, "pdf")[1]   # same scans, new bytes


def test_real_rapidocr(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    path = _write(tmp_path, "real.pdf", make_scan("Seismic Criteria", "Nuclear facilities shall resist earthquake ground shaking."))
    els = kg_ingest.load_pdf(path)
    text = " ".join(e.text for e in els).lower()
    assert "seismic criteria" in text and "earthquake" in text and all(e.meta["ocr_engine"] == "rapidocr" for e in els)


def test_agent_records_ocr_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(kg_ocr, "ENGINE_OVERRIDE", FakeEngine())
    site = Site()
    try:
        std = {"meta": {}, "standards": [{"id": 5, "document": "Scanned Standard", "links": [{"text": "PDF", "url": site.base + "/docs/scanned.pdf"}],
                                          "update_tracking_urls": []}]}
        sp, rp = tmp_path / "s.json", tmp_path / "r.json"
        sp.write_text(json.dumps(std))
        rp.write_text(json.dumps({"meta": {}, "reports": []}))
        s = kg_agent.Settings(home=tmp_path / "home", standards=sp, reports=rp, min_interval=0)
        st = kg_agent.run_once(s)
        assert "OCR on page(s) [1]" in st["report"]
        vt = json.loads(sp.read_text())["standards"][0]["version_tracking"]
        assert vt["staged"]["documents"][0]["ocr_pages"] == [1]
        st = kg_agent.run_once(s)                                         # unchanged scan: not rebuilt
        assert st["summary"]["docs_unchanged"] == 1
    finally:
        site.close()
