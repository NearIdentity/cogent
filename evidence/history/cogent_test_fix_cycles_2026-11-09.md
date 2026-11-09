# cogent: test–fix cycle history (to 2026-11-09)

**Prepared:** 2026-11-09, by Claude in the test/review session listed below

**Repository:** NearIdentity/cogent, `main` at `0394a9c` (PR #11 merged 2026-11-09 00:08 ET)

**Time zone:** all times are US Eastern. That is EDT (UTC−4) up to 2026-11-01 and EST (UTC−5) after.

**Where the facts come from:**
- the session transcript (UTC timestamps, converted to ET);
- the git commit and merge times on `main` and on the PR refs;
- the reports written during the cycles, which are listed at the end.

Root causes in the test reports were hypotheses drawn from tool output. Only PR #9 and PR #10 were reviewed against source code.

## Session and identity record (for audit)

| Role | Identifier | Covers |
|---|---|---|
| Client-side testing and PR reviews (this session) | Claude session `https://claude.ai/code/session_01McNHRJuh33PMHsyfvwxziD`; local transcript id `c25ca343-19f5-517f-a30c-e7483eeb6bb9`; model `claude-opus-5-5` | Every test round R1–R6, reports v1/v2, the R6 fix list, and the reviews of PR #9 and PR #10 (2026-10-21 → 2026-11-09) |
| Documentation update and evidence report | Claude session `https://claude.ai/code/session_017SgWjQ2rpRD4UwSvsT6f3Y` (Claude Sonnet 5.5, from the `Claude-Session` trailers of `d602091` and `c291372`) | PR #11 (README/docs alignment, `CHANGELOG.md`); branch `claude/pdf-html-knowledge-graph-773a5d` (evidence E1–E17, against `1f237f0`) |
| Fix commits | Git author `NearIdentity`, no `Claude-Session` trailer | Every fix commit in PR #6, #9 and #10. Whether a Claude Code session wrote them, and which one, cannot be told from the repository. |
| Merges | Repository owner's GitHub account | PR merges #2–#11 |
| MCP servers under test | `cogent-test20261021` (rounds R1–R5 and the workflow round); `cogent-test20261026` (R6) | |

This session did not push code or comment on any PR. Its only push is this `evidence/history/` folder, on branch `claude/pdf-html-knowledge-graph-773a5d` (2026-11-09).

## Summary

| Cycle | Dates | Trigger | Fix vehicle | Verification | Outcome |
|---|---|---|---|---|---|
| 0 | 10-12 → 10-19 | Initial build | Initial commit, PR #2, #4, #5 | None by this session | Baseline |
| 1 | 10-20 → 10-21 | Table DB + MCP table tools (PR #6) | `2901187`, `dc6e56e`, `33c0ed1`, `fe12f9a`; PR #6 merged 10-21 14:12 | Rounds R1–R5, live through the connector | 37 issues logged (report v2); 7 resolved in-cycle |
| 2 | 10-21 16:10–16:30 | Workflow tests, 10 scenarios | Same build as the end of cycle 1 | Live | 9 more resolved or mitigated; new TB-06 and a units error |
| 3 | 10-24 → 10-26 | Scale-out and observability (not test-driven) | PR #7, PR #8 | Not tested by this session | New server `cogent-test20261026` |
| 4 | 10-26 22:10 → 10-27 00:30 | Round R6 | PR #9 (`a434240`, merged 10-27 00:17) | Code review and test suite (PG16 + pgvector) | 15-item fix list R6-01…15; PR #9 covered 13; 4 must-fix review items |
| 5 | 10-27 01:41 → 02:56 | PR #9 review | PR #10 (`624271c`, `0659c5c`; merged as `1f237f0` at 02:56) | Code review, edge-case probes, full suite (332 pass) | All code findings fixed; D1 deferred by the owner; one residual issue (N1b) |
| 6 | 10-27 10:12 | Post-merge live check | — | Connector failed (HTTP 502) | Live verification of PR #9/#10 is **outstanding** |
| 7 | 11-09 | Documentation | PR #11 (`d602091`, merged 00:08) | Not reviewed by this session | CHANGELOG reconstructed; docs aligned |

## Cycle detail

### Cycle 0: baseline (2026-10-12 → 10-19)

- Initial commit `c3c26a8` (10-12 20:01): KG staging agent.
- `kg_daemon.py` (10-13).
- MCP server with OAuth (PR #2, merged 10-19 13:49).
- Figure `.bin` fix (PR #4, 10-19).
- Forced refresh (PR #5, 10-19 23:55).

This session did no client testing in this cycle.

### Cycle 1: table database and first MCP client rounds (2026-10-20 → 10-21)

**Code in:**
- `64f8cc3` (10-20 18:07): PGVector table DB and MCP table services.
- `2901187` (10-21 00:49): runtime fix.

**Issue identification**

| Round | When (ET) | Server state | Steps | Found |
|---|---|---|---|---|
| R1 | 10-21 00:50–01:00 | Agent run `20261021T041754Z` | Full sweep: overview, entries, outline, search, neighbours, subgraph, export, all table tools, edge cases | 25 issues, P0–P2. Report v1 `cogent_mcp_test_report.md` (01:04), with 11 suggested regression tests |
| R2 | 10-21 02:30 | Unchanged | `list_assets` / `get_table_csv` with the full 16-char document id | Assets were never missing. T-02 downgraded P0→P2; A-03 merged into it |

**Fixes:**
- `dc6e56e` (02:05) and `c3419e3` (02:13).
- `33c0ed1` (12:31): "Fixes post-MCP-client-testing".
- The read-only DB credential was fixed on the server.

**Post-fix tests**

| Round | When (ET) | Steps | Result |
|---|---|---|---|
| R3 | 10-21 12:50 | `get_overview.table_database`; `list_tables standard:16` | **T-01 resolved** (`reachable`, `cogent_ro`, `can_select`, 614 tables). **A-05 resolved** (`run_errors` / `document_errors` itemised). New issues: N-01 sub-number dropped from labels, N-02 `doc_label`="PDF", N-03 caption hyphen/space, N-04 TOC and acronym tables loaded |
| R4 | 10-21 13:23 | Same, after restart and forced run of standard:16 | **N-01–N-04 resolved** (611 tables, 10 tables with `Table 5-1`-style labels). New: N-05 `open_warnings` = 0, N-06 wind-table caption, N-07 missing space in section path, N-13 Table 2-1 lost a row |
| R5 | 10-21 14:02 | Full agent run (18:01Z); `get_overview`, `get_warnings`, `list_tables` | **N-05 resolved** (3 open warnings; info items not counted). New: N-08 warning history wiped, N-09 duplicate 403 in `run_errors`, N-10 counting rule undocumented, N-11/N-12 to verify |

**Report:** `cogent_mcp_test_report_v2.md` (14:06), 37 issues. Status then: 7 resolved, 2 partial, 25 open, 3 needing verification.

**Closing fixes:**
- `fe12f9a` (14:09): "Fixes from further testing".
- **PR #6 merged at 14:12.**

### Cycle 2: workflow scenarios (2026-10-21 16:10–16:30)

**Steps:** ten end-to-end scenarios on `cogent-test20261021`:
1. Part 53 outline and type summary.
2. "safety-related" across Parts 50/52/53.
3. 50.69 two-hop neighbours.
4. NEI 18-04 dependents and relationship map.
5. Part 53 → RA-S-1.4 / RG 1.200 path.
6. NE-STD-1020 SDC tables and the limit-state table.
7. NE-STD-1027 thresholds CSV and units.
8. ASCE 43 ↔ NE-STD-1020 joins.
9. DOE-STD-1271 document map.
10. Appendix B vs NQA-1, and the Part 53 / NEI 18-04 / RG 1.233 / LMP subgraph export.

**Outputs:**
- `ne-std-1027-2025_table1-1_thresholds.csv`
- `lmp_subgraph_part53_nei1804_rg1233_report1.json`

**Status changes observed:**

| Status | Issues |
|---|---|
| Resolved | X-02 (plural identifiers), X-05 (identifier → entry resolution), D-02 (duplicate Part 53 definitions folded with `also_in`), A-02 (`standard:N` accepted), T-02 (8-char id accepted), A-01 (`get_table` paging), S-01 (local paths now relative) |
| Mitigated | R-01 (`order_warnings` flag the problem, but the order is still wrong); A-04 (search returns `term_counts` and suggests `match='any'`) |
| Partial | X-01 (worst junk labels reduced; hundreds of one-offs remain); X-06 (50.69 → Part 100 and → Appendix B cites still missing); V-01 (`doc_label` now shows the 2005 local copy; name, status and severity unchanged) |
| Still open | TB-02 (ASCE 43 OCR prose as tables); N-08 (history not restored) |
| New | **TB-06**: NE-STD-1020 Table 3-1 has a split spanning header, SDC rows not grouped, merged cells and missing notes. **Unnumbered**: `explain_units` reads the footnote marker "(A)" in "Hazard Category 2(A)" as amperes, at 0.97 confidence |

No separate report was written for this round. The findings were given in the replies.

### Cycle 3: scale-out and observability (2026-10-24 → 10-26)

- PR #7 (scale-out, merged 10-24 17:16).
- PR #8 (Grafana/OpenTelemetry, merged 10-26 22:28).

These came from the roadmap, not from test findings, and this session did not test them. The server was redeployed as `cogent-test20261026`.

### Cycle 4: round R6 and PR #9 (2026-10-26 22:10 → 10-27 00:30)

**Issue identification: R6 (10-26 22:10–22:22).** Steps:
- overview of the knowledge base;
- "allowable stress for A572" via `lookup_values`;
- `search_graph` "seismic category" plus neighbours;
- isotope-storage tables and their joins;
- `get_node 'nonexistent'`.

**Fix list:** `cogent_fix_list_2026-10-26.md` (22:23), 15 items:
- R6-01 to R6-04 are P1: lookup with no "not found" signal; Part 50 tables filed under "Appendix B"; the A-6 → 1-1 join is missing; tables not reloaded after the rebuild.
- R6-05 to R6-14 are P2.
- R6-15 asks for verification.

**Confirmed fixed or held in R6:**
- N-08 fixed going forward (history from before 10-21 not restored).
- N-12 resolved.
- Glossary flagged as front matter.
- T-01 and A-05 holding.

**Fix:**
- PR #9 `a434240` (10-27 00:13), **merged 00:17**.
- The review below started at 00:19, so it was a post-merge review.

**Post-fix tests (00:19–00:30):**

| Test | Result |
|---|---|
| Full suite, without PostgreSQL | 257 passed, 46 skipped, 5 failed |
| Table, MCP, agent and extraction tests with PostgreSQL 16 + pgvector | 203 passed, 1 skipped |
| The 5 failures (`test_table_footnotes` ×4, `test_ocr::test_real_rapidocr`) | Also fail on `main`: environment library versions, not the PR |

Probe scripts reproduced four defects:
- the all-identifier gate gave false "no match" (HC-3 + Cs-137);
- ordinal "1." headers still passed as join keys;
- `caption_fragment` missed the Table 1-1 caption and flagged "Damping Values";
- the index regex matched "Index Properties of Soils".

**Review:** `cogent_pr9_review.md`. Verdict "approve after 4 changes":
- **Must fix:** A1, B1, C1, D1.
- **Should fix:** C2, E1, F1, F2.
- **Nits:** A2–A4, B2–B3, D2, E2–E3, F3, G.

### Cycle 5: PR #10 and its follow-up (2026-10-27 01:41 → 02:56)

**Fix 1:** `624271c` (01:41), 12 files, +587/−66. Includes the new `tests/test_round6.py`, about 30 tests.

**Post-fix tests (01:45–01:50):**
- Full suite: 329 passed, 4 skipped, 5 failed (the same environment failures).
- Edge-case probes confirmed every code item from the PR #9 review was fixed.
- Two new findings:
  - **N1:** bare document numbers matched as substrings, e.g. `RA-S-1.4` matched `140` and `2014`.
  - **N2:** `U235` and `Pu239` were treated as non-codes.
- D1 was still open, because `config/standards.json` was unchanged.

Report: `cogent_pr10_review.md`.

**Fix 2:** `0659c5c` (02:37), +57/−10 with 3 new tests:
- N1: bare numbers must be whole tokens.
- N2: nuclides without a hyphen accepted.
- `risc` expansion added.
- DID / "Defense-in-Depth" detected.
- Comment on numeric headers added.

**Post-fix tests (02:39–02:50):**
- Full suite: 332 passed, 4 skipped, 5 failed (the same environment failures).
- `test_round6.py`: 33/33 passed.
- Probes confirmed N1 is fixed for substrings and N2 is fixed.
- **Residual N1b:** a bare `1.4` still matches a whole-token `1.4` in a data cell, a caption ("Table 1.4"), a section ("1.4 Scope"), or a different family (RG 1.4 vs RA-S-1.4). One new test asserts that "section 1.4" is lexical.

Report: `cogent_pr10_review_v2.md`.

**D1 handling:**
- 02:50: the owner reported the config had been updated. Neither GitHub nor the live server showed it: entry 28's Appendix B document still had 6,894 nodes, the size of all of Part 50.
- 02:54: the owner deferred D1, because `config/standards.json` is rewritten at runtime.
- 02:55: verdict changed to **Approve**.
- **PR #10 merged at 02:56** (`1f237f0`).

### Cycle 6: post-merge live check (2026-10-27 10:12)

The `cogent-test20261026` connector failed to reconnect (HTTP 502 through the MCP proxy). Nothing in PR #9 or PR #10 has been checked against the live server.

### Cycle 7: documentation (2026-11-09)

- PR #11 `d602091` aligned README, `docs/` and `notes/` with the code and added `CHANGELOG.md`. It was merged at 00:08.
- `CHANGELOG.md` confirms that the committed configuration does not use `scope` yet, which matches D1 being deferred.
- The evidence report (branch `claude/pdf-html-knowledge-graph-773a5d`, E1–E17 against `1f237f0`) is in the repository but not on `main`. This session did not review it.

## Current known status of every issue

Status key:
- **Resolved (live):** confirmed through the connector.
- **Fixed (code):** fixed and unit-tested, but not yet confirmed on the live server.
- **Partial / Mitigated:** improved, but the issue still shows.
- **Open:** confirmed and not fixed.
- **Not retested:** last observed open and not checked since.
- **Deferred:** the owner chose to postpone.

### Rounds R1–R5 and the workflow round (report v2 IDs)

| ID | Sev | Issue | Status | Last evidence |
|---|---|---|---|---|
| T-01 | P0 | `cogent_ro` auth failure; table DB down | Resolved (live) | R3 10-21; held in R6 10-26 |
| V-01 | P0 | ASCE 43 local copy is 2005; config says 2019 | **Open** (label partly fixed) | R6 10-26: still `info` severity |
| N-08 | P1 | Warning history wiped by restart | Resolved going forward; history before 10-21 lost | R6 10-26 |
| S-01 | P1 | Absolute local paths exposed | Resolved (live) | Workflow 10-21 |
| S-02 | P1 | Session preferences shared under `web:default` | Not retested | R1 10-21 |
| X-01 | P1 | Junk relation labels | Partial; the recipe-10 rebuild may help (unverified) | Workflow 10-21; R6 10-26 |
| X-02 | P1 | Plural identifier lists | Resolved (live) | Workflow 10-21 |
| X-03 | P1 | RA-S-1.4 typed Entity/Value | Not retested (PR #9 added `ANS` and `RA-S` handling) | R1 |
| X-04 | P1 | `None` in identifier label | Not retested | R1 |
| X-05 | P1 | Identifier → entry resolution | Resolved (live) | Workflow 10-21 |
| X-06 | P1 | Missing entry-level cites (Part 100, Appendix B) | Partial | Workflow 10-21 |
| TB-01 | P1 | Footnote markers merged into values | Open on live (NE-STD-1027); fixed (code) via the reload in R6-04 | R6 10-26 |
| TB-02 | P1 | Non-tables ingested | Partial: front matter fixed; ASCE 43 OCR prose remains | Workflow 10-21; R6 |
| R-01 | P1 | Two-column reading order | Mitigated (flagged, not corrected) | Workflow 10-21 |
| T-02 | P2 | 8-char document id rejected | Resolved (live) | Workflow 10-21 |
| TB-03 | P2 | Repeated headers and OCR noise | Fixed (code) via caption recovery and reload (PR #10); unverified | R6 10-26 |
| TB-04 | P2 | Junk joins | Fixed (code) via R6-05 (PR #9/#10); unverified | — |
| TB-05 | P2 | Join evaluation for NE-STD-1020 | Partial | R5 |
| TB-06 | P1 | NE-STD-1020 Table 3-1 structure (spanning header, grouping, notes) | **Open** | Workflow 10-21 |
| — | P1 | `explain_units` reads "(A)" as amperes | **Open** (unnumbered) | Workflow 10-21 |
| A-01 | P2 | `get_table` paging | Resolved (live) | Workflow 10-21 |
| A-02 | P2 | Node-id forms | Resolved (live) | Workflow 10-21 |
| A-04 | P2 | Hyphenated multi-term search | Mitigated (`term_counts`, `match='any'` hint) | Workflow 10-21 |
| A-05 | P2 | Run errors itemised | Resolved (live) | R3; held in R6 |
| A-06 | P2 | Outline titles truncated at 80 chars | Not retested | R1 |
| D-01 | P2 | NQA-1 on board is 1989 | Open (data; warned) | R6 10-26 |
| D-02 | P2 | Duplicate Part 53 definitions | Resolved (live) | Workflow 10-21 |
| N-01–N-04 | P2 | Labels, `doc_label`, captions, front matter | Resolved (live) | R4 10-21 |
| N-05 | P1 | `open_warnings` = 0 | Resolved (live) | R5 10-21 |
| N-06 | P2 | Wind-table caption fragment, no label | Not retested (PR #10 caption recovery may apply) | R5 |
| N-07 | P2 | Missing space in section path | Not retested | R5 |
| N-09 | P2 | Same 403 listed twice | Not retested | R5 |
| N-10 | P2 | `open_warnings` counting rule | Explained (info not counted); documentation not checked | R5 |
| N-11 | — | QME-1 `new_edition_for_purchase` disappeared | Needs owner verification | R5 |
| N-12 | — | Spurious `store_page_changed` | Resolved (live) | R6 |
| N-13 | — | Table 2-1 lost a row | Needs owner verification | R4 |

### Round R6 fix list (2026-10-26)

| ID | Sev | Issue | Status | Evidence |
|---|---|---|---|---|
| R6-01 | P1 | `lookup_values` has no "not found" signal | Fixed (code); residual **N1b** open | PR #9/#10, `test_round6.py` |
| R6-02 | P1 | Part 50 tables filed under "Appendix B" | **Deferred** (D1). The scope mechanism exists, but `config/standards.json` doesn't use it | Owner decision 10-27 02:54 |
| R6-03 | P1 | NE-STD-1027 A-6 → 1-1 join not inferred | Fixed (code); not verified on data | PR #9 |
| R6-04 | P1 | Tables not reloaded after a graph rebuild | Fixed (code): recipe 10 forces one rebuild and reload | PR #9/#10 |
| R6-05 | P2 | Junk joins pass `min_score` | Fixed (code) | PR #10 (ordinal headers) |
| R6-06 | P2 | Store and index tables in the table DB | Fixed (code); index rule narrowed | PR #10 |
| R6-07 | P2 | ANSI/ANS-2.26 not an identifier | Fixed (code) | PR #9 |
| R6-08 | P2 | Singular/plural duplicate entities | Fixed (code) | PR #9/#10 |
| R6-09 | P2 | Entities not linked to tables | Fixed (code) | PR #9/#10 |
| R6-10 | P2 | Circular SDC edges | Fixed (code) | PR #9/#10 |
| R6-11 | P2 | No `get_node` suggestions on `combined` | Fixed (code) | PR #9 |
| R6-12 | P2 | Noisy `get_node` suggestions | Fixed (code) | PR #9 |
| R6-13 | P2 | Misleading unknown-entry error | Fixed (code) | PR #9 |
| R6-14 | P2 | Stale graph builds | Fixed (code) via the recipe stamp | PR #9/#10 |
| R6-15 | — | 11 tables dropped | Fixed (code): `dropped_tables` now reported | PR #9 |

### Code review items (PR #9 / PR #10)

**Fixed:**
- A1–A4: lookup subjects vs. category labels, `min_similarity` preference, RA-S detection, per-field matching.
- B1–B2: placeholder and ordinal headers.
- C1–C2: caption fragments and label recovery; anchored index rule.
- D2: scope conflicts reported.
- E1–E3: hyphenated acronyms, irregular plurals, stricter entity links.
- F1–F3: tests and comment order.
- N1: substring matching of bare numbers.
- N2: nuclides without a hyphen.
- DID detection; `risc` expansion.

**Deferred:** D1.

**Open:**
- **N1b (should fix):** a bare number still matches any whole token `1.4`.
- Nit: connector-word initials loosen acronym matching.
- Nit: unused fixture in `test_more_abbreviations_and_risc_label`.
- Nit: `_fields` docstring.
- Nit: `bases`→`basis` ambiguity.
- Nit: captions with two pipes flagged as fragments.
- Nit: hyphen-split "Ta | ble" label recovery.
- Nit: NSRSS not detected.
- Nit: numeric headers excluded from name joins (documented, by design).

### Environment note

These 5 tests fail in the review container on `main` and on every PR branch:
- `test_table_footnotes` ×4
- `test_ocr::test_real_rapidocr`

They come from PyMuPDF / RapidOCR library versions. They have not been fixed or confirmed against the project's pinned environment.

## Outstanding work, in priority order

1. **Live verification of PR #9/#10.** Restore the MCP connector, which has failed with HTTP 502 since 10-27. Then rerun the eight regression calls from the R6 fix list, for example:
   - "A572" should give `no_confident_match`;
   - the NE-STD-1027 relationship map should include A-6 → 1-1;
   - after the recipe-10 rebuild, `tables.loaded_at` should be at or after the graph build time.
2. **V-01** (P0, open since 10-21): the ASCE 43 edition mismatch is still only an `info` warning.
3. **TB-06** and the `explain_units` amperes error: open since 10-21 and never in a fix list.
4. **R6-02 / D1:** decide how a `scope` survives the runtime rewrites of `config/standards.json`.
5. **N1b:** a bare-number hit should need its family name in the same field.
6. **Retest the rows marked "Not retested":** S-02, X-03, X-04, A-06, N-06, N-07, N-09. Close N-11 and N-13 with the owner.

## Documents produced in this session

| File | Date (ET) | Content |
|---|---|---|
| `cogent_mcp_test_report.md` | 10-21 01:04 (updated 02:31) | v1: 25 issues |
| `cogent_mcp_test_report_v2.md` | 10-21 14:06 | v2: 37 issues with statuses, rounds R1–R5 |
| `ne-std-1027-2025_table1-1_thresholds.csv` | 10-21 16:22 | Cleaned Table 1-1, 1,262 nuclides |
| `lmp_subgraph_part53_nei1804_rg1233_report1.json` | 10-21 16:29 | Exported subgraph |
| `cogent_fix_list_2026-10-26.md` | 10-26 22:23 | R6-01…R6-15 |
| `cogent_pr9_review.md` | 10-27 00:30 | PR #9 review |
| `cogent_pr10_review.md` | 10-27 01:50 | PR #10 review |
| `cogent_pr10_review_v2.md` | 10-27 02:55 | PR #10 re-review; verdict Approve |
| `cogent_test_fix_cycles_2026-11-09.md` | 11-09 | This summary |
