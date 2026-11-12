"""Markdown rendering of the MCP server's tabular data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_helpers import lookup_rows  # noqa: E402

from kg_chat import render  # noqa: E402


def test_cells_are_verbatim_but_cannot_break_the_row():
    assert render.md_cell("4.0E-4") == "4.0E-4" and render.md_cell("≥ 5") == "≥ 5" and render.md_cell(7) == "7" and render.md_cell(None) == ""
    assert render.md_cell("A|B") == "A\\|B" and render.md_cell("line1\nline2") == "line1 line2" and render.md_cell({"a": 1}) == '{"a": 1}'
    t = render.md_table(["a", "b"], [["x|y", "z"], ["only one"]])
    lines = t.splitlines()
    assert lines[0] == "| a | b |" and lines[1] == "| --- | --- |" and lines[2] == "| x\\|y | z |" and lines[3] == "| only one |  |"
    assert all(l.count(" | ") + 1 == 2 or "\\|" in l for l in lines)


def test_graph_table_node_first_row_is_the_header():
    md = render.graph_table('[["Cat", "Value"], ["SDC-3", "4.0E-4"]]')
    assert md.splitlines() == ["| Cat | Value |", "| --- | --- |", "| SDC-3 | 4.0E-4 |"]
    assert render.graph_table("not json") == "" and render.graph_table([]) == "" and render.graph_table([["a"], "x"]) == ""


def test_lookup_rows_become_one_markdown_table_per_source_table():
    views = render.lookup_views(lookup_rows())
    assert len(views) == 1
    v = views[0]
    assert v.title.startswith("Table 4-1") and v.columns == ["Seismic Category", "Structure Type", "Damping (%)", "similarity"]
    assert v.rows[0] == ["SDC-3", "Welded steel", "4", "0.91"] and v.rows[1][1] == "Reinforced concrete"
    md = v.markdown
    assert md.startswith("**Table 4\\-1") or md.startswith("**Table 4-1")
    assert "| SDC-3 | Welded steel | 4 | 0.91 |" in md and "> Units: Damping (%) → %" in md
    assert "Seismic Design Criteria, p. 12" in md and v.source.page == 12 and v.source.url == "https://www.example.org/docs/a.html"


def test_other_tool_results_render():
    q = render.query_view({"table_id": "d/t1", "columns": ["x"], "rows": [{"row": 1, "values": {"Seismic Category": "SDC-4", "Damping (%)": "7"}, "typed": {}}],
                           "count": 1, "table": {"caption": "Table 4-1", "source": {"document": "D", "page": 3}}, "units": {"Damping (%)": "%"}})
    assert "| SDC-4 | 7 |" in q.markdown and "1 row(s)" in q.markdown
    j = render.join_view({"left": "a/t1", "right": "b/t2", "on": "cat = cat", "count": 1, "rows": [{"l_cat": "SDC-3", "r_cat": "SDC-3", "r_damping": "4"}]})
    assert "left cat" in j.markdown and "right damping" in j.markdown and "on cat = cat" in j.markdown
    d = render.describe_views({"table": {"table_id": "t", "caption": "Cap", "columns": [{"name": "c", "header": "C (%)", "pg_type": "numeric", "unit": "%", "n_distinct": 3, "min": 1, "max": 9}],
                                         "sample_rows": [{"C (%)": "1"}]}})
    assert len(d) == 2 and "| c | C (%) | numeric | % | 3 | 1 | 9 |" in d[0].markdown and "| 1 |" in d[1].markdown
    s = render.search_tables_view({"mode": "semantic", "tables": [{"table_id": "t1", "caption": "C", "source": {"document": "D", "page": 2, "caption": "C"}, "similarity": 0.8}]})
    assert "| t1 | C | D | 2 | 0.80 |" in s.markdown
    e = render.entries_view([{"entry": "standard:1", "name": "N", "version_status": "current", "has_graph": True, "paywalled": True, "open_warnings": 2}])
    assert "| standard:1 | N | current | yes | paywalled | 2 |" in e.markdown
    assert render.query_view({"rows": [], "columns": []}) is None and render.join_view({"rows": []}) is None


def test_titles_cannot_inject_markdown():
    v = render.entries_view([])
    assert "0 entries" in v.markdown
    t = render._view("*bold* [x](http://evil) `c`", ["a"], [["1"]], None)
    assert "\\*bold\\*" in t.markdown and "\\[x\\]" in t.markdown
