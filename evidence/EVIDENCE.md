# Evidence report for the cogent panel presentation

Collected 2026-11-09 (UTC), start 03:19Z, by the coding agent, from branch **main** = `origin/main` at commit `1f237f0` ("Merge pull request #10 …", 2026-10-27). All commands ran in a git worktree of that commit; nothing in the repository was modified, everything written is under `evidence/` (raw output in `evidence/raw/`).

**Environment (applies to every item).** This machine was a fresh container: the project's Python dependencies were **not installed**, so I created a virtualenv outside the repository and ran `pip install -r requirements.txt` (raw: `E0_pip_install.txt`, versions: `E0_versions.txt`, e.g. pytest 9.2.0, pymupdf 2.1, pymupdf4llm 2.1, pymupdf_layout 2.1, mcp 1.30.0, psycopg 3.3.8, Python 3.13.18). PostgreSQL 16 server binaries are present but the **pgvector extension is not** (`extension "vector" is not available`). **Ollama is not installed or running** (`curl 127.0.0.1:11434` fails, no binary). The OpenTelemetry SDK is not installed (only `opentelemetry-api`). **No real staged data exists on this machine**: no agent home, no staging folder, no real PDF (E4, E5).

**Fixtures.** Where an item needs a staged graph or tables and no real one exists, I staged small homes from the repository's own test fixtures (`tests/mock_site.py` served on 127.0.0.1, and `tests/conftest.py` `DOC_A/DOC_B/DOC_U`). Every such result is marked **FIXTURE** below and in the raw file name. They are not real documents. No request left the machine.

Secrets: none were printed; tokens created for the local servers were written outside `evidence/` and checked not to appear in any file here.

---

## E1. Test count

**Finding:** 341 tests collected; 291 passed, 46 skipped, 4 failed in 54.9 s (second run: 54.6 s).

**Evidence**
- `python -m pytest tests -q -rs -p no:cacheprovider` in the main worktree. Run 1: started 2026-11-09T03:20:44Z, finished 03:21:41Z → `4 failed, 291 passed, 46 skipped, 5 warnings in 54.93s`. Run 2 (with `-rfEs`): `4 failed, 291 passed, 46 skipped, 5 warnings in 54.64s`. `pytest --collect-only -q`: `341 tests collected in 2.24s`.
- **Skips (46):** 42 because pgvector is missing — `pgvector is not available: extension "vector" is not available` — in `tests/test_tabledb.py` (16), `tests/test_mcp_tailoring.py` (13), `tests/test_mcp_tables.py` (11), `tests/test_agent.py` (2). 4 for the OpenTelemetry SDK not being installed: `tests/test_telemetry.py` (3, "OpenTelemetry SDK not installed"), `tests/test_agent.py:741` (1, "could not import 'opentelemetry.sdk.trace'"). Full list with line numbers: `raw/E1_pytest_full.txt`, `raw/E1_pytest_run2_failures.txt`.
- **Failures (4), all in `tests/test_table_footnotes.py`:** `test_superscript_markers_are_split_from_values`, `test_exponents_and_unit_powers_are_not_footnotes[1.0 x 10-4]`, `[Area in m-2]`, `[Dose in cm-3]` (e.g. `assert [['Category',...', 'PAC 2 -']] == [['Category',...Sv', 'PAC-2']]`). They ran against pymupdf 2.1 as installed here.
- **`def test_` functions per file** (counted with grep, not parametrised cases): 265 in total — test_agent 29, test_round6 33, test_tablecheck 20, test_tabledb 16, test_daemon 13, test_mcp 13, test_mcp_tailoring 13, test_scaleout 12, test_telemetry 12, test_mcp_tables 11, test_tables 10, test_llmjoin 9, test_ocr 9, test_config 8, test_snapshot 8, test_store_api 8, test_identifiers 7, test_layout 7, test_combine_resolution 6, test_predicates 6, test_units 5, test_assets_export 4, test_table_footnotes 4, test_identity_filenames 2 (`raw/E1_def_test_per_file.txt`).

**Raw:** `E1_pytest_full.txt`, `E1_pytest_run2_failures.txt`, `E1_collect_only.txt`, `E1_def_test_per_file.txt`, `E1_pytest_started.txt`, `E1_run2_started.txt`, `E1_env.txt`.

**Caveats:** the numbers depend on this environment (no pgvector, no OTel SDK, pymupdf 2.1). With pgvector present the 42 table tests would run instead of being skipped; I did not run them. I did not establish why the 4 footnote tests fail. The first attempt to run pytest failed because pytest was not installed (before the venv); that output is not kept.

---

## E2. MCP tool count

**Finding:** the server advertises **24** tools with default flags and **33** with the table database, LLM and SQL options (31 with only `--tabledb-dsn`).

**Evidence** (tools/list through the MCP Python client against `python src/kg_mcp.py --home <temp> serve --host 127.0.0.1 --port <p> …`; protocol 2025-11-25, server name `cogent-kg`; flags in `raw/E2_commands.txt`)

| config | flags | count |
|---|---|---|
| (a) default | `serve --host 127.0.0.1 --port 18801` | **24** |
| (b) + table database | `--tabledb-dsn postgresql://cogent_ro@127.0.0.1:1/cogent --embedder hash` | **31** |
| (c) + LLM join tool | (b) `--llm-model llama3.1:8b` | **32** |
| (d) + SQL tool | (c) `--allow-sql` | **33** |

(a) names: get_overview, list_entries, get_entry_metadata, get_dependents, get_warnings, get_disclaimers, graph_summary, get_outline, search_graph, get_node, get_neighbors, get_subgraph, get_table, list_assets, get_figure, get_table_csv, list_profiles, set_session_preferences, get_session_preferences, reset_session_preferences, get_data_dictionary, get_relationship_map, get_join_evaluation, export_graph. (b) adds: list_tables, describe_table, explain_units, search_tables, lookup_values, query_table, join_tables. (c) adds: evaluate_joins. (d) adds: run_sql. Registration conditions in code: `src/kg_mcp.py:661` (`if tables is not None and tables.has_db`), `:723` (`if tables.llm_model`), `:736` (`if tables.allow_sql`).

**Raw:** `E2_a_default_tools.json`, `E2_b_tabledb_tools.json`, `E2_c_tabledb_llm_tools.json`, `E2_d_tabledb_llm_sql_tools.json`, `E2_*_server.log`, `E2_commands.txt`, `E2_serve_help.txt`.

