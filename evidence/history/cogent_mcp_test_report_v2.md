# cogent MCP server: consolidated test report (v2)

**Server under test:** `cogent-test20261021` (MCP connector)
**Report date:** 2026-10-21, 14:05 ET
**Supersedes:** `cogent_mcp_test_report.md` (v1)
**Tester:** Claude (claude.ai) exercising the server through its MCP tools; no access to source code

## Test rounds

| Round | Time (ET) | Server state | Scope |
|---|---|---|---|
| R1 | 00:50–01:00 | Agent run `20261021T041754Z` | Full sweep: navigation, traversal, tables, workflows, edge cases, export |
| R2 | 02:30 | Unchanged | Asset tools with full 16-char document ID |
| R3 | 12:50 | Read-only DB credential fixed | `get_overview`, `list_tables standard:16` |
| R4 | 13:23 | Restart; forced run `20261021T171858Z` (standard:16 only) | `get_overview`, `list_tables standard:16` |
| R5 | 14:02 | Full agent run at 18:01:47Z | `get_overview`, `get_warnings`, `list_tables standard:16` |

## How to use this report

- Each issue has an ID, severity, status, reproduction call, and evidence.
- Root causes are **hypotheses** drawn from tool output. Confirm each one in the code before changing anything.
- Work open issues in severity order.

**Severity**
- **P0:** a tool family is unusable, or the server returns wrong data with no warning.
- **P1:** a wrong or missing result a user is likely to hit.
- **P2:** a quality or ergonomics issue.

**Status**
- **Resolved:** fix confirmed by a later test round.
- **Partial:** improved, but part of the issue remains or is unverified.
- **Open:** confirmed and not yet fixed (or not yet retested after fixes landed).
- **Needs verification:** observed behaviour that may be intended; the owner should confirm.

---

## Summary

| Status | Count |
|---|---|
| Resolved | 7 |
| Partial | 2 |
| Open | 25 |
| Needs verification | 3 |
| **Total** | **37** |

Open by severity: **P0: 1** (V-01) · **P1: 11** · **P2: 13**. Partial: TB-02 (P1), TB-05 (P2).

## Issue index

