# cogent MCP server: fix list from test round 6

**Server under test:** `cogent-test20261026`
**Test date:** 2026-10-26, 22:10–22:22 ET
**Server state:** agent run at 2026-10-27T01:40:50Z; table DB 606 tables; 45 entries
**Builds on:** `cogent_mcp_test_report_v2.md` (issues from rounds 1–5)
**Tester:** Claude (claude.ai), MCP tools only; root causes are hypotheses from tool output

Severity: **P1** = wrong or misleading result a user is likely to hit. **P2** = quality or ergonomics.

---

## Summary

| ID | Sev | Area | Issue |
|---|---|---|---|
| R6-01 | P1 | Table lookup | `lookup_values` returns unrelated rows with no "no confident match" signal |
| R6-02 | P1 | Provenance | Part 50 tables (§ 50.61a, § 50.75) are attributed to the "Appendix B" document of standard:28 |
| R6-03 | P1 | Join inference | The key join in NE-STD-1027 (Table A-6 → Table 1-1) is not inferred |
| R6-04 | P1 | Table DB freshness | NE-STD-1027 tables not reloaded after the graph rebuild; old extraction defects persist |
| R6-05 | P2 | Join inference | Junk joins pass the default `min_score` (list numbers, table-reference columns) |
| R6-06 | P2 | Table DB | Store-page "Quantity / Item" tables and NQA-1 interpretation indexes ingested as data tables |
| R6-07 | P2 | Extraction | "ANSI/ANS-2.26-2004" split into entities "ANSI" and "ANS-2"; no Identifier |
| R6-08 | P2 | Entities | Singular and plural forms are separate entities (SDC/SDCs, SSC/SSCs) |
| R6-09 | P2 | Graph | Entities not linked to the tables that use them (SDC → Table 3-1) |
| R6-10 | P2 | Graph | Circular `related_to` + `has_value` edges between "Seismic Design Category" and "SDC" |
| R6-11 | P2 | Errors | `get_node` suggestions offered on entry graphs but not on `combined` |
| R6-12 | P2 | Errors | `get_node` suggestions are spelling matches with no relevance cutoff |
| R6-13 | P2 | Errors | Unknown entry reported as "no knowledge graph staged" |
| R6-14 | P2 | Operations | Many entry graphs still on 10-20/21 builds; extraction fixes not applied |
| R6-15 | — | Verify | Last run dropped 11 tables (610 → 606); confirm they were intended |

Still open from earlier rounds and re-observed today: **V-01** (ASCE 43 2005 copy, only `info` severity), **TB-01 / TB-03** (NE-STD-1027 headers and footnotes; see R6-04), **X-01** (junk relation labels in older builds).

---

## P1

### R6-01: `lookup_values` gives no "not found" signal

**Repro**
```json
lookup_values {"question": "allowable stress for ASTM A572 steel", "retrieval": "hybrid", "units": "broad"}
search_tables {"question": "A572", "retrieval": "lexical"}
```

**Observed**
- `search_tables` (lexical) returns 0 tables: "A572" appears nowhere.
- `lookup_values` still returns 10 rows at similarity ~0.68, all from § 50.61a pressurized-thermal-shock tables (RT_MAX limits in °F, flaw counts). Nothing says they are unrelated.
- The session `min_score` of 0.55 filters nothing.

**Risk:** a user or agent can present RT_MAX values as an allowable-stress answer.

**Fix**
- When the question contains a code-like token (A572, SA-516, RG 1.233), require it to appear lexically in the returned table or row, or flag the result.
- Add `match_quality` and `no_confident_match: true` when the top similarity is under a calibrated threshold or there is no lexical match. Include a message such as "no table mentions 'A572'; rows below are nearest neighbours only".
- Recalibrate the default `min_score` for row retrieval; 0.55 is below the similarity of clearly irrelevant rows.

**Acceptance:** the A572 query returns `no_confident_match: true` (or zero rows) and a clear message.

### R6-02: Part 50 tables attributed to "Appendix B"

**Repro:** the `lookup_values` call in R6-01; inspect `table.source`.

**Observed:** the tables have `document: "Appendix B"`, document `1b3e9cb2fc97bb1f`, entry standard:28. But their `section` is "PART 50 … § 50.61a Alternate fracture toughness requirements…" (and § 50.75 for `tbl2392`). The document behind standard:28 seems to be the full Part 50 eCFR page, labelled "Appendix B".

**Risk:** citations point to the wrong regulation, and Part 50 content is duplicated across standard:1 and standard:28.

**Fix**
- Fetch only Appendix B for standard:28 (the eCFR supports appendix-level URLs), or keep the full page but tag nodes and tables by actual section and attribute them to the most specific entry.
- Set `doc_label` from the document's real scope, not the entry name.

**Acceptance:** § 50.61a tables report entry standard:1 (Part 50) and a Part 50 label; standard:28 holds only Appendix B (plus RG 1.28) content.

### R6-03: Key NE-STD-1027 join not inferred