**Caveats:** **`--allow-sql` was enabled for (d), only to count `run_sql`.** The DSN points to a closed port (`127.0.0.1:1`), so tools are registered but every table call fails with "table database not reachable" (server log: `TABLE DATABASE NOT USABLE`); no SQL ran. Ollama was not running; `--llm-model` only registers the tool. Tool counts were taken against this commit only.

---

## E3. Configuration counts

**Finding:** 30 standards and 15 reports; 4 standards and 1 report are paywalled by the catalogue's own rule; `format_of_data` is free text, not an enumerated field.

**Evidence** (`raw/E3_config_counts.txt`, collected 2026-11-09T03:20:53Z)
- `config/standards.json` meta `count: 30`, 30 entries; `config/reports.json` meta `count: 15`, 15 entries.
- **Paywalled** (rule in `src/kg_store.py`: `paywalled` field true, or "paywalled" in `format_of_data`): standards 4 — ids 20, 21, 25, 30 (the `paywalled` field is true on 2 of them; the others say "paywalled" in the format text); reports 1 (format text "HTML or paywalled PDF (publishers); …"). Entries with a link on a host in `PAYWALL_HOSTS` (`src/kg_models.py:21`): standards 4 (ids 20, 21, 25, 30), reports 0.
- **Source formats.** There is no format field; counts from the free text of `format_of_data` (entries may mention several): standards — PDF 30, HTML 20, XML 10, eCFR 10 (10 mention PDF but no HTML/XML; 0 mention HTML/XML without PDF); reports — PDF 12, HTML 5, eCFR 3 (9 PDF only; 2 HTML/eCFR without PDF). By link URL: standards have 44 links (11 end in `.pdf`, 10 on `ecfr.gov`, 23 other); reports have 12 (8 `.pdf`, 1 eCFR, 3 other) and **8 of 15 reports have no links**.
- **Formats actually staged** (recorded in the committed `version_tracking.staged.documents[].kind`): 46 distinct document URLs: pdf 18, html 18, xml 10.

**Raw:** `E3_config_counts.txt`.

**Caveats:** "PDF / HTML / eCFR XML" per entry is not recorded as a field; the counts above are text matches or URL patterns and the staged-kind counts reflect only what the committed `version_tracking` blocks recorded (E4).

---

## E4. The 2026-10-12 live run

**Finding:** **No run report, log, state file or archive from a live run exists** in the repository or on this machine; the only trace is the committed `version_tracking` blocks in `config/*.json`, whose timestamps put the first fetches at 2026-10-12 20:37–20:42 UTC, about 3 h 20 min **before** the initial commit that already contains the 0.8 threshold and the review queue. No confidences were recorded.

**Evidence**
- *Search:* no tracked or historically tracked path matches `logs/`, `report.md`, `changes.jsonl`, `runs.jsonl`, `config/history`, `archive/`, `state/`, `staging/`, `alerts.json`, `*.jsonl` (`git ls-files`, `git log --all --name-only`); `find /` (excluding pytest temp dirs from E1) found no agent home. (`raw/E4_search.txt`). The only such files on the machine are from my own mock-site runs.
- `README.md:523`: "The committed `config/*.json` carry `version_tracking` blocks from a full run against the live sites on 2026-10-12."
- *What the committed blocks show* (`raw/E4_config_version_tracking.txt`): all 30 standards and 15 reports have a `version_tracking` block with keys `status`, `latest`, `staged` only. Statuses — standards: current 26, partially_staged 1, no_local_copy 2, update_available 1; reports: current 7, no_documents 8. Documents recorded as fetched: 45 on 2026-10-12 (first `2026-10-12T20:37:26Z`, last `20:42:06Z`), 4 on 2026-10-13, 8 on 2026-10-21; `kg_built` dates 2026-10-20 (37) and 2026-10-21 (8). **Sites reached on 2026-10-12** (45 documents by host): www.nrc.gov 23, www.ecfr.gov 8, id.energy.gov 4, www.energy.gov 3, www.directives.doe.gov 3, www.standards.doe.gov 3, www.asme.org 1. Only 4 `latest.detected` timestamps exist (one on 2026-10-12: `QME 1` edition 2025 at `2026-10-12T20:37:26Z`; three on 2026-10-21).
- *Findings/confidence/auto-applied vs queued:* **NOT FOUND** — no `confidence` field exists in any committed block (0 found), and no pending/changes/report file was committed, so how many findings were auto-applied (≥ 0.8) vs queued, and whether confidences were mixed, **cannot be determined from the repository**. For reference only, the code assigns these literal confidences: 1.0 (3 places), 0.9 or 0.5 (`0.9 if link else 0.5`, `src/kg_tools.py:682`), 0.3 (`page_changed`, `:698`) (`raw/E4_confidence_values_in_code.txt`).
- *Threshold history* (`raw/E4_threshold_history.txt`): `git log -S"min_confidence"` → first commit `c3c26a8` 2026-10-12T20:01:52-04:00 "Initial commit -- staging agent for Knowledge Graphs" (then again in `64f8cc3`, 2026-10-20). `auto_apply` and the review queue (`state/pending.json`, "Pending (needs human review)" report section, `--no-auto-apply`) are also in `c3c26a8`. Current definitions: `src/kg_agent.py:74-75` (`auto_apply: bool = True`, `min_confidence: float = 0.8`), applied at `:411` (`s.auto_apply and f.confidence >= s.min_confidence and f.new_url and not rt.dry`).
- *Before or after the live run:* the first commit (= 2026-10-13T00:01:52Z) already contains the committed config with timestamps `2026-10-12T20:37:26Z` … `20:42:41Z` (standards: 70 timestamps; reports: 27) — i.e. the recorded fetches are **3 h 19 min 46 s earlier than the commit** that introduces 0.8 (`raw/E4_config_history.txt`). Git history starts with that commit, so it cannot show whether the 0.8 threshold existed in the code when the fetches ran.

**Raw:** `E4_search.txt`, `E4_config_version_tracking.txt`, `E4_config_history.txt`, `E4_threshold_history.txt`, `E4_confidence_values_in_code.txt`.

**Caveats:** timestamps are as written in the JSON (`…Z`); the ordering above assumes they are UTC. The config blocks were also rewritten by later runs (2026-10-13, 20, 21), so the 2026-10-12 values describe only documents that were not refetched since.