| ID | Sev | Status | Area | Summary | Last checked |
|---|---|---|---|---|---|
| T-01 | P0 | **Resolved** | Table DB | Read-only user `cogent_ro` failed password auth; all table-DB tools down | R5 |
| V-01 | P0 | Open | Versioning | ASCE 43 local copy is the 2005 edition; config asserts 2019; only an info warning | R5 |
| N-08 | P1 | Open | Warnings | Warning history (`first_seen`, `runs`, `is_new`) wiped by restart/forced run | R5 |
| S-01 | P1 | Open | Security | Absolute local filesystem paths (incl. home dir) exposed in metadata | R1 |
| S-02 | P1 | Open | Sessions | Session preferences keyed on shared `web:default` | R1 |
| X-01 | P1 | Open | Extraction | Relation labels built from raw text fragments (hundreds of junk types) | R1 |
| X-02 | P1 | Open | Extraction | Identifier parser misses plurals ("RGs 1.200 and 1.247") | R1 |
| X-03 | P1 | Open | Extraction | "ASME/ANS RA-S-1.4" typed as Entity/Value, not Identifier | R1 |
| X-04 | P1 | Open | Extraction | `None` leaks into identifier label (`RA-S-1.4-None`) | R1 |
| X-05 | P1 | Open | Combined graph | Self-citation via shared document; identifier fan-out to several entries | R1 |
| X-06 | P1 | Open | Combined graph | "part 100" reference in 50.69 not turned into an entry-level cite | R1 |
| TB-01 | P1 | Open | Tables | Footnote superscripts merged into values (`NDC-24`, `PAC-13`) | R1 (see note) |
| TB-02 | P1 | Partial | Tables | Non-tables ingested as tables (OCR prose, TOC, acronym lists) | R5 |
| R-01 | P1 | Open | Reading order | Two-column pages give out-of-order headings (NQA-1) | R1 |
| T-02 | P2 | Open | Assets | Asset tools reject the 8-char document prefix used in graph node IDs | R2 |
| TB-03 | P2 | Open | Tables | Repeated page headers kept as data rows; OCR garbage in headers | R2 |
| TB-04 | P2 | Open | Tables | Junk joins (committee lists joined on a person's name) | R1 |
| TB-05 | P2 | Partial | Tables | Join evaluation never ran for NE-STD-1020 | R5 |
| A-01 | P2 | Open | API | `get_table` has no pagination; ~166K chars in one response | R1 |
| A-02 | P2 | Open | API | Node-ID forms differ (`standard:3` vs `entry:standard:3`); unhelpful error | R1 |
| A-04 | P2 | Open | Search | Multi-term query with a hyphenated term returns nothing | R1 |
| A-05 | P2 | **Resolved** | Observability | Run errors counted but not listed in `get_overview` | R5 |
| A-06 | P2 | Open | Outline | Outline titles cut at 80 chars with no flag | R1 |
| D-01 | P2 | Open | Data/config | NQA-1 on board is 1989; RG 1.28 Rev. 6 endorses only 2017/2019/2022 | R5 |
| D-02 | P2 | Open | Data | Part 53 graph holds FR rule and eCFR; definitions returned twice | R1 |
| N-01 | P2 | **Resolved** | Tables | `table_label` dropped the subnumber (`Table 5` for 5-1, 5-2, 5-3) | R5 |
| N-02 | P2 | **Resolved** | Tables | `doc_label` was `"PDF"` instead of the entry name | R5 |
| N-03 | P2 | **Resolved** | Tables | Captions had doubled hyphen (`Table 2--1`) and missing space (`andSDC-2`) | R5 |
| N-04 | P2 | **Resolved** | Tables | TOC and acronym list loaded as tables for standard:16 | R5 |
| N-05 | P1 | **Resolved** | Warnings | `open_warnings` dropped to 0 after a single-document forced run | R5 |
| N-06 | P2 | Open | Tables | Page-24 wind table has a caption fragment and no `table_label` | R5 |
| N-07 | P2 | Open | Tables | Section path missing a space: `2.3.3Criteria` | R5 |
| N-09 | P2 | Open | Observability | `run_errors` lists the same 403 twice (tracker and document) | R5 |
| N-10 | P2 | Open | Warnings | `open_warnings` (3) vs `get_warnings` (7 items): counting rule undocumented | R5 |
| N-11 | — | Needs verification | Warnings | QME-1 `new_edition_for_purchase` warning disappeared | R5 |
| N-12 | — | Needs verification | Warnings | Three `store_page_changed` warnings may be spurious after state reset | R5 |
| N-13 | — | Needs verification | Tables | Table 2-1 went from 8 rows to 7 | R4 |

---

## Resolved

### T-01: Table database unreachable for read-only user · P0 · Resolved

**Was (R1):** `search_tables`, `list_tables` and `explain_units` all failed:
```
password authentication failed for user "cogent_ro"
```

**Now (R3–R5):** `get_overview` returns
```json
"table_database": {"configured": true, "reachable": true, "user": "cogent_ro", "built": true, "can_select": true, "tables": 610}
```
and `list_tables {"ref": "standard:16"}` returns 10 tables.

**Not yet retested:** `search_tables`, `query_table`, `explain_units`, `join_tables`, `lookup_values`, `describe_table`. Run each once to close fully.

**Keep as regression test:** startup health check on the `cogent_ro` connection; `table_database` block in `get_overview`.

### A-05: Run errors not itemized · P2 · Resolved

**Was (R1):** `get_overview` showed `errors: 2, docs_error: 1` with no detail.

**Now (R5):** `run_errors` lists each message; `document_errors` gives document ID, entries, URL, error, `consecutive_errors` (15) and `last_checked`. `agent_last_run_summary.error_messages` mirrors `run_errors`. See N-09 for a duplication nit.

### N-01: `table_label` dropped the subnumber · P2 · Resolved

**Was (R3):** Tables 5-1, 5-2 and 5-3 all had `table_label: "Table 5"`.
**Now (R4, R5):** labels are `Table 2-1`, `3-1`, `5-1`, `5-2`, `5-3`, `7-1`, `7-2`, `8-1`, `8-2`.

### N-02: `doc_label` was "PDF" · P2 · Resolved

**Was (R3):** `doc_label: "PDF"`.
**Now (R4, R5):** `doc_label: "NE-STD-1020-2025, Natural Phenomena Hazards Analysis and Design Criteria"`.

### N-03: Caption formatting · P2 · Resolved

**Was (R3):** `Table 2--1`, `SDC-1 andSDC-2`.
**Now (R4, R5):** `Table 2-1`, `SDC-1 and SDC-2`.

### N-04: Front matter loaded as tables (standard:16) · P2 · Resolved

**Was (R3):** `list_tables standard:16` returned 13 tables, including two table-of-contents fragments (pages 4–5, captions made of dot leaders) and the acronym list (page 10).
**Now (R4, R5):** 10 tables; front matter gone. Corpus table count went 614 → 611 → 610.

**Follow-up:** confirm the filter applies corpus-wide, not only to the re-run document. Check `list_tables` for another PDF entry (e.g. standard:15, standard:18) for TOC or acronym tables. This is part of TB-02.

### N-05: `open_warnings` dropped to 0 · P1 · Resolved

**Was (R4):** after a forced run of one document, `open_warnings` was 0 while all four underlying conditions still held.
**Now (R5):** after a full agent run, `open_warnings` is 3, matching the warning-severity items.

**Root cause likely still present:** see N-08. A partial run appears to rebuild warning state from only the documents it touched. The count recovered because the next run was a full run.

---

## Open: P0

### V-01: Wrong edition on board, masked by configuration · P0 · Open

**Repro**
```json
get_entry_metadata {"entry": "standard:20"}
get_warnings       {}
```

**Observed (R1, unchanged at R5)**
- Local file is `ASCE_SEI43-05.pdf` (2005 edition).
- `version.on_board = {"ASCE 43": {"edition": "19", "source": "configuration"}}`.
- Status `partially_staged`; warning `local_copy_unidentified` at **info** severity.
- Entry labelled "ASCE/SEI 43-19"; all tables and joins come from the 2005 text (87 of 96 pages OCR).

**Expected:** status `local_copy_outdated`, warning severity, "local copy is 43-05, latest is 43-19".

**Likely cause:** edition resolver falls back to config when text detection fails and ignores the filename.

**Suggested fix**
- Add a filename edition probe (`43-05`, `_1989`, `-2019`). Rank: text > filename > config.
- Filename/config disagreement → `edition_mismatch` at warning severity.
- Mark config-only editions as `assumed`, never as on-board fact.

**Acceptance:** standard:20 reports on-board edition 05, warning severity, status not `partially_staged`.

---

## Open: P1

### N-08: Warning history wiped by restart or forced run · P1 · Open

**Repro:** `get_warnings {}`

**Observed (R5):** every warning has `first_seen: 2026-10-21T18:01:26Z`, `last_seen` equal to it, `runs: 1`, `is_new: true`.

**Before (R1):**

| Warning | first_seen | runs |
|---|---|---|
| QME-1 `no_local_copy` | 2026-10-12T20:42:41Z | 14 |
| RA-S-1.4 `no_local_copy` | 2026-10-13T13:06:28Z | 13 |
| NQA-1 `local_copy_outdated` | 2026-10-13T13:24:26Z | 12 |
| ASCE 43 `local_copy_unidentified` | 2026-10-13T13:24:26Z | 12 |

**Contrast:** `document_errors.consecutive_errors` for standard:20 went 14 → 15, so the error store kept its state while the warning store did not.

**Impact**
- Long-standing problems look new; users can't tell chronic from fresh issues.
- Any alerting on `is_new` fires for everything after each restart.
- Probably the cause of N-05 and possibly N-11 and N-12.

**Likely cause:** the warning store is rebuilt from scratch (or keyed on something that changed, such as run ID) instead of merged with the previous state. A forced single-document run may replace the whole store with that run's findings.

**Suggested fix**
- Persist warnings keyed on `(kind, entry, identity)`.
- On each run: update `last_seen` and increment `runs` for matches; insert new ones with `is_new: true`; mark the rest `resolved` (keep history).
- A partial or forced run touches only the warnings for the entries it processed.

**Acceptance:** after a restart plus a forced one-document run, the four warnings above keep their original `first_seen` and their `runs` count increases.

### S-01: Absolute local paths exposed · P1 · Open

**Repro:** `get_entry_metadata {"entry": "standard:30"}` (also standard:20)

**Observed (R1):** `url` and `fetched_from` contain `file:///<local path>/kg_agent_home/local/standards/30/ASME_NQA-1_1989.pdf`, exposing the local directory layout.

**Expected:** a path relative to `kg_agent_home`, as `on_board_documents` already does (`local:ASME_NQA-1_1989.pdf`).

**Fix:** normalize local paths at the serialization boundary. Test: no response contains `file:///` or the agent-home absolute path.

### S-02: Session preferences shared under `web:default` · P1 · Open

**Repro**
```json
set_session_preferences   {"profile": "strict", "min_score": 0.8, "retrieval": "lexical"}
get_relationship_map      {"ref": "standard:16"}
reset_session_preferences {}
```

**Observed (R1):** works functionally, but `"session": "web:default"`. Concurrent clients without a `session_id` share one preference state; one user's `strict` setting changes another's results.

**Fix:** default the session to a per-connection identifier; document `session_id` as the override.

### X-01: Relation types built from raw text fragments · P1 · Open

**Repro:** `graph_summary {"ref": "standard:6"}` (also standard:16, standard:3)

**Observed (R1):** besides `CONTAINS`, `NEXT`, `MENTIONS`, `references`, `CITES`, `related_to`, there are hundreds of one-off labels:
- standard:6: `or` (338), `and` (151), `10` (106), `the` (96), `“` (68), `—` (62), `ADAMS_accession_No./web_link/_Federal_Register_citation` (121), `/30/2026`, a full Federal Register URL.
- standard:16: TOC dot leaders (`INTRODUCTION.....`) and table-cell text (`FFRDC_Facilities_Unm_Thres`, `Atmospheric_Pressure_Change` ×44).

**Note:** the standard:16 table-DB front-matter fix (N-04) does not clean the graph; retest after the graph is rebuilt.

**Likely cause:** the rule-based extractor uses the raw span between two entities as the predicate.

**Fix:** normalize predicates to a vocabulary (verb lemma + preposition); stop-list; reject digit-only, punctuation-only, URL and over-long labels; map failures to `related_to`; skip TOC pages and table cells as sources.

**Acceptance:** standard:6 has ≤ ~30 relation types; none match `^[\W\d]+$` or contain `http`.

### X-02: Plural identifier forms not parsed · P1 · Open

**Repro:** `search_graph {"ref": "standard:6", "query": "1.200"}`

**Observed (R1):** paragraph `6526ad48:p0225` ("…endorsed in RGs 1.200 and 1.247…") yields Entity `RGs 1.200` but no Identifier, so the combined graph lacks Part 53 → RG 1.200 (standard:26).

**Fix:** support plural prefixes (`RGs`, `NUREGs`, `§§`) and list continuations; emit one Identifier per member.

**Acceptance:** `entry:standard:6 —cites→ entry:standard:26` exists.

### X-03: Standard references typed as Entity/Value · P1 · Open

**Repro:** `search_graph {"ref": "standard:22", "query": "RA-S"}`

**Observed (R1):** paragraph `36c12dcd:p0197` ("ASME/ANS RA-S-1.4") yields Entities `ANS RA-S-1`, `ANS-RA-S-1` and Values `ANS-Ra-S-1.4-2013`, `ASME/ANS-Ra-S-1.4-2013`. No Identifier, no NEI 18-04 → standard:25 edge, no deduplication.

**Fix:** add ASME/ANS patterns (`(ASME/ANS[- ])?RA-S-\d+(\.\d+)?(-\d{4})?`) to the identifier recognizer, run before entity/value typing, canonicalize case and hyphens.

### X-04: `None` in identifier label · P1 · Open

**Repro:** `search_graph {"ref": "standard:6", "query": "RA-S-1.4"}`

**Observed (R1):** Identifier `6526ad48:ident:RA-S 1.4` has `label: "RA-S-1.4-None"`. Same response has a TableRow node with an `ent:` ID prefix.

**Fix:** `f"{base}-{edition}" if edition else base`; audit node-ID prefixes against node types.

### X-05: Identifier-to-entry resolution errors · P1 · Open

**Repro:** `get_dependents {"entry": "standard:22"}`

**Observed (R1)**
- Self-citation: `inferred_cites` includes report:1 via `NEI 18-04`; report:1's on-board document is NEI 18-04.
- Fan-out: `CFR 50` → standard:1 and standard:28; `CFR 50.69` → standard:3 and report:4.

**Fix:** resolve each identifier to one canonical standard (most specific; standards over reports); never resolve to an entry sharing the source document; link reports via `complies_with` only.

### X-06: Entry-level cite missing for "part 100" · P1 · Open

**Repro:** `graph_summary {"ref": "standard:3"}`, `get_dependents {"entry": "standard:3"}`

**Observed (R1):** 50.69 has a text relation `to_part_100` but no standard:3 → standard:5 edge, and no link to its parent Part 50.

**Fix:** have the combined-graph builder accept "part NNN" forms ("appendix A to part 100", "10 CFR part 100"); consider a `part_of` relation for section entries.

### TB-01: Footnote superscripts merged into cell values · P1 · Open

**Repro:** `get_table {"ref": "standard:16", "node_id": "d74d69e8:tbl0106"}`

**Observed (R1):** `NDC-24` (NDC-2 + footnote 4), `≥ PAC-13` (PAC-1 + footnote 3), headers `holds1`, `Worker2`. Values look valid but are wrong.

**Note (R4):** this table was reloaded and its row count changed from 8 to 7 (N-13). Cell values have not been rechecked.

**Fix:** split superscript runs by font size / baseline into a `footnotes` attribute; fallback domain filter for code families (`NDC-[1-3]`, `PAC-[1-3]`, `SDC-[1-5]`, `FDC-[1-5]`).

### TB-02: Non-tables ingested as tables · P1 · Partial

**Repro:** `get_relationship_map {"ref": "standard:20", "format": "mermaid"}`; `list_tables {"ref": "standard:16"}`

**Observed**
- R1: most of ASCE 43's ~300 "tables" are two-column OCR prose with sentence-fragment headers.
- R3: TOC and acronym list in standard:16.

**Progress (R4):** front matter removed for standard:16 (N-04).

**Remaining:** ASCE 43 OCR-prose tables not retested; other PDFs not checked for front matter.

**Fix:** table-validity classifier before ingestion: reject high mean cell length (> ~60 chars), no numeric cells, 2 columns of running text, sentence punctuation in headers.

### R-01: Reading order wrong on two-column pages · P1 · Open

**Repro:** `get_outline {"ref": "standard:30"}`

**Observed (R1):** `1 PURPOSE, 3 RESPONSIBILITY, 2 APPLICABILITY, …, 5, 10, 11, 6, 7, 12, 13, 8, 9, 14, 15, 17, 18, 16`.

**Fix:** per-page column detection (x-histogram of text blocks), column-major ordering; warn when numbered headings are non-monotonic.

---

## Open: P2

### T-02: Asset tools reject the 8-char document prefix · P2 · Open

*(Downgraded from P0 at R2; absorbs v1's A-03.)*

**Works (R2):**
```json
list_assets   {"ref": "57988ceda0881836", "kind": "table"}                     → 21 tables, all available
get_table_csv {"ref": "standard:15", "asset_id": "57988ceda0881836/tbl0224"}   → CSV, rows_total 1336
```

**Fails with misleading errors (R1):**
- `list_assets {"ref": "57988ced"}` → "no exported figures or tables … (the agent exports them when run with assets enabled)".
- `get_table_csv {"asset_id": "57988ced/tbl0224"}` → "asset … does not belong to 'standard:15'".
- `get_table_csv {"asset_id": "57988ced:tbl0224"}` → "use '<document_id>/<id>' from list_assets" (doesn't say 16 chars).

Graph node IDs (`57988ced:tbl0224`) only expose the 8-char prefix, so a user following the graph can't reach the asset. v1's hypothesis that the assets had been purged was wrong.

**Fix:** accept a graph Table node ID as `asset_id`; resolve unique short prefixes; on a miss, say "unknown document id '57988ced'; did you mean 57988ceda0881836?".

**Note:** `list_tables` (R3+) returns full 16-char `table_id`s, which helps users of the table-DB tools.

### TB-03: Repeated headers and OCR noise · P2 · Open

**Repro:** `get_table_csv {"ref": "standard:15", "asset_id": "57988ceda0881836/tbl0224", "max_rows": 40}`

**Observed (R1, R2):** NE-STD-1027 Table 1-1 repeats its two header rows on every page with garbled text (`Rdilid`, `aonuce`, `mtng Pathway(B)`, `y 3(A) Liii`).

**Fix:** merge multi-page tables by header similarity; drop repeated header rows.

### TB-04: Junk joins · P2 · Open

**Observed (R1):** standard:20 relationship map has `t_tbl0048 ||--|| t_tbl0051 : "g_bagchi = g_bagchi"` (committee member lists joined on a name).

**Fix:** exclude tables flagged non-tabular (TB-02); require a key column with non-name semantics.

**Note (R5):** the full run now reports `joins: 16` across 4 documents (was 0). Review those 16 for quality.

### TB-05: Join evaluation never ran for NE-STD-1020 · P2 · Partial

**Repro:** `get_join_evaluation {"ref": "standard:16", "format": "markdown"}`

**Observed (R1):** "Suggester None · judge None · LLM stage: not run", 0 candidates, though the document has 13 tables (now 10).

**Progress (R4, R5):** the forced run reported `documents_with_joins: 1`; the full run reports 16 joins in 4 documents. Not confirmed whether standard:16 is among them.

**Next:** rerun `get_join_evaluation` for standard:16; surface "stage skipped: <reason>" when a stage doesn't run.

### A-01: `get_table` has no pagination · P2 · Open

**Observed (R1):** `get_table` for NE-STD-1027 Table 1-1 returned ~166K characters and exceeded the client limit. `get_table_csv` already supports `max_rows` with `rows_total` / `truncated`.

**Fix:** add `offset`/`limit` (default ~200 rows) and `total_rows` to `get_table`.

### A-02: Node-ID forms differ between tools · P2 · Open

**Repro:** `get_subgraph {"ref": "combined", "center": "standard:3"}` → `node 'standard:3' not in combined`. Works with `entry:standard:3`.

**Fix:** accept both, or reply "did you mean entry:standard:3?".

### A-04: Hyphenated multi-term search returns nothing · P2 · Open

**Repro:** `search_graph {"ref": "standard:14", "query": "PDSA DOE-STD"}` → no matches; "PDSA" alone and "NSDA" alone return results.

**Fix:** check hyphen tokenization (split on `-` or treat as a phrase).

### A-06: Outline titles truncated · P2 · Open

**Observed (R1):** titles cut at 80 chars, e.g. "§ 53.220 Safety criteria for licensing-basis events other than design-basis acci".

**Fix:** return full titles or add `truncated: true`.

### D-01: NQA-1 on-board edition · P2 · Open (data/config)

**Observed (R1, R5):** local copy NQA-1-1989; latest 2026; RG 1.28 Rev. 6 endorses only 2017, 2019, 2022. The `local_copy_outdated` warning fires correctly.

**Suggestion:** also warn when the on-board edition isn't one the endorsing reg guide accepts.

### D-02: Duplicate Part 53 definitions · P2 · Open

**Observed (R1):** standard:6 contains the FR final rule (`6526ad48`) and eCFR (`43929258`); `search_graph` returns each definition twice.

**Fix:** tag document role (regulatory text vs preamble), rank regulatory text first, or deduplicate identical paragraphs.

### N-06: Wind table caption fragment, no label · P2 · Open

**Repro:** `list_tables {"ref": "standard:16"}`

**Observed (R3–R5):** `d74d69e8972e8d3c/tbl0223` (page 24) has `caption: "WDC-3, WDC-4, and WDC-5 SSCs"` and `table_label: ""`. The caption is the tail of a longer sentence, not a table title.

**Fix:** search further up (or across a page break) for a "Table N-M" caption; flag tables with no label.

### N-07: Section path missing a space · P2 · Open

**Observed (R3–R5):** `section_path` for Table 2-1 contains `2.3.3Criteria and Guidance for Establishing…`.

**Fix:** apply the same spacing normalization used for captions (N-03) to headings and section paths.

### N-09: Same failure listed twice in `run_errors` · P2 · Open

**Observed (R5):**
```
"tracker https://ascelibrary.org/doi/book/10.1061/9780784415405: HTTP 403 Forbidden; browser fallback failed: HTTP 403 "
"document https://ascelibrary.org/doi/book/10.1061/9780784415405: HTTP 403 Forbidden; browser fallback failed: HTTP 403  (STALE: repeatedly unreachable)"
```

Not wrong (tracker and document fetch both failed), but it inflates `errors: 2` for one root cause. Messages also have a trailing space.

**Fix:** group by URL with a `stages: ["tracker", "document"]` field, or keep both but count unique URLs.

### N-10: `open_warnings` counting rule undocumented · P2 · Open

**Observed (R5):** `get_overview.open_warnings = 3`; `get_warnings` returns 7 items (3 warning, 4 info). The 3 matches warning severity only.

**Fix:** document the rule in the tool description, or return `open_warnings_by_severity: {"warning": 3, "info": 4}`.

---

## Needs verification

### N-11: QME-1 new-edition warning disappeared

**R1:** `new_edition_for_purchase` for standard:21 ("QME-1-2025 (was 2023)"), tracker `asmeqme2023`.
**R5:** that warning is absent; a `store_page_changed` warning now references `asmeqme2025`.

**Question for owner:** was the tracker URL updated deliberately (making the warning correctly resolved), or did the state reset (N-08) drop it?

### N-12: `store_page_changed` warnings may be spurious

**R5:** three new info warnings for standard:20 (`ascesei432019`), standard:21 (`asmeqme2025`), standard:30 (`asmenqa2026`), all with `first_seen` = run time.

**Question for owner:** did these store pages really change, or were their stored fingerprints cleared with the warning state (N-08), so the first comparison reported a change? Check stored hashes before vs after.

### N-13: Table 2-1 row count changed (8 → 7)

**R3:** `n_rows: 8`. **R4/R5:** `n_rows: 7`, `loaded_at: 2026-10-21T17:19:13Z`.

**Question for owner:** was a header row deliberately dropped, or was a data row lost? Check with `get_table` that all six NDC rows (three FFRDC, three privately funded) remain, and recheck TB-01 at the same time.

---

## What passed

Keep these as regression tests so fixes don't break them.

- **`get_overview`, `list_entries`:** accurate counts, version statuses, `table_database` block (R3+), itemized errors (R5).
- **`get_outline`:** correct for eCFR HTML (Part 53 Subparts A–F) and single-column PDFs (DOE-STD-1271).
- **`search_graph`:** correct "safety-related" definitions from Part 50 §50.2 and Part 53 §53.020.
- **`get_dependents`:** report:8 lists 11 DOE dependencies; curated/inferred split and `if_this_changes_reports_affected` work.
- **`get_subgraph`, `export_graph` (combined):** node-link JSON, 45 nodes / 252 edges, `basis` and `confidence` per edge.
- **Provenance:** every graph response carries `version_status`, `warnings`, `caveats`, disclaimers; paywall and OCR caveats appear wherever they apply.
- **`get_warnings`, `get_disclaimers`:** correct kinds for missing copies (QME-1, RA-S-1.4) and outdated copy (NQA-1).
- **Empty entries (report:5):** "No documents are configured for this entry."
- **`get_table` on a clean PDF:** NE-STD-1020 Table 5-1 exact.
- **`list_assets`, `get_table_csv` with full 16-char ID:** 21 tables; CSV with `rows_total`, `truncated`, `max_rows`.
- **`list_tables` (R5):** 10 tables for standard:16 with correct labels, entry name, section path, source URL, page, and a table-extraction disclaimer.
- **Session preferences:** set, apply, read, reset all work.
- **`document_errors` counter:** persisted across restart (14 → 15).

## Retest checklist for the next round

1. Table-DB tools not yet exercised since T-01's fix: `search_tables`, `lookup_values`, `query_table`, `describe_table`, `explain_units`, `join_tables`.
2. `get_warnings` after another restart and a forced single-document run: `first_seen` and `runs` preserved (N-08).
3. `get_entry_metadata standard:20`: edition and severity (V-01).
4. `get_table standard:16 d74d69e8:tbl0106`: footnotes split and all six NDC rows present (TB-01, N-13).
5. `list_tables` for standard:15 and standard:18: no TOC or acronym tables (TB-02).
6. `get_join_evaluation standard:16`: stages ran (TB-05); review the 16 new joins (TB-04).
7. `get_entry_metadata standard:30`: no `file:///` paths (S-01).
8. `graph_summary standard:6` after a rebuild: relation-type count (X-01).

## Suggested regression tests

1. Table-DB connectivity as `cogent_ro`; `table_database.reachable` true (T-01).
2. Filename edition `X-05.pdf` with config edition 19 → `edition_mismatch`, warning severity (V-01).
3. Warning store survives restart and partial run: `first_seen` unchanged, `runs` incremented (N-08).
4. No response contains `file:///` or the agent-home absolute path (S-01).
5. Two connections get distinct default sessions (S-02).
6. Relation-type count and pattern assertions on standard:6 (X-01).
7. "RGs 1.200 and 1.247" → two Identifiers (X-02); "ASME/ANS RA-S-1.4" → one canonical Identifier (X-03).
8. No identifier label contains `None` (X-04).
9. No entry cites itself through a shared document (X-05).
10. Footnote fixture: "NDC-2" + superscript "4" → value `NDC-2`, footnote `4` (TB-01).
11. Numbered headings in `get_outline` are monotonic for a two-column fixture (R-01).
12. Asset tools accept a graph Table node ID and a unique 8-char prefix (T-02).
13. `table_label` keeps subnumbers; `doc_label` is the entry name; captions normalized (N-01–N-03).
14. No table whose caption is a TOC line or whose rows are an acronym list (N-04, TB-02).