**Repro**
```json
get_relationship_map {"ref": "57988ceda0881836", "include_rejected": true}
join_tables {"left_table": "57988ceda0881836/tbl0324", "right_table": "57988ceda0881836/tbl0224",
             "left_column": "column_1", "right_column": "rdilid"}
```

**Observed**
- The relationship map lists 5 joins, none involving Table 1-1 (`tbl0224`).
- The manual join on Table A-6 `column_1` = Table 1-1 `rdilid` returns **33 matching rows**.
- Table A-6's first column holds "Isotope Name Used in Table 1-1" as a data cell. Both key columns have garbage headers (`column_1`, `rdilid`), so name similarity is ~0.

**Likely cause:** candidate generation weights header-name similarity heavily, and the value-overlap test isn't run, or doesn't score enough, when headers are meaningless.

**Fix**
- Always run value-containment checks for high-cardinality text key columns, even without a header match (33 of ~36 values contained here).
- Treat a header or first cell that names another table ("…Used in Table 1-1") as strong evidence.
- Repair headers first (promote the real header row; see TB-03), which also helps this.

**Acceptance:** the relationship map includes `tbl0324.column_1 → tbl0224.<radionuclide>` as N:1 with score ≥ 0.8.

### R6-04: NE-STD-1027 tables not reloaded after the graph rebuild

**Repro:** `search_tables {"question": "isotope radionuclide storage inventory quantity"}`; check `loaded_at` and `list_entries`.

**Observed**
- Graph `graph_built: 2026-10-26T20:18:47Z`, but its tables still have `loaded_at: 2026-10-21T03:22:5x`.
- Table 1-1 still has caption "Rdilid | TABLE 1 Hazard Ca | -1: THRESHOL tegory 2(A) | …", no `table_label`, and garbled column names.
- The defect where `explain_units` reads "(A)" as amperes (TB-01 family) is presumably still present.

**Fix**
- Reload a document's tables whenever its graph is rebuilt with a newer extraction recipe, or record a `recipe_version` per table and reload when it's behind.
- Expose `tables_recipe` and `tables_loaded_at` in `get_entry_metadata` so mismatches are visible.

**Acceptance:** after a rebuild, `loaded_at` ≥ `graph_built` for every table of the entry, and Table 1-1 has a clean caption, the label "Table 1-1", columns named radionuclide / HC-2 Ci / HC-2 g / HC-3 Ci / HC-3 g / pathway, and units Ci and g (not A).

---

## P2

### R6-05: Junk joins pass the default `min_score`

**Repro:** `get_relationship_map {"ref": "57988ceda0881836"}`

**Observed**
- `tbl0333.c_1 = tbl0339.c_1`, score **0.95**: the key values are list numbers "2.", "3.", "4.".
- `tbl0317.column_4 = tbl0316.column_4`, score 0.74: the values are table references ("table 1: table 2:", "entire repo").

**Fix:** reject keys that are mostly ordinal list markers (`^\d+\.?$` with small cardinality) or cross-reference text. Lower the score when both columns have placeholder names (`column_N`, `c_N`). Consider making the default `approach` require LLM or judge agreement for keys with low semantic content.

### R6-06: Non-data tables in the table database

**Repro:** the R6-04 search.

**Observed:** returned as "tables":
- `629013b35592ab45/tbl0023` and `c90b611d71b4c912/tbl0030`: ASME store-page "Quantity | Item" product-option boxes.
- `51af9c128a2bd7d3/tbl1551` ("Subject index") and `tbl1336` ("Numerical index"): NQA-1 interpretation indexes.
- `1b3e9cb2fc97bb1f/tbl2392`, caption "| Millions" (§ 50.75 decommissioning amounts): the caption is a fragment.

**Fix:** extend the front-matter / non-table filter (the glossary `front_matter` flag now works) to:
- e-commerce widgets on store pages (`role: store page`)
- index and interpretation-index sections
- tables whose caption is a single header fragment; fall back to the nearest "Table N" text or section heading

### R6-07: ANS standard references not recognized as identifiers

**Repro**
```json
get_neighbors {"ref": "standard:16", "node_id": "d74d69e8:p0069"}
```

**Observed:** "[ANSI/ANS-2.26-2004]" produces the entities `ANSI` and `ANS-2`. There's no Identifier, so the definition isn't linked to ANSI/ANS-2.26. The same applies to ANSI/ANS-2.27-2020 in the next paragraph.

**Fix:** add patterns `(ANSI/)?ANS[- ]\d+(\.\d+)*(-\d{4})?( \(R\d{4}\))?` and the ANSI/ASME, ANSI/AISC and ANSI/IEEE forms to the identifier recognizer, ahead of entity splitting. This is the same class as X-03 (ASME/ANS RA-S-1.4).

### R6-08: Singular and plural entity duplicates

**Observed:** separate nodes for `ent:sdc` / `ent:sdcs` and `ent:ssc` / `ent:sscs`.