---

## E5. PDF engines compared on a real document

**Finding:** **NOT RUN.** No real PDF exists in the repository's tracked files or in any staging/documents folder on this machine, so there was nothing to ingest twice.

**Evidence**
- `git ls-files | grep -i '\.pdf$'` → no output. `find /` for `*.pdf` (excluding pytest temp dirs) → only 11 matplotlib icon/logo PDFs inside the scratch venv and one LibreOffice file `xpdfimport_err.pdf` (the listing was cut at 30 lines by `head`; it had 12 entries); none is a standard or a staged document. No `staging/` or `documents/` folder with staged data exists (`raw/E5_search.txt`). The tests build their own PDFs with reportlab/PyMuPDF (`tests/mock_site.py:14-31`, `tests/test_layout.py:73-77`): those are fixtures, which the brief excludes.
- *Engines in this environment* (`raw/E5_engine_availability.txt`): `pymupdf`, `pymupdf4llm` and `pymupdf.layout` import fine (pymupdf_layout 2.1 installed; the module name `pymupdf_layout` itself does not import, `pymupdf.layout` does). The CLI flag exists: `--pdf-engine {auto,layout,basic}` (`src/kg_ingest.py`; `KG_PDF_ENGINE`). So the layout libraries are installed; the missing piece is the document.

**Raw:** `E5_search.txt`, `E5_engine_availability.txt`.

**Caveats:** I did not substitute a fixture PDF. No node lists were produced.

---

## E6. A real table

**Finding:** **NOT FOUND (real) / NOT RUN (lookup).** There is no staged table from a real standard anywhere; the only tables available are from test fixtures. No staged table mentions A572 except one **fixture** table; `lookup_values` could not run (no pgvector).

**Evidence**
- No table database content: PostgreSQL has no pgvector here, so no table DB can be built (42 tests skipped for this reason, E1). No real CSV exports exist (E4/E5 searches).
- *Fixture tables (not from a real standard)* — three examples from `raw/E6_tables_FIXTURE.txt` (staged from `tests/conftest.py` fixtures; page is `None` because the fixtures are HTML):
  1. document `aaaaaaaaaaaaaaaa` (DOC_A), asset `tbl0004`, caption "Table 3-1: Seismic design categories", columns `['Seismic Category', 'Limit State', 'Annual Probability (per year)']`, 3 data rows.
  2. document `aaaaaaaaaaaaaaaa`, asset `tbl0008`, caption "Table 4-1: Damping values by seismic category", columns `['Seismic Category', 'Structure Type', 'Damping (%)']`, 5 data rows.
  3. document `cccccccccccccccc` (DOC_U), asset `tbl0004`, caption "Table 5-1: Plate limits (units: ksi)", columns `['Material', 'Allowable Stress', 'Thickness', 'Damping', 'Temperature (°C)', 'Count']`, 3 data rows.
- *A572:* `git grep A572` → the only table content is the fixture row `<tr><td>A572</td><td>30.0</td>…` at `tests/conftest.py:154` (DOC_U, "Table 5-1: Plate limits (units: ksi)"), also in `tests/test_tablecheck.py:151`; used by `tests/test_mcp_tailoring.py:262-272` (`lookup_values … "allowable stress A572 steel plate"` expects `Material == "A572"`). The other hits are documentation/code comments (`notes/LiveDemo.md:99,127`, `src/kg_mcp.py:58`, `src/kg_tableservice.py`) and tests that expect `no_confident_match` for "ASTM A572" on a different fixture (`tests/test_mcp_tables.py:130-132`). No real standard's table contains "A572" in the repository.
- *`lookup_values` for "allowable stress A572" under `strict` and `exploratory`:* **NOT RUN.** The call was made through the MCP server (strict session `ev-strict`) and returned `Error executing tool lookup_values: table database not reachable: OperationalError: connection failed …` (`raw/E11_calls/31_lookup_values.json`). Because it did not run, I cannot state whether the result is `no_confident_match` or rows, nor list the returned fields from an actual result. *From code only* (`src/kg_tableservice.py`, `lookup_values` ~lines 470-500 and `assess_lookup`), a result carries: `question`, `mode`, optional `note`, `match_quality`, `no_confident_match`, `message`, `matched_terms`, `missing_terms`, `nearest_neighbours`, `rows` (each with `table_id`, `row`, `values`, `similarity`, `found_by`, `table.caption`, `table.source{document_id, document, url, page, caption, section}`, `table.paywalled`, units fields), `tables_searched`, and a `provenance` block (`derived_from`, `embedder`, `recipe`, `disclaimer`, `preferences`) plus `paywalled`/`copyright_notice`/`warnings` only for paywalled documents (`raw/E11_code_table_wrap.txt`).

**Raw:** `E6_tables_FIXTURE.txt`, `E11_calls/31_lookup_values.json`, `E11_code_table_wrap.txt`.

**Caveats:** the three table examples are fixtures; do not cite them as real standards.

---

## E7. Strict versus exploratory without a language model

**Finding:** **PARTIAL.** The profile settings are identical in neither case — 7 of 8 preference fields differ — but the same *question* could not be run through the two profiles (no table database).

**Evidence** (real MCP calls to a local fixture server, `set_session_preferences(profile=…, session_id=…)` then `get_session_preferences`; `raw/E7_strict_vs_exploratory_prefs.txt`)

| field | strict | exploratory |
|---|---|---|
| profile | strict | exploratory |
| approach (join candidates counted) | consensus | hybrid |
| include_rejected | false | true |
| min_score (join score floor) | 0.6 | 0.3 |
| min_similarity (lookup floor) | 0.7 | 0.0 |
| retrieval | semantic | hybrid |
| units | strict | broad |
| min_agreement | 0.0 | 0.0 (same) |

`list_profiles` descriptions: strict — "only joins found by both methods and accepted by the judge: fewest false joins"; exploratory — "everything plausible: rejected / disputed joins (flagged), inferred units with alternatives, hybrid retrieval: for review".

**Not run:** any result of `lookup_values`/`query_table` under the two profiles — what changes in the returned rows, flagged items and rejected joins — needs PostgreSQL with pgvector (`raw/E11_calls/32_query_table.json` etc. show "table database not reachable"). No Ollama or judge was involved in these calls.

**Raw:** `E7_strict_vs_exploratory_prefs.txt`, `E11_calls/` (calls 18-23).

