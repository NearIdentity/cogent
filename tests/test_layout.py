"""Reading order on multi-column pages (R-01)."""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_layout as LY  # noqa: E402


@dataclass
class B:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float


W = 600.0


def two_columns(left_names, right_names, left_x=(50, 280), right_x=(320, 550), top=100, step=60):
    boxes = [B(n, left_x[0], top + i * step, left_x[1], top + i * step + 40) for i, n in enumerate(left_names)]
    boxes += [B(n, right_x[0], top + 25 + i * step, right_x[1], top + 25 + i * step + 40) for i, n in enumerate(right_names)]   # staggered: a y-sort interleaves them
    return boxes


def names(xs):
    return [b.name for b in xs]


def test_two_columns_are_read_column_by_column():
    boxes = two_columns(["1 PURPOSE", "2 APPLICABILITY", "3 SCOPE", "4 DEFINITIONS"], ["5 RESPONSIBILITY", "6 DOCUMENT CONTROL", "7 AUDITS", "8 RECORDS"])
    y_only = names(sorted(boxes, key=lambda b: b.y0))
    assert y_only[:4] != ["1 PURPOSE", "2 APPLICABILITY", "3 SCOPE", "4 DEFINITIONS"]                     # the old order interleaves
    assert names(LY.order(boxes, W)) == ["1 PURPOSE", "2 APPLICABILITY", "3 SCOPE", "4 DEFINITIONS", "5 RESPONSIBILITY", "6 DOCUMENT CONTROL", "7 AUDITS", "8 RECORDS"]


def test_off_centre_gutter_and_a_spanning_title():
    boxes = two_columns(["a1", "a2", "a3"], ["b1", "b2", "b3"], left_x=(40, 200), right_x=(240, 560), top=160)       # the gutter is not at the page middle
    boxes.append(B("TITLE", 40, 60, 560, 100))                                                                       # full-width
    boxes.append(B("FOOT", 40, 400, 560, 430))
    assert names(LY.order(boxes, W)) == ["TITLE", "a1", "a2", "a3", "b1", "b2", "b3", "FOOT"]


def test_columns_below_a_wide_block_are_ordered_separately():
    top = two_columns(["a1", "a2", "a3"], ["b1", "b2", "b3"], top=100, step=40)
    mid = B("WIDE TABLE", 40, 300, 560, 340)
    bottom = two_columns(["c1", "c2", "c3"], ["d1", "d2", "d3"], top=360, step=40)
    assert names(LY.order(top + [mid] + bottom, W)) == ["a1", "a2", "a3", "b1", "b2", "b3", "WIDE TABLE", "c1", "c2", "c3", "d1", "d2", "d3"]


def test_single_column_and_sparse_pages_keep_top_to_bottom_order():
    one = [B(f"p{i}", 60, 80 + i * 50, 540, 120 + i * 50) for i in range(6)]
    assert names(LY.order(list(reversed(one)), W)) == [f"p{i}" for i in range(6)]
    few = [B("b", 320, 100, 550, 140), B("a", 50, 100, 280, 140), B("c", 50, 200, 280, 240)]
    assert names(LY.order(few, W)) == ["a", "b", "c"]                                                                 # too little to call it columns: rows, left to right


def test_text_row_with_cells_stays_together():
    row = [B("cell1", 50, 100, 150, 120), B("cell2", 200, 101, 300, 121), B("cell3", 350, 99, 450, 119)]
    below = [B("next", 50, 140, 450, 160)]
    assert names(LY.order(row + below, W)) == ["cell1", "cell2", "cell3", "next"]


def test_numbering_check():
    heads = [("a", t) for t in ["1 PURPOSE", "3 RESPONSIBILITY", "2 APPLICABILITY", "5 X", "10 INSPECTION", "11 TEST CONTROL", "6 DOCUMENT CONTROL", "3.1 Sub", "Foreword"]]
    probs = LY.numbering_problems(heads)
    assert len(probs) == 2 and "section 2" in probs[0] and "section 6" in probs[1] and "after section 11" in probs[1]
    assert LY.numbering_problems([("a", t) for t in ["1 A", "2 B", "3 C", "3.1 D", "4 E"]]) == []


def test_basic_pdf_parser_reads_a_two_column_page_in_order(tmp_path):
    import pymupdf

    import kg_ingest
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    left = ["1 PURPOSE", "2 APPLICABILITY", "3 SCOPE", "4 DEFINITIONS"]
    right = ["5 RESPONSIBILITY", "6 DOCUMENT CONTROL", "7 AUDITS", "8 RECORDS"]
    for i, t in enumerate(left):
        page.insert_textbox(pymupdf.Rect(50, 100 + i * 120, 280, 160 + i * 120), t + "\nThe text of this section is written here in a sentence.", fontsize=11)
    for i, t in enumerate(right):
        page.insert_textbox(pymupdf.Rect(320, 130 + i * 120, 550, 190 + i * 120), t + "\nThe text of this section is written here in a sentence.", fontsize=11)
    path = tmp_path / "two.pdf"
    doc.save(str(path))
    kg_ingest.set_pdf_engine("basic")
    els = kg_ingest.load_pdf(str(path))
    firsts = [e.text.split("\n")[0].split(" The text")[0] for e in els if e.kind in ("paragraph", "heading")]
    assert firsts == left + right