**Fix:** lemmatize or normalize entity keys (strip a plural "s" for acronyms, plural nouns to singular); keep surface forms as aliases.

### R6-09: Entities not linked to tables

**Observed:** `ent:seismic design category` neighbors: one paragraph, the glossary table, and `ent:sdc`. There's no link to NE-STD-1020 Table 3-1 (SDC-1/2 coefficients), and `ent:sdc1` / `ent:sdc5` have no table-row links.

**Fix:** when loading tables, emit MENTIONS edges from Table / TableRow nodes to entities found in captions, headers and key cells (e.g. SDC-1, SDC-2, FDC-3).

### R6-10: Circular SDC edges

**Observed:** `ent:seismic design category —related_to→ ent:sdc` and `ent:sdc —has_value→ ent:seismic design category`.

**Fix:** model acronym expansion as one `alias_of` / `abbreviation_of` edge (or merge the nodes); `has_value` should only point to Value nodes.

### R6-11: `get_node` suggestions missing on `combined`

**Repro**
```json
get_node {"ref": "combined",   "node_id": "nonexistent"}   → "node 'nonexistent' not in combined"
get_node {"ref": "standard:6", "node_id": "nonexistent"}   → "...did you mean 'ent:consistent', 'ent:existing'?"
```

**Fix:** use the same suggestion logic for `combined`. Most useful there for entry-ID mistakes (`standard:3` → `entry:standard:3`).

### R6-12: Noisy `get_node` suggestions

**Observed:** "nonexistent" → "ent:consistent", "ent:existing".

**Fix:** apply a minimum similarity (e.g. normalized edit similarity ≥ 0.8). Prefer candidates with the same ID prefix or document; say "no close match" when nothing passes.

### R6-13: Misleading error for an unknown entry

**Repro:** `get_node {"ref": "standard:999", "node_id": "x"}` → "no knowledge graph staged for 'standard:999'"

**Fix:** tell an unknown entry apart from a known entry without a graph, e.g. "unknown entry 'standard:999' (valid: standard:1–30, report:1–15)". Apply this to every tool that takes `ref` / `entry`.

### R6-14: Stale graph builds

**Observed (list_entries):** these are still on 2026-10-20/21 builds, before the extraction fixes verified in round 5 (verb-lemma relations, plural identifiers, `part_of`, single-target resolution):
- Part 50, Part 52, 50.69, Part 73, Part 100, Part 810
- Parts 830, 835, 851; DOE O 420.1D
- DOE-STD-1271, NE-STD-1020
- NEI 18-04, RG 1.233, NEI 21-07/RG 1.253, RG 1.200, RG 1.232, RG 1.206
- Most reports

Effects: X-01 junk relations and the missing 50.69 → Part 100 / Appendix B edges (X-06) persist in these graphs.

**Fix:** schedule one forced rebuild of all entries after an extraction-recipe change. Record `recipe_version` per graph and report entries behind the current recipe in `get_overview`.

### R6-15: Verify the 11 dropped tables

**Observed:** the last run reports `tabledb.dropped: 11`, `loaded: 13`; the table count went 610 → 606.

**Ask:** confirm the dropped tables were intended (e.g. the new non-table filter) and that none were real data tables. Consider listing dropped `table_id`s in the run summary.

---

## Verified fixed or improved in this round

| Earlier ID | Status | Evidence |
|---|---|---|
| N-08 warning history | **Fixed going forward** | Warnings keep `first_seen: 2026-10-21T18:01:26Z`; `runs: 4`; `is_new: false`. History before 10-21 was not restored |
| N-12 spurious `store_page_changed` | **Resolved** | The three warnings are gone after later runs |
| N-04 / TB-02 front matter | **Improved** | Glossary table flagged `front_matter: "glossary"` |
| A-05 itemized errors | **Holding** | `run_errors` and `document_errors` listed; ascelibrary.org 403 count now 18 |
| T-01 table DB | **Holding** | `reachable: true`, `can_select: true`, 606 tables |
| `get_node` errors | **Improved** | Clear "not in <ref>" message; entry graphs suggest close matches (see R6-11/12) |
| `search_graph` misses | **Holding** | Returns `term_counts` and suggests `match='any'` when no node has all terms |

## Suggested regression tests

1. `lookup_values("allowable stress for ASTM A572")` → `no_confident_match: true` (R6-01).
2. No table under standard:28 has a section path outside Appendix B / RG 1.28 (R6-02).
3. The NE-STD-1027 relationship map includes A-6 → Table 1-1 (R6-03).
4. For every entry, `max(tables.loaded_at) ≥ graph_built` after a rebuild (R6-04).
5. No inferred join whose key values are all `^\d+\.?$` (R6-05).
6. No table from a `store page` document or an index section in the table DB (R6-06).
7. "[ANSI/ANS-2.26-2004]" yields one Identifier `ANS 2.26` (R6-07).
8. `get_node` on an unknown entry returns "unknown entry" (R6-13).