**Caveats:** the preference table is the effective configuration, not an observed difference in answers.

---

## E8. Edition information

**Finding:** **PARTIAL.** `get_entry_metadata` returns edition information in several fields (fixture example below); table tools' results contain **no edition field** according to the code, but `lookup_values`/`query_table` could not be run to confirm on a real result.

**Evidence**
- `get_entry_metadata` (FIXTURE agent home, entry `standard:2`, after the mock site published a newer revision; `raw/E8_edition_fields.txt`): keys `…, 'graph', 'capture', 'documents', 'version', 'warnings', 'disclaimers'`. Example quoted from `version`: `"status": "update_available"`, `"latest_available": [{"identity": "RG 1.233", "edition": "1", "label": "RG 1.233 Rev. 1", …}]`, `"on_board": {"RG 1.233": {"edition": "0", "source": "configuration"}}`, `"summary": "A newer edition is available than the one on board (latest: RG 1.233 Rev. 1)."`. Each `documents[]` item also has `editions_stated_in_text` (an empty `{}` for the fixture) and `on_board_documents[].editions`.
- `lookup_values`, `query_table`: **NOT RUN** (no pgvector). In code, a table result's `source` has `document_id, document, url, page, caption, section` and `provenance` has `derived_from, embedder, recipe, disclaimer, preferences` (`src/kg_tableservice.py` `_decorate`/`_wrap`, `raw/E11_code_table_wrap.txt`); `grep -i 'edition\|version'` over `src/kg_tableservice.py` finds no edition field (`raw/E8_edition_fields.txt`). The "document" string is the title from the configuration (e.g. "ASME NQA-1-2026 (fixture)"), which may contain an edition only if the configured title does.
- Graph tools return a `provenance` block with `version_status`, `version_summary`, `latest_available` (e.g. `raw/E11_calls/13_list_assets.json`).

**Raw:** `E8_edition_fields.txt`, `E11_calls_agenthome/02_get_entry_metadata.json`, `E11_calls/13_list_assets.json`.

**Caveats:** all quoted values are FIXTURE data.

---

## E9. Notifications

**Finding:** **No outward notification channel exists in the code.** Alerts are written to files and shown by the CLI/MCP; failure/warning hooks run a user-supplied command; mail/Slack/pager are mentioned only in documentation or as templates.

**Evidence** (`git grep` on `origin/main`, `raw/E9_notification_grep.txt`)
- Email/SMTP: no `smtplib`/SMTP code. Hits: `deploy/cogent.cron:3: MAILTO=you@example.org` (a cron feature, placeholder address), `docs/scheduling.md:19` ("Failure notification | `MAILTO` if mail works | `OnFailure=` unit | `--on-failure CMD`, `--on-warnings CMD`"), `deploy/cogent-run.service:32` (a comment: "notification on failure, e.g. a unit that sends mail: OnFailure=cogent-notify@%n.service"), `docs/observability.md:93` ("Routing the alert (e-mail, Slack, pager) needs an Alertmanager or Grafana contact point; neither is configured here") and `:109` ("No Alertmanager or notification channel is configured.").
- Webhook/Slack/Teams/Discord/PagerDuty/etc.: only the `docs/observability.md:93` mention. Syslog/desktop notifications: **NOT FOUND** (no `syslog`, `notify-send`, `osascript`).
- `sd_notify` (`src/kg_daemon.py:150`) talks to systemd's notify socket only (`READY=1`, `STATUS=`, `WATCHDOG=1`) — not a user notification. Metrics push: `--pushgateway`/`COGENT_PUSHGATEWAY` (`src/kg_telemetry.py:566`) is an optional PUT of metrics to a Pushgateway, not a notification.
- Hooks: `src/kg_daemon.py:289-290,448-460`: `--on-failure CMD` (when a run fails for good) and `--on-warnings CMD` (after a run that left open warnings) run a command supplied by the operator with `COGENT_*` environment variables; none is configured by default.
- Outbound HTTP calls in `src/` are for fetching sources, Ollama (chat/embeddings), snapshots and the Pushgateway only (`urlopen` hits in kg_tools, kg_ingest, kg_llm, kg_embed, kg_snapshot, kg_telemetry).
- *Where alerts end up* (`raw/E9_alert_destinations.txt`, `raw/E9_status_pending_FIXTURE.txt`): `state/alerts.json` (`src/kg_agent.py:541-563`), `state/pending.json` (the review queue), a section appended to `logs/report.md` per run ("Pending (needs human review)", `src/kg_agent.py:1545`), counters in `logs/runs.jsonl`, `config/changes.jsonl` (audit log), the `status` subcommand (`kg_agent.py status`, "entry KGs: …; config changes logged: …"), and over MCP `get_warnings`, `get_overview.open_warnings`, `get_entry_metadata.warnings`. The daemon's `status` command prints the count of open warnings read from `state/alerts.json` (`src/kg_daemon.py:561-580`).

**Raw:** `E9_notification_grep.txt`, `E9_alert_destinations.txt`, `E9_status_pending_FIXTURE.txt`.

**Caveats:** the `status` output shown in the raw file is from the FIXTURE home.

---

## E10. Reversibility

**Finding:** **NOT FOUND — no command or code reverts a superseded graph or an applied finding.** Superseded versions and configuration snapshots are archived, and orphaned data can be restored by a manual `tar` command recorded in state.

