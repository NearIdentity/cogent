"""Edition identifiers read from file names (V-01): separators such as ``_`` and ``SEI43`` must not hide the edition."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kg_tools as T  # noqa: E402


def ids(name: str) -> list[tuple[str, str | None]]:
    return [(i.label, i.edition) for i in T.parse_identities(*T.filename_variants(name))]


@pytest.mark.parametrize("name,want", [
    ("ASCE_SEI43-05.pdf", [("ASCE 43", "05")]),
    ("ASCE SEI 43-19.pdf", [("ASCE 43", "19")]),
    ("ASCE/SEI 43-19.pdf", [("ASCE 43", "19")]),
    ("ASME_NQA-1_1989.pdf", [("NQA 1", "1989")]),
    ("ASME_NQA-1-2008.pdf", [("NQA 1", "2008")]),
    ("DOE-STD-1020-2016.pdf", [("DOE-STD 1020", "2016")]),
    ("random_notes.pdf", []),
])
def test_file_name_editions(name, want):
    assert ids(name) == want


def test_running_text_is_not_affected():
    assert [(i.label, i.edition) for i in T.parse_identities("see ASCE/SEI 43-19 and ASCE 4-16")] == [("ASCE 43", "19"), ("ASCE 4", "16")]
    assert [(i.label, i.edition) for i in T.parse_identities("NQA-1 requirements apply")] == [("NQA 1", None)]
