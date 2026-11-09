# cogent MCP server: test report

**Server under test:** `cogent-test20261021` (MCP connector)
**Test date:** 2026-10-21, ~00:50–01:00 ET
**Agent run at test time:** `20261021T041754Z` (last run 2026-10-21T04:21:54Z; last forced run `20261020T033106Z`)
**Corpus:** 45 entries (30 standards, 15 reports), all with graphs
**Tester:** Claude (claude.ai) exercising the server through its MCP tools

## How to use this report

Each issue has an ID, a severity, an exact reproduction call, the observed and expected behavior, and the evidence. Root causes are **hypotheses** drawn from tool output only. The tester had no access to the source code. Confirm each one against the code before changing anything.

Suggested workflow for Claude Code:
1. Read the "Issue index" table.
2. For each issue, locate the responsible module, confirm the root cause, then propose a fix and a regression test.
3. Work in severity order: P0 issues first.

Severity scale:
- **P0:** a tool family is unusable, or the server returns wrong data with no warning.
- **P1:** a wrong or missing result that a user is likely to hit.
- **P2:** a quality or ergonomics issue.

---

## Issue index

| ID | Sev | Area | Summary |
|---|---|---|---|
| T-01 | P0 | Table DB | Every table-DB tool fails: `password authentication failed for user "cogent_ro"` |
| V-01 | P0 | Versioning | ASCE 43 local copy is the 2005 edition, but config asserts 2019; status hides it |
| S-01 | P1 | Security | Absolute local filesystem paths (incl. home dir) exposed in metadata |
| S-02 | P1 | Sessions | Session preferences keyed on shared `web:default` (cross-user leakage risk) |
| X-01 | P1 | Extraction | Relation labels built from raw text fragments (hundreds of junk relation types) |
| X-02 | P1 | Extraction | Identifier parser misses plural forms ("RGs 1.200 and 1.247") |
| X-03 | P1 | Extraction | Standard references typed as Entity/Value instead of Identifier, so no cross-entry edge |
| X-04 | P1 | Extraction | `None` leaks into identifier label (`RA-S-1.4-None`) |
| X-05 | P1 | Combined graph | Identifier-to-entry resolution yields self-citation and fan-out to multiple entries |
| X-06 | P1 | Combined graph | 50.69 → Part 100 citation present in text relations but missing at entry level |
| TB-01 | P1 | Tables | Footnote superscripts merged into cell values (`NDC-24`, `PAC-13`) |
| TB-02 | P1 | Tables | Two-column OCR prose detected as tables (~80% of ASCE 43 "tables") |
| T-02 | P2 | Assets | *(Revised, was P0)* Asset tools need the full 16-char document ID, but graph node IDs expose only an 8-char prefix; errors don't say so |
| TB-03 | P2 | Tables | Repeated page headers kept as data rows; OCR garbage in headers |
| TB-04 | P2 | Tables | Junk joins (e.g., committee member lists joined on a person's name) |
| TB-05 | P2 | Tables | Join evaluation never ran for NE-STD-1020 (0 candidates, LLM/judge "not run") |
| R-01 | P1 | Reading order | Two-column layout yields out-of-order headings (NQA-1: 3 before 2, 10/11 before 6) |
| A-01 | P2 | API | `get_table` has no pagination/limit; returned ~166K chars |
| A-02 | P2 | API | Node-ID format differs between tools (`standard:3` vs `entry:standard:3`), unhelpful error |
| A-04 | P2 | Search | Multi-term search with a hyphenated term returns nothing |
| A-05 | P2 | Observability | Run errors are counted in `get_overview` but not itemized anywhere except per-entry metadata |
| A-06 | P2 | Outline | Outline titles truncated at 80 chars with no indication |
| D-01 | P2 | Data/config | NQA-1 on-board copy is the 1989 edition; RG 1.28 Rev. 6 endorses only 2017/2019/2022 |
| D-02 | P2 | Data | Part 53 graph includes the Federal Register rule as well as eCFR, so definitions appear twice |

---

## P0 issues

### T-01: Table database unreachable for the read-only user

**Repro**
```json
search_tables {"question": "seismic design category limit state", "ref": "standard:16", "k": 5}
list_tables   {"ref": "standard:16", "query": "seismic design category"}
explain_units {"table_id": "57988ced:tbl0224"}
```
**Observed:** all three fail with
```
table database not reachable: OperationalError: connection failed: connection to server at
"127.0.0.1", port 5432 failed: FATAL:  password authentication failed for user "cogent_ro"
```
`query_table` and `join_tables` share the same path, so they fail the same way. Not called separately.

**Contrast:** `get_overview` reports a healthy table-DB load in the last run (`sources: 624, unchanged: 614, skipped: 10, errors: []`). The writer path works; only the read-only role fails.

**Likely cause:** the `cogent_ro` password in the MCP server's environment or config doesn't match Postgres. Possibilities: the role was never created, the password was rotated, the env var is missing, or `pg_hba.conf` uses an auth method the client doesn't support (scram vs md5).

**Suggested fix**
- Verify the role exists and has `CONNECT` plus `SELECT` grants.
- Align the credential source between the agent and the MCP server.
- Add a startup health check that fails loudly, and a `tabledb` status field in `get_overview`.

**Acceptance:** all five table-DB tools return results for `standard:16`, and the overview shows tabledb reachable.

### V-01: Wrong edition on board, masked by configuration

**Repro**
```json
get_entry_metadata {"entry": "standard:20"}
get_warnings       {}
```
**Observed**
- The local file is `ASCE_SEI43-05.pdf`, i.e. the 2005 edition.
- `version.on_board` = `{"ASCE 43": {"edition": "19", "source": "configuration"}}`.
- Status is `partially_staged`, with only an **info**-severity warning: "cannot tell which edition of ASCE 43 the local copy (ASCE_SEI43-05.pdf) is; expected 19".
- The entry is labelled "ASCE/SEI 43-19" throughout, and all its tables and joins come from the 2005 text.
- The text-based edition probe found nothing (`editions_stated_in_text: {}`); 87 of 96 pages are OCR.

**Expected:** status `local_copy_outdated`, warning-level, "local copy is 43-05, latest is 43-19".

**Likely cause:** the edition resolver trusts the configured edition when it can't detect the edition from the text, and ignores the filename.

**Suggested fix**
- Add a filename-based edition probe, e.g. patterns like `43-05`, `_1989`, `-2019`. Rank it above configuration and below text.
- When the filename edition disagrees with the configured edition, raise `local_copy_outdated` or `edition_mismatch` at warning severity.
- Never label `on_board` from configuration alone. Mark it `assumed`.

**Acceptance:** standard:20 reports on-board edition 05, status not `partially_staged`, and a warning-level message.

---

## P1 issues

### S-01: Local absolute paths exposed

**Repro:** `get_entry_metadata {"entry": "standard:30"}` (also `standard:20`)

**Observed:** the `url` and `fetched_from` fields contain the full local path, e.g. `file:///<local path>/kg_agent_home/local/standards/30/ASME_NQA-1_1989.pdf`. These expose the local directory layout.

**Expected:** a path relative to `kg_agent_home` (e.g. `local:standards/30/ASME_NQA-1_1989.pdf`). `on_board_documents` already does this (`local:ASME_NQA-1_1989.pdf`).

**Suggested fix:** normalize every local path to be relative to the agent home at the serialization boundary. Add a test asserting that no MCP response contains `file:///` or the home directory.

### S-02: Session preferences shared under `web:default`

**Repro**
```json
set_session_preferences   {"profile": "strict", "min_score": 0.8, "retrieval": "lexical"}
get_relationship_map      {"ref": "standard:16"}
reset_session_preferences {}
```
**Observed:** the functionality works (strict preferences applied, then reset), but the session is `"session": "web:default"`.

**Risk:** if more than one client or conversation connects without passing `session_id`, they all share one preference state. One user's `strict` setting silently changes another user's join and unit results.

**Suggested fix:** derive the default session from a per-connection identifier (MCP session or transport connection ID), not a constant. Document `session_id` as the override.

### X-01: Relation types built from raw text fragments

**Repro:** `graph_summary {"ref": "standard:6"}` (also `standard:16`, `standard:3`)

**Observed:** besides the expected relations (`CONTAINS`, `NEXT`, `MENTIONS`, `references`, `CITES`, `related_to`), there are several hundred one-off labels. Examples from standard:6:
- `or` (338), `and` (151), `10` (106), `the` (96), `“` (68), `—` (62)
- `ADAMS_accession_No./web_link/_Federal_Register_citation` (121)
- `-15881_(186_pages`, `/30/2026`, `https://www.federalregister.gov/d/2026-06048`

Standard:16 has table-of-contents dot leaders as labels (`INTRODUCTION.....…`) and table-cell text (`FFRDC_Facilities_Unm_Thres`, `Atmospheric_Pressure_Change`).

**Likely cause:** the rule-based relation extractor uses the raw text span between two entity mentions as the predicate, with no filtering.

**Suggested fix**
- Normalize predicates against a vocabulary (verb lemma plus preposition), with a stop-list and minimum-quality rules: must contain a verb, no digits-only labels, no URLs, no punctuation-only labels, length cap.
- Map anything that fails these rules to `related_to`.
- Skip TOC pages and table cells as relation sources.

**Acceptance:** standard:6 has no more than ~30 relation types, none matching `^[\W\d]+$` or containing `http`.

### X-02: Plural identifier forms not parsed

**Repro**
```json
search_graph {"ref": "standard:6", "query": "1.200"}
```
**Observed:** paragraph `6526ad48:p0225` reads "…endorsed in RGs 1.200 and 1.247…". It produced an Entity `RGs 1.200` but no Identifier, so the combined graph has no Part 53 → RG 1.200 (standard:26) edge.

**Suggested fix:**
- Extend the identifier grammar for plural prefixes (`RGs`, `NUREGs`, `§§`) and list continuations ("1.200 and 1.247", "50.34, 50.36, and 50.36a").
- Emit one Identifier per member.

**Acceptance:** the combined graph contains `entry:standard:6 —cites→ entry:standard:26`.

### X-03: Standard references typed as Entity/Value, not Identifier

**Repro**
```json
search_graph {"ref": "standard:22", "query": "RA-S"}
```
**Observed:**
- Paragraph `36c12dcd:p0197` mentions "ASME/ANS RA-S-1.4". The nodes produced are Entities `ANS RA-S-1` and `ANS-RA-S-1`, plus Values `ANS-Ra-S-1.4-2013` and `ASME/ANS-Ra-S-1.4-2013`. None of them is an Identifier.
- `get_dependents standard:22` therefore shows no NEI 18-04 → standard:25 edge.
- The four variants are not deduplicated.

**Suggested fix:**
- Add ASME/ANS standard patterns (`RA-S-\d+(\.\d+)?(-\d{4})?`, with an optional `ASME/ANS` prefix) to the identifier recognizer.
- Do this before entity/value typing, so a match wins over the Value typing.
- Canonicalize case and hyphenation for deduplication.

### X-04: `None` in identifier label

**Repro:** `search_graph {"ref": "standard:6", "query": "RA-S-1.4"}`

**Observed:** Identifier `6526ad48:ident:RA-S 1.4` has `label: "RA-S-1.4-None"`. The same response includes a TableRow node with an `ent:` id prefix, which suggests ID-namespace or type mixing.

**Suggested fix:** format labels with an optional edition: `f"{base}-{edition}" if edition else base`. Audit node-ID prefixes against node types.

### X-05: Identifier-to-entry resolution errors (self-cite, fan-out)

**Repro:** `get_dependents {"entry": "standard:22"}`

**Observed**
- **Self-citation:** `inferred_cites` contains `report:1` via identifier `NEI 18-04`. report:1's on-board document *is* NEI 18-04, so the entry cites itself through a proxy.
- **Fan-out:**
  - `CFR 50` resolves to both `standard:1` (Part 50) and `standard:28` (Appendix B + RG 1.28).
  - `CFR 50.69` resolves to both `standard:3` and `report:4`.

**Suggested fix**
- Resolve each identifier to exactly one canonical **standard** entry: the most specific match, preferring standards over reports.
- Never resolve an entry's identifier to an entry that shares its own source documents.
- Show reports as dependents through `complies_with`, not as cite targets of their own source standard.

### X-06: Entry-level cites missing despite text-level relation

**Repro:** `graph_summary {"ref": "standard:3"}` and `get_dependents {"entry": "standard:3"}`

**Observed:** standard:3 (50.69) has a text relation `to_part_100`, but `inferred_cites` is empty. There is no standard:3 → standard:5 edge. 50.69 also has no edge to its parent Part 50; consider a `part_of` relation.

**Suggested fix:** make the combined-graph builder consume Identifier nodes for "part NNN" forms (e.g. "appendix A to part 100", "10 CFR part 100"), not only "CFR NNN.NN".

### TB-01: Footnote superscripts merged into cell values

**Repro:** `get_table {"ref": "standard:16", "node_id": "d74d69e8:tbl0106"}`

**Observed:** the Category column reads `NDC-24`, which should be `NDC-2` plus footnote 4. Other cells read `≥ PAC-13`, which should be PAC-1 plus footnote 3. Headers have footnote digits fused too: `holds1`, `Worker2`.

**Impact:** values look valid but are wrong (there is no NDC-24 or PAC-13), so this is a silent correctness error.

**Suggested fix:** in PDF table extraction, use font size and baseline offset to split superscript runs into a `footnotes` attribute. A cheaper fallback: a domain post-filter for known code families (`NDC-[1-3]`, `PAC-[1-3]`, `SDC-[1-5]`, `FDC-[1-5]`) that flags trailing extra digits.

### TB-02: Two-column OCR prose detected as tables

**Repro:** `get_relationship_map {"ref": "standard:20", "format": "mermaid"}`

**Observed:** most of the ~300 "tables" have two columns whose headers are sentence fragments (e.g. `c_6_2_1general`, `the_uncertaintiesin_the_estimation_of_ha`). They are prose split into the two page columns, mostly from OCR'd pages. 87 of 96 pages are OCR.

**Suggested fix:** add a table-validity classifier before ingestion. Reject candidates where:
- the mean cell length is above about 60 characters,
- there are no numeric cells,
- there are exactly 2 columns of running text, or
- the header cells contain sentence punctuation.

### R-01: Reading order wrong on two-column pages

**Repro:** `get_outline {"ref": "standard:30"}`

**Observed:** the headings come out as `1 PURPOSE, 3 RESPONSIBILITY, 2 APPLICABILITY, …, 5, 10 INSPECTION, 11 TEST CONTROL, 6 DOCUMENT CONTROL, 7, 12, 13, 8, 9, 14, 15, 17, 18, 16`.

**Suggested fix:** detect columns per page (x-histogram of text blocks) and order column-major. When headings are numbered, validate the order against the numbering sequence and warn on violations.

---

## P2 issues

**TB-03: Repeated headers and OCR noise in `get_table` rows.** NE-STD-1027 Table 1-1 (`57988ced:tbl0224`) repeats the two header rows on every page, with garbled text such as `Rdilid`, `aonuce`, `mtng Pathway(B)`, `y 3(A) Liii`.
- Fix: merge multi-page tables by header similarity and drop repeated header rows.

**TB-04: Junk joins.** In the standard:20 relationship map, `t_tbl0048 ||--|| t_tbl0051 : "g_bagchi = g_bagchi"` joins committee-member lists on a person's name.
- Fix: exclude tables flagged non-tabular by the TB-02 classifier, and require a key column with non-name semantics.

**TB-05: Join evaluation never ran for NE-STD-1020.** `get_join_evaluation {"ref": "standard:16"}` returns `Suggester None · judge None · LLM stage: not run`, with 0 candidates, although the document has 13 tables. The document is also labelled just "PDF".
- Fix: find out why the deterministic stage produced no candidates, and surface "stage skipped: <reason>".

**A-01: `get_table` has no paging.** It returned ~166K characters for one table and exceeded the client's limit.
- Fix: add `offset`/`limit` (default ~200 rows) and a `total_rows` field.

**A-02: Inconsistent node IDs.** `get_subgraph {"ref": "combined", "center": "standard:3"}` returns `node 'standard:3' not in combined`; the working form is `entry:standard:3`.
- Fix: accept both forms, or have the error say "did you mean entry:standard:3?".

**T-02 (revised; was P0, absorbs former A-03): asset tools reject the 8-char document prefix that graph node IDs expose.**
Retest at 02:30 ET showed the assets were never missing. With the full 16-hex document ID both tools work:
```json
list_assets   {"ref": "57988ceda0881836", "kind": "table"}                      → 21 tables, all "available": true
get_table_csv {"ref": "standard:15", "asset_id": "57988ceda0881836/tbl0224"}   → CSV, rows_total 1336, truncated true
```
With the 8-char prefix that appears in every graph node ID (`57988ced:tbl0224`), the calls fail with misleading errors:
- `list_assets {"ref": "57988ced"}` → "no exported figures or tables … (the agent exports them when run with assets enabled)". This wrongly implies the export never ran.
- `get_table_csv {"asset_id": "57988ced/tbl0224"}` → "asset … does not belong to 'standard:15'". This is a lookup miss reported as an ownership error.
- `get_table_csv {"asset_id": "57988ced:tbl0224"}` → "use '<document_id>/<id>' from list_assets", without saying the document ID must be the full 16 characters.

The original P0 hypothesis (assets purged by the forced run) was wrong; `assets_purged_files: 40` was unrelated or already re-exported.

Fix:
- Accept a graph Table node ID (`57988ced:tbl0224`) directly as `asset_id` and resolve it internally.
- Resolve unique short document prefixes to the full ID (as git does), or include the full document ID in graph node IDs or Table node attributes.
- When a lookup misses, say "unknown document id '57988ced'; did you mean 57988ceda0881836?" instead of the "assets not exported" or ownership messages.

Acceptance: both the 8-char-prefix forms above return the same results as the full-ID forms.

**A-04: Hyphenated multi-term search returns nothing.** `search_graph {"ref": "standard:14", "query": "PDSA DOE-STD"}` returns no matches, although "PDSA" alone and "NSDA" alone both return results.
- Fix: check how hyphens are tokenized (split on `-`, or match the term as a phrase).

**A-05: Run errors are not itemized.** `get_overview` reports `errors: 2, docs_error: 1` without details. One of them is only visible through `get_entry_metadata standard:20` (ascelibrary.org: `HTTP 403 Forbidden; browser fallback failed`, 14 consecutive errors).
- Fix: add an `errors[]` list (entry, document, message, count) to `get_overview`, or a `get_run_errors` tool.

**A-06: Outline titles truncated.** Titles are cut at 80 characters (e.g. "§ 53.220 Safety criteria for licensing-basis events other than design-basis acci").
- Fix: return the full title, or add a `truncated: true` flag.

**D-01: NQA-1 on-board edition (data/config, not code).** The local copy is NQA-1-1989, the latest is 2026, and RG 1.28 Rev. 6 endorses only the 2017, 2019 and 2022 editions. The warning fires correctly. Consider also warning when the on-board edition is not one the endorsing reg guide accepts.

**D-02: Duplicate Part 53 definitions.** The Part 53 graph includes the Federal Register final rule (`6526ad48`) as well as the eCFR text (`43929258`), so `search_graph` returns each definition twice.
- Fix: tag the document role (regulatory text vs preamble) and rank regulatory text first, or deduplicate identical paragraphs.

---

## What passed

These worked correctly and should be covered by regression tests so fixes don't break them:

- **`get_overview`, `list_entries`:** accurate counts, version statuses and warning counts.
- **`get_outline`:** correct for eCFR HTML (Part 53 Subparts A–F) and single-column PDFs (DOE-STD-1271).
- **`search_graph`:** correct definitions of "safety-related" from Part 50 §50.2 and Part 53 §53.020.
- **`get_dependents`:**
  - report:8 correctly lists 11 DOE dependencies.
  - The curated/inferred split and `if_this_changes_reports_affected` work as designed.
- **`get_subgraph`, `export_graph` on `combined`:** correct node-link JSON (45 nodes, 252 edges, with `basis` and `confidence` per edge).
- **Provenance:** every response carries `version_status`, `warnings`, `caveats` and disclaimers. Paywalled and stale caveats appear wherever they apply.
- **`get_warnings`, `get_disclaimers`:** correct for missing local copies (QME-1, RA-S-1.4) and an outdated local copy (NQA-1).
- **Empty entries (e.g. report:5):** graceful, with the clear message "No documents are configured for this entry."
- **`get_table` on a clean PDF:** NE-STD-1020 Table 5-1 extracted perfectly (FDC-1…5 return periods).
- **`list_assets`, `get_table_csv` with the full 16-char document ID:** 21 tables listed for `57988ceda0881836`; `tbl0224` returned as CSV with `rows_total` and `truncated` fields (retest 02:30 ET). Note `get_table_csv` paginates via `max_rows`, which `get_table` lacks (A-01).
- **Session preferences:** set, apply, read and reset all work (strict profile → 0 joins; reset → balanced).

## Suggested regression tests

1. Table-DB connectivity test using the `cogent_ro` credentials (T-01).
2. `get_table_csv` and `list_assets` accept a graph Table node ID and a unique 8-char document prefix, and return the same result as the full 16-char ID (T-02).
3. A fixture PDF named `X-05.pdf` with config edition 19 must give `local_copy_outdated` (V-01).
4. No response body contains `file:///` or the absolute agent-home path (S-01).
5. Two distinct connections get distinct default sessions (S-02).
6. Relation-type count and pattern assertions on standard:6 (X-01).
7. The identifier parser yields two Identifiers from "RGs 1.200 and 1.247" (X-02) and one canonical Identifier from "ASME/ANS RA-S-1.4" (X-03).
8. An identifier label never contains `None` (X-04).
9. No entry cites itself through a shared-document proxy (X-05).
10. Footnote fixture: a cell rendered "NDC-2" with superscript "4" yields value `NDC-2`, footnote `4` (TB-01).
11. Heading numbers in `get_outline` are monotonic for a two-column fixture (R-01).