**Evidence** (`raw/E10_reversibility_code.txt`, `raw/E10_archive_example_FIXTURE.txt`)
- `git grep -i "revert|rollback|restore|undo|unapply|rewind|downgrade"` over `src README docs notes deploy`: the only restore-related hits are `README.md:263-264` and `src/kg_agent.py:828-861,1444` — orphaned staging data archived by a forced run into `archive/orphans/<kind>_<id>_<run>.tar.gz` with a recorded command `tar -xzf <home>/archive/orphans/<name> -C <home>/<restore_into>/` (manual, in `state/orphans.json`). Other hits (`kg_tabledb.py`/`kg_tableservice.py` `rollback`) are SQL transaction rollbacks. No CLI subcommand does this: the subcommands are `run`, `daemon`, `probe`, `status`, `local`, `add-local` (kg_agent); `serve`, `token` (kg_mcp); `publish`, `pull`, `watch`, `status` (kg_snapshot); `build`, `status`, `check`, `search` (kg_tabledb).
- *What is archived on a change* (`_archive`, `src/kg_agent.py:1038-1046`; `config/history` at `:326-338,625-626`): a superseded **document KG** (`kg.json` + `state.json`) under `archive/documents/<docid>/<ts>_<sha>/`; a superseded **entry KG** (`kg.json` + `meta.json`) under `archive/entries/<key>/<ts>/`; **configuration snapshots** `config/history/<run>_<name>.json` and `<name>.last.json`; an **audit-log** line per configuration change in `config/changes.jsonl` (fields `ts, run_id, file, entry, path, old, new, reason, source, auto`). The diff of node/edge counts is written to the run report (e.g. `nodes 10→11, edges 15→17`), not stored as a separate file.
- *One real example path (FIXTURE run on the local mock site, 2026-11-09):* `archive/documents/571fc37611a8de18/20261109T032544Z_1435dcfc/kg.json` (+ `state.json`), `archive/entries/standard_1/20261109T032544Z/kg.json` (+ `meta.json`), `config/history/20261109T032544Z_standards.json`, and `config/changes.jsonl` with 8 lines. Report line: "previous version archived: `archive/documents/571fc37611a8de18/20261109T032544Z_1435dcfc`".

**Raw:** `E10_reversibility_code.txt`, `E10_archive_example_FIXTURE.txt`.

**Caveats:** archives exist so a human can copy files back; I found no code that does so. `--keep-archive N` prunes older archived versions (`_prune_archive`).

---

## E11. Provenance coverage by tool

**Finding:** **PARTIAL.** For 20 of the 33 tools I have an observed result (FIXTURE data); the other 13 (the table-database tools, the three published-file tools and `get_figure`) returned errors and are described from code only. Provenance and disclaimers appear on graph and asset tools; open warnings appear only on entry-level/metadata tools and in `provenance` of entry-scoped graph calls; table tools add `warnings` only for paywalled documents.

**Evidence.** Real MCP calls, one or more per tool, against two local FIXTURE servers (`raw/E11_calls/`, fixture home with tables, port 18810; `raw/E11_calls_agenthome/`, home built by the agent from the mock site, port 18811). The matrix was computed by scanning the saved JSON for keys (`raw/E11_provenance_matrix.md`, full table with the keys found). Summary (✓ = key seen in the observed result; "—" = key absent from the observed result; **n/r** = not run):

| tool(s) | provenance (document, page, caption) | open warnings | paywall flag | machine-extracted disclaimer |
|---|---|---|---|---|
| get_overview | — (no `provenance`; counts) | `open_warnings` ✓ | — | `disclaimers` ✓ |
| list_entries | — | `open_warnings` per entry ✓ | `paywalled` per entry ✓ | — |
| get_entry_metadata | — (own metadata: capture, documents, version) | `warnings` ✓ (non-empty for the paywalled fixture entry) | `paywalled` ✓ | `disclaimers` ✓ |
| get_warnings | — | `warnings` ✓ | — | — |
| get_disclaimers | — | — | — | `disclaimers` ✓ |
| get_dependents | — | — | — | — (agent home call: keys only `entry, name, depends_on, declared_dependents…`) |
| graph_summary, search_graph, get_node, get_neighbors, get_subgraph, export_graph | `provenance` ✓ (graph build time, sources_last_checked, version status; `page` only inside node/edge items) | `provenance.warnings` ✓ for entry refs (empty in fixture); none for document-id refs, where `provenance.note` says to call get_entry_metadata | `node(s).paywalled` on combined-graph calls; none otherwise | `provenance.disclaimer(s)` ✓ |
| get_outline | `provenance` ✓, `outline[].page` ✓ | `provenance.warnings` (entry ref) | — | ✓ |
| get_table | `provenance` ✓, `page` ✓ | — | — | ✓ |
| list_assets | `provenance` ✓; per asset `asset_id, kind, page, caption, rows, cols` ✓ | `documents.warnings` + `provenance.warnings` ✓ (non-empty for the paywalled fixture entry) | `documents.paywalled`, `paywalled_assets_policy` ✓ | `provenance.disclaimer` ✓ |
| get_table_csv | `provenance` ✓, `caption`, `page` ✓ | `warnings` ✓ (non-empty for the paywalled fixture) | `paywalled` ✓ | `provenance.disclaimers` ✓ |
| get_figure | **n/r** (the fixtures contain no figure: "no figure …/fig0001") | n/r | n/r | n/r |
| list_profiles, set/get/reset_session_preferences | — (preferences only) | — | — | — |
| get_data_dictionary, get_relationship_map, get_join_evaluation | **n/r** ("none published yet: run the table database agent") | n/r | n/r | n/r |
| list_tables, describe_table, explain_units, search_tables, lookup_values, query_table, join_tables, evaluate_joins, run_sql | **n/r** (table database unreachable) | n/r | n/r | n/r |

*From code (not run), table tools* (`src/kg_tableservice.py:333-345` `_wrap`, `_decorate`; `raw/E11_code_table_wrap.txt`): every `_wrap`ped result (list_tables, describe_table, explain_units, search_tables, lookup_values, query_table, join_tables, evaluate_joins, run_sql) gets `provenance{derived_from, embedder, recipe, disclaimer, preferences}`; `source{document_id, document, url, page, caption, section}` is attached to tables/rows; `paywalled: true`, `copyright_notice` and `warnings` (the paywall warnings only) are added **only if a document in scope is paywalled**; open agent alerts are **not** included in table results. The three file-based tools (data dictionary / relationship map / join evaluation) are built from published files and carry `paywalled` (`:563,643`); I did not find a `provenance` wrapper for them in `_wrap` (not run).

**Raw:** `E11_provenance_matrix.md`, `E11_calls/*.json`, `E11_calls_agenthome/*.json`, `E11_code_table_wrap.txt`.

**Caveats:** matrix cells reflect keys present in single calls on tiny fixtures (e.g. an empty `warnings` list is reported as present-but-empty). The paywalled fixture entry is `standard:2` of the tables home; a real paywalled document was not available.

---

## E12. Development history and testing rounds

**Finding:** 40 commits (29 non-merge) between 2026-10-12 and 2026-10-27 by two author names; **no trailers, no file and no folder on main records testing rounds 1–5**; testing rounds appear only in commit messages and in `tests/test_round6.py` (R6-ids). Nothing in the repository says the testing client was a language model.

