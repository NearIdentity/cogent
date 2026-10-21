"""Footnote markers inside PDF table cells (TB-01): 'NDC-2' + raised 4 must not become the value 'NDC-24'."""
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_ingest  # noqa: E402


def table_pdf(path, cells):
    """cells: {(row, col): (text, superscript or None)}, drawn in a ruled grid so that PyMuPDF finds the table."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    xs, ys = [60, 200, 340, 480], [100, 130, 160, 190, 220]
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for (r, c), (text, sup) in cells.items():
        x, y = xs[c] + 6, ys[r] + 20
        page.insert_text((x, y), text, fontsize=11)
        if sup:
            page.insert_text((x + pymupdf.get_text_length(text, fontsize=11) + 0.5, y - 5), sup, fontsize=7)
    doc.save(str(path))


def load(path):
    kg_ingest.set_pdf_engine("basic")
    return [e for e in kg_ingest.load_pdf(str(path)) if e.kind == "table"]


def test_superscript_markers_are_split_from_values(tmp_path):
    table_pdf(tmp_path / "a.pdf", {(0, 0): ("Category", None), (0, 1): ("Worker", "2"), (0, 2): ("holds", "1"),
                                   (1, 0): ("NDC-2", "4"), (1, 1): ("10 mSv", None), (1, 2): (">= PAC-1", "3"),
                                   (2, 0): ("NDC-3", None), (2, 1): ("50 mSv", None), (2, 2): ("PAC-2", None)})
    (t,) = load(tmp_path / "a.pdf")
    assert t.table == [["Category", "Worker", "holds"], ["NDC-2", "10 mSv", ">= PAC-1"], ["NDC-3", "50 mSv", "PAC-2"]]
    assert t.meta["footnotes"] == [{"row": 0, "col": 1, "marker": "2"}, {"row": 0, "col": 2, "marker": "1"},
                                   {"row": 1, "col": 0, "marker": "4"}, {"row": 1, "col": 2, "marker": "3"}]


@pytest.mark.parametrize("text,sup", [("1.0 x 10", "4"), ("Area in m", "2"), ("Dose in cm", "3")])
def test_exponents_and_unit_powers_are_not_footnotes(tmp_path, text, sup):
    table_pdf(tmp_path / "b.pdf", {(0, 0): ("Name", None), (0, 1): ("Value", None), (0, 2): ("Unit", None),
                                   (1, 0): ("x", None), (1, 1): (text, sup), (1, 2): ("y", None), (2, 0): ("z", None), (2, 1): ("1", None), (2, 2): ("2", None)})
    (t,) = load(tmp_path / "b.pdf")
    assert t.table[1][1] == text + sup and "footnotes" not in t.meta                       # text extraction keeps 'm2' / '10^4' as one value


def test_a_letter_marker_and_no_superscripts(tmp_path):
    table_pdf(tmp_path / "c.pdf", {(0, 0): ("Item", None), (0, 1): ("Limit", None), (0, 2): ("Note", None),
                                   (1, 0): ("Alpha", "a"), (1, 1): ("5", None), (1, 2): ("ok", None), (2, 0): ("Beta", None), (2, 1): ("6", None), (2, 2): ("ok", None)})
    (t,) = load(tmp_path / "c.pdf")
    assert t.table[1][0] == "Alpha" and t.meta["footnotes"] == [{"row": 1, "col": 0, "marker": "a"}]


def test_marker_rules():
    f = kg_ingest._is_footnote_marker
    assert f("NDC-2", "4") and f("Worker", "2") and f("Value", "a") and f("x", "*")
    assert not f("1.0 x 10", "4") and not f("10", "-4") and not f("Area in m", "2") and not f("Worker", "")
    assert not f("Worker", "123")