**Evidence** (`raw/E12_git_authors.txt`, `E12_git_messages.txt`, `E12_rounds_grep.txt`, `E12_other_branch.txt`)
- `git shortlog -sn` (all 40 commits): **NearIdentity 30**, **Abdullah Al Rashid 10**; non-merge commits (29): NearIdentity 29. Committer name: GitHub 13, NearIdentity 27. First commit 2026-10-12 20:01:52 -0400, last 2026-10-27 02:56:06 -0400. The 10 commits by "Abdullah Al Rashid" are all merge commits.
- **Trailers:** none. `git log --format=%(trailers)` shows no `Co-Authored-By` (or any) trailer on main; a search of message bodies for "co-authored|claude|anthropic|agent|tester|round" matches only on the word "agent" in three messages (`39feede` "Forced data refresh option for KG agent", `c3c26a8` "…staging agent for Knowledge Graphs", and the PR 9 merge via its branch name `mcp-agent-testcycle-updates`). `origin/claude/pdf-html-knowledge-graph-773a5d` exists on the remote and has **0 commits ahead of main** (`E12_other_branch.txt`).
- **PR merge messages (verbatim):** #2 "MCP Server for Staged Knowledge Graphs"; #4 "Fixes to .bin Format for Extracted Figures"; #5 "Forced data refresh option for KG agent"; #6 "PGVector DB for Extracted Tabular Data + Associated MCP Services"; #7 "Steps to Facilitate Scaleable Deployment"; #8 "Grafana Dashboard for Analytics"; #9 "Updates Based on the Latest MCP Client-Side Testing (2026-10-26)"; #10 "Updated fixes post-testing" (merge `1f237f0`, 2026-10-27).
- **Commit messages (verbatim, single-line):** `33c0ed1` (2026-10-21 12:31 -0400, NearIdentity) "Fixes post-MCP-client-testing"; `a434240` (2026-10-27 00:13 -0400) "Updates based on the latest MCP client-side testing (2026-10-26)"; also `fe12f9a` "Fixes from further testing" (2026-10-21), `dc6e56e` "Various fixes", `2901187` "Runtime bug-fix", `624271c` "Updated fixes post-testing", `0659c5c` "Follow-up fixes". Branch names that mention testing: `mcp-agent-testcycle-updates` (PR 9), `mcp-client-testcycle-fixes-addendum` (PR 10).
- **Files recording rounds:** a case-insensitive search of tracked files for `round [0-9]`, `R[1-6]-[0-9]{2}`, `test round`, `testcycle`, `MCP client`, `tester`, `test report`, `client-side testing`, `language model`, `claude`, `anthropic` finds **no file or folder recording rounds 1–5** (no CHANGELOG, test report or fix list is tracked on main; `git ls-files` has no such names). The only round-labelled file is `tests/test_round6.py` (docstring "Test-round 6 fixes (batch 1): … (R6-01) … (R6-04 / R6-14 / R6-15) … (R6-11 / 12 / 13)"; section comments for R6-01…R6-15); `README.md:517` mentions "round-6 fixes: `test_round6.py`".
- **Was the tester a language model?** **NOT FOUND.** The repository never describes the test client. Hits for "Claude" are about *serving* Claude Code / claude.ai (e.g. `README.md:10` "to AI clients such as Claude Code or the Claude web app", `docs/mcp.md`, `notes/LiveDemo.md`); `tests/test_mcp.py:195` registers an OAuth test client named "Claude" (a fixture); `src/kg_mcp.py` instructions text is addressed to an MCP client agent. `README.md:13` says update detection is "done by hard-coded parsers and trackers, not by a language model" (about the agent, not the tester).

**Raw:** `E12_git_authors.txt`, `E12_git_messages.txt`, `E12_rounds_grep.txt`, `E12_other_branch.txt`.

**Caveats:** this is main only; other local branches or an author's local files are out of scope. Co-author trailers could have been stripped by squashing; I only report what is in the log.

---

## E13. Observability

**Finding:** observability files are present; **no recorded results** (no `.prom`, `agent.json`, `metrics/`, run logs) are tracked. The only artefact of traffic is a screenshot, which the docs state came from synthetic traffic (mock site, mock Ollama).

**Evidence** (`raw/E13_observability.txt`)
- Files: `deploy/observability/{demo/generate_traffic.py, docker-compose.observability.yml, env.example, grafana/build_dashboard.py, grafana/dashboards/cogent-observability.json, grafana/provisioning/dashboards/cogent.yml, grafana/provisioning/datasources/prometheus.yml, prometheus/alerts.yml, prometheus/alerts_test.yml, prometheus/mcp_token.example, prometheus/prometheus.yml}`, `docs/observability.md`, `docs/img/grafana-dashboard.png` (434,112 bytes, 1600×1900 px, added in `f534b72` 2026-10-24), `src/kg_telemetry.py`, `tests/test_telemetry.py`.
- **Dashboard:** `deploy/observability/grafana/dashboards/cogent-observability.json` — Grafana dashboard "cogent: agent and MCP server" (uid `cogent-observability`, refresh 10 s, last 30 minutes) with 29 panels in six rows: last run, latency p50/p95, tokens/cost/findings, LLM quality (judge agreement, hallucination), failures and retries, MCP server.
- **Alert rule:** `deploy/observability/prometheus/alerts.yml` — one rule, `CogentAgentFailureRateHigh`: fires when failed agent operations / all agent operations over 24 h exceeds 0.25 for 5 minutes, severity `warning` (the file's own comment: "The one alert: more than a quarter of the agent's operations failed over the last 24 hours"). `alerts_test.yml` holds three promtool unit-test cases (healthy 5 %, a bad stretch firing at 41.56 %, no data).
- **What traffic produced results:** `docs/observability.md:7`: "The screenshot was taken from a real Prometheus and Grafana, fed by `deploy/observability/demo/generate_traffic.py` … The traffic is synthetic: a mock web site, a mock Ollama …"; `notes/LiveDemo.md:144,153` ("synthetic runs (mock site, mock model)", "exercised … against a synthetic demo … not against your sources, Ollama model, or the Claude web app"); `deploy/observability/demo/generate_traffic.py:1-12` describes a private PostgreSQL, `tests/mock_site.py`, a mock Ollama and the MCP server. `docs/observability.md:108-109`: OTLP tested with in-memory/console exporters only, Pushgateway against a stub, no Alertmanager. Real traffic: none recorded.

**Raw:** `E13_observability.txt`.

**Caveats:** I did not open or verify the screenshot's content; the "synthetic" statement is from the docs and the generator's own header. I did not run Prometheus/Grafana here.

---

## E14. Graph schema as produced

**Finding:** **PARTIAL.** No real or staged-from-real graph exists; counts below are from **FIXTURE** graphs built from the mock site. Edge types seen: CONTAINS, NEXT, CITES, MENTIONS, references, related_to, HAS_DOCUMENT; combined graph: complies_with, cites.

**Evidence** (`raw/E14_graph_schema_FIXTURE.txt`; mock site, 3 standards + 2 reports, two runs; collected 2026-11-09)
- Document graph `…/571fc37611a8de18` (eCFR-style XML): 11 nodes — Heading 3, Paragraph 3, Entity 2, Identifier 2, Document 1; 17 edges — CONTAINS 6, NEXT 5, references 3, MENTIONS 2, related_to 1.
- Document graphs of the two PDFs: 5 nodes/6 edges (Heading 2, Document 1, Paragraph 1, Identifier 1; CONTAINS 3, NEXT 2, CITES 1) and 7 nodes/8 edges (Identifier 3, Heading 2, Document 1, Paragraph 1; CONTAINS 3, CITES 3, NEXT 2).
- Entry graph `standard:1`: 12 nodes (adds `Standard` 1) and 18 edges, adding `HAS_DOCUMENT` 1.
- **Combined graph:** 5 nodes (Standard 3, Report 2), 5 edges (`complies_with` 3, `cites` 2). Edges and their fields: `entry:report:1 → entry:standard:1` `complies_with` `basis: config`, `confidence: curated`; `entry:report:1 → entry:standard:2` `complies_with` config/curated; `entry:report:2 → entry:standard:3` `complies_with` config/curated; `entry:standard:2 → entry:standard:1` `cites` `basis: text`, `confidence: inferred`, `count: 1`, `identifiers: ['CFR 50.69']`; `entry:report:1 → entry:standard:1` `cites` text/inferred. Combined node attributes: `graph_built, has_graph, id, kind, label, latest_available, on_board, paywalled, type, version_status`.
- Not present in these fixtures (so not counted): `overlaps_with` (no overlaps in the fixture configuration; the code writes it from `overlaps_with` in `config/standards.json`, `README.md` mentions it), `has_value` and table nodes/`TableRow`/`Value` (the mock documents contain no tables).

**Raw:** `E14_graph_schema_FIXTURE.txt`.

**Caveats:** totals are tiny and are not representative of a real run; they only show which node and edge types the pipeline produced for fixture input.

---

## E15. Embedding claim

**Finding:** **PARTIAL.** Only the deterministic `hash` embedder could be run; the configured Ollama embedder (`nomic-embed-text`) is **NOT RUN** (no Ollama).

**Evidence** (`raw/E15_embedding_cosine.txt`, 2026-11-09T03:28Z). Embedder `HashEmbedder` (`src/kg_embed.py:63`: feature hashing of tokens `re.findall(r'[a-z0-9]+', text.lower())` plus bigrams; no model); `get_embedder("hash", dim=…)`. Pairwise cosine similarity, `hash/64` and `hash/768`:

| pair | hash/64 | hash/768 |
|---|---|---|
| "0.5" vs "5.0" | 0.8889 | 0.8889 |
| "0.5" vs "50" | 0.0000 | 0.0000 |
| "0.5" vs "0.05" | 0.4444 | 0.4444 |
| "5.0" vs "50" | 0.0000 | 0.0000 |
| "5.0" vs "0.05" | 0.4444 | 0.4444 |
| "50" vs "0.05" | 0.0000 | 0.0000 |
| "0.5" vs "yield strength 0.5" | 0.6468 | 0.6882 |
| "0.5" vs "yield strength 5.0" | 0.5353 | 0.6118 |
| "5.0" vs "yield strength 0.5" | 0.6468 | 0.6118 |
| "5.0" vs "yield strength 5.0" | 0.6882 | 0.6882 |
| "0.05" vs "yield strength 0.5" | 0.3234 | 0.3059 |
| "0.05" vs "yield strength 5.0" | 0.3059 | 0.3059 |
| "50" vs "yield strength 0.5" / "5.0" | 0.0000 | 0.0000 |
| "yield strength 0.5" vs "yield strength 5.0" | 0.8903 | 0.8947 |

Configured embedder: default `nomic-embed-text` (`src/kg_embed.py:27`), selected by `--embedder ollama` (default); `curl 127.0.0.1:11434` → no response, no `ollama` binary.

**Raw:** `E15_embedding_cosine.txt` (contains, at its top, a traceback from my first attempt, which called a method that does not exist on the class; the corrected run follows it).

**Caveats:** numbers are for the hash embedder only and say nothing about `nomic-embed-text`; no interpretation is offered.

---

## E16. LLM-based edges

**Finding:** the default extractor is the rule-based one (`rules`); LLM join inference is off by default and needs `--tables --llm-joins`; **no recorded run in the repository used a real model** — the docs state the LLM stages were tested only with scripted models and a mock Ollama server.

**Evidence** (`raw/E16_llm_edges.txt`)
- Defaults: `Settings.extractor: str = "rules"` (`src/kg_agent.py:69`); CLI `--extractor {rules,ollama,hybrid}` default `rules` (`:1758`); `--model` (Ollama model for triples), `--llm` (Ollama model "used only to narrate the report"), `--ollama-host`. `kg_ingest.make_extractor` returns `RuleBasedExtractor` for `rules`, `OllamaExtractor` for `ollama` (LLM, rule-based fallback) and `HybridExtractor` (LLM + rules) for `hybrid`.
- `llm_joins: bool = False` (`src/kg_agent.py:94`); flags `--llm-joins` ("with --tables: also infer joins with an Ollama LLM and evaluate all candidates with an LLM judge", `:1792`), `--join-model` (default `llama3.1:8b`), `--join-judge-model`, `--join-samples` (3), `--join-judge-runs` (3), `--join-max-pairs` (40), `--join-max-seconds`. MCP: `--llm-model` / `COGENT_LLM_MODEL` enables `evaluate_joins`; `--judge-model`. Embeddings: `--embedder {ollama,hash}` (default `ollama`).
- Statements: `README.md:519` "The LLM stages are tested against scripted models and a mock Ollama server, never a real model."; `docs/tabledb.md:166` "…tested against scripted models and a mock Ollama server, not a real model: expect to tune prompts and thresholds."; `notes/MCP_Server.md:109` ("it hasn't been tried against … a real Ollama model …").
- Tests use `kg_llm.FakeLLM` (`src/kg_llm.py:116`, "Scripted model for tests") in `tests/test_llmjoin.py`, `test_mcp_tailoring.py`, `test_tabledb.py`, `test_telemetry.py`, and a mock Ollama server in `tests/test_telemetry.py:97`; `deploy/observability/demo/generate_traffic.py` runs a mock Ollama. No tracked log, `.jsonl` or fixture records a real-model run (`git ls-files` for `.jsonl`/logs: none).

**Raw:** `E16_llm_edges.txt`.

**Caveats:** I cannot rule out runs by the author that are not committed; I report only what the repository records.

---

## E17. OAuth and clients

**Finding:** the repository documents OAuth 2.1 as tested only against the server's own test client, not claude.ai; the exercised clients are the MCP Python client and `httpx`/Starlette test clients in the tests.

**Evidence** (`raw/E17_oauth_clients.txt`, `raw/E17_test_mcp_run.txt`)
- `docs/mcp.md:139-140`: "Custom-connector availability depends on your Claude plan, and this flow was tested against the server's own OAuth test client, not against claude.ai itself." `notes/LiveDemo.md:157`: "The OAuth flow was tested only against the server's own test client, not claude.ai. If the web app rejects the sign-in, note the error." `notes/MCP_Server.md:109`: "**Untested against real services:** it hasn't been tried against claude.ai, the real Cloudflare service, a real Ollama model, or a real OTLP collector (Jaeger, Tempo)."
- What is documented about the design: `docs/mcp.md:162-180` (OAuth 2.1 with dynamic client registration, PKCE S256, consent page, 5-attempt lockout; static bearer tokens still work), `src/kg_oauth.py`.
- `tests/test_mcp.py::test_oauth_flow` (lines 165-): `httpx.Client` against the server's endpoints — discovery, `/register` with `client_name: "Claude"` and `redirect_uris: ["https://claude.ai/api/mcp/auth_callback"]`, `/authorize` → `/consent` (wrong token rejected, right token accepted), `/token` with PKCE (wrong verifier → 400), then an MCP call with the issued token. In this environment `test_mcp.py` ran **13 passed** including `test_oauth_flow` (2026-11-09T03:29Z).
- Clients exercised, per the repository: the MCP Python SDK client (`streamablehttp_client` + `ClientSession`, `tests/test_mcp.py:24-25,141,222,367`, `tests/test_mcp_tables.py:19`), `httpx.Client` and Starlette `TestClient` (`tests/test_mcp.py:188,396`). Claude Code, Claude Desktop and the Claude API connector appear only as **setup instructions** (`README.md:112-115`, `docs/mcp.md:187-211`, `deploy/demo-tunnel.sh:45-50`), with no statement that they were run; the claude.ai web app is documented as not tested.

**Raw:** `E17_oauth_clients.txt`, `E17_test_mcp_run.txt`.

**Caveats:** `docs/mcp.md:176` says `X-API-Key` is not accepted while the module docstring `src/kg_mcp.py:10` says bearer tokens or `X-API-Key` are accepted (reported as found, not investigated).

---

## Summary table

| item | status | file(s) in `evidence/raw/` |
|---|---|---|
| E1 Test count | FOUND (341 collected / 291 passed / 46 skipped / 4 failed, 54.9 s; 42 skips = no pgvector) | E1_pytest_full.txt, E1_pytest_run2_failures.txt, E1_collect_only.txt, E1_def_test_per_file.txt |
| E2 MCP tool count | FOUND (24 default; 31 / 32 / 33 with table DB / LLM / SQL flags) | E2_a_default_tools.json … E2_d_tabledb_llm_sql_tools.json, E2_commands.txt |
| E3 Configuration counts | FOUND (30 standards, 15 reports; 4 + 1 paywalled; formats are free text) | E3_config_counts.txt |
| E4 2026-10-12 live run | PARTIAL (only `version_tracking` timestamps and sites; no report, no confidences; threshold in the initial commit, after the recorded fetches) | E4_search.txt, E4_config_version_tracking.txt, E4_config_history.txt, E4_threshold_history.txt, E4_confidence_values_in_code.txt |
| E5 PDF engines on a real document | NOT RUN (no real PDF; libraries installed) | E5_search.txt, E5_engine_availability.txt |
| E6 A real table | NOT FOUND (real tables); lookup NOT RUN; fixture tables only | E6_tables_FIXTURE.txt, E11_calls/31_lookup_values.json |
| E7 Strict vs exploratory | PARTIAL (profile settings compared; answers NOT RUN) | E7_strict_vs_exploratory_prefs.txt |
| E8 Edition information | PARTIAL (fixture `get_entry_metadata`; table tools NOT RUN) | E8_edition_fields.txt, E11_calls_agenthome/02_get_entry_metadata.json |
| E9 Notifications | FOUND (no outward channel; hooks, files, CLI/MCP only) | E9_notification_grep.txt, E9_alert_destinations.txt, E9_status_pending_FIXTURE.txt |
| E10 Reversibility | NOT FOUND (no revert command; archives and manual `tar` restore for orphans) | E10_reversibility_code.txt, E10_archive_example_FIXTURE.txt |
| E11 Provenance by tool | PARTIAL (20 tools observed on fixtures; 13 NOT RUN) | E11_provenance_matrix.md, E11_calls/, E11_calls_agenthome/, E11_code_table_wrap.txt |
| E12 Development history | FOUND (tester identity NOT FOUND; no files for rounds 1–5) | E12_git_authors.txt, E12_git_messages.txt, E12_rounds_grep.txt, E12_other_branch.txt |
| E13 Observability | FOUND (files and docs; no recorded results) | E13_observability.txt |
| E14 Graph schema | PARTIAL (fixture graphs only) | E14_graph_schema_FIXTURE.txt |
| E15 Embedding claim | PARTIAL (hash embedder only; Ollama NOT RUN) | E15_embedding_cosine.txt |
| E16 LLM-based edges | FOUND (defaults and flags; no real-model run recorded) | E16_llm_edges.txt |
| E17 OAuth and clients | FOUND | E17_oauth_clients.txt, E17_test_mcp_run.txt |
