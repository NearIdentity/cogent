# Follow-up evidence run (F0 to F12)

Collected 2026-11-09 (UTC) from 05:14Z. Rules as before: no source, test, config or documentation file was changed; everything written is under `evidence/followup/` (raw output in `evidence/followup/raw/`); no secrets were printed.

## Read this first: this is not the presenting machine

The prompt says this run is on the presenter's machine with the real staged data, PostgreSQL + pgvector, Ollama, the MCP server and Grafana. **What I observed says otherwise.** This is the same kind of cloud container as the earlier evidence run:

- `uname -a`: `Linux vm 6.18.44-fc-v310 … x86_64`, Ubuntu 24.04.5, hostname `vm`, user `root`; boot time 2026-11-09 05:14:25 (`uptime -s`) (`raw/F1_probe.txt`, `raw/F1_uptime.txt`).
- The scratch directory of the earlier run is still here (`/tmp/claude-0/-home-user-cogent/cd99a80a-…/scratchpad/` with the fixture homes and the virtualenv I created then).
- **No Ollama** (binary absent, `127.0.0.1:11434` no response), **no pgvector** (`CREATE EXTENSION vector` fails, F1), **no Prometheus, no Grafana** (ports 9090/3000 no response; Docker daemon not running), **no MCP server** (port 8765 no response), no listening ports at all, no `~/.bash_history`.
- **No real staged data**: no directory with `config/ state/ logs/ staging/ archive/` other than my own earlier fixture homes (F3), and no real PDF (F4).

So almost every item that needs the real data or services is **NOT RUN / NOT FOUND**, and I have not substituted fixture results for them. What could be done here honestly was done: F0, F1, F2 (including the pymupdf version test), F8 (the documented demo command line, labelled), F10 and F12 (the parts that come from files in the repository). If you run the same prompt on the presenting machine, the raw commands in each section can be reused.

---

## F0. Repository state

**Finding:** the checkout is `documentation-update` at `d602091`, which is an ancestor of main; `origin/main` is `0394a9c`, matching the prompt; `git log 0394a9c..HEAD` is empty.

**Evidence** (`raw/F0_repo_state.txt`, 2026-11-09T05:14:53Z): `HEAD: d602091b99d7d0e7bb615a6dd823c3a9b37d1851`, `branch: documentation-update`, `git status --short`: `?? evidence/` (this report's directory only). `origin/main: 0394a9c8c78f8e3055dedb1be3d9b1e50f42f02d` ("Merge pull request #11 from NearIdentity/documentation-update"; its parents are `1f237f0` and `d602091`). `git log --oneline 0394a9c..HEAD` printed nothing (HEAD is contained in main). All tests and probes ran in a detached worktree at exactly `0394a9c` (`raw/F1_environment.txt`: `worktree HEAD: 0394a9c8c78f8e3055dedb1be3d9b1e50f42f02d`, clean `git status`). This report is committed on `claude/pdf-html-knowledge-graph-773a5d`.

**Raw:** `F0_repo_state.txt`. **Caveats:** the feature branch itself does not contain the main commits after `1f237f0` (`CHANGELOG.md` and the documentation update); results refer to the `0394a9c` worktree.

---

## F1. Environment

**Finding:** Ubuntu 24.04, Python 3.13.18, PostgreSQL 16.15 server present but **pgvector is not installed** (`CREATE EXTENSION vector` fails); the OpenTelemetry SDK is not installed.

**Evidence** (`raw/F1_environment.txt`, `raw/F1_postgres_vector.txt`)
- OS `Ubuntu 24.04.5 LTS`, kernel 6.18.44-fc-v310, Python 3.13.18 (`/usr/local/bin/python3`).
- Versions in the virtualenv used for the runs (`pip list`): pymupdf 2.1, pymupdf_layout 2.1, pymupdf4llm 2.1, pytest 9.2.0, mcp 1.30.0, psycopg 3.3.8 (+ psycopg-binary 3.3.8), opentelemetry-api 1.45.0, langgraph 1.2.20, networkx 3.7, rapidocr-onnxruntime 1.2.3.
- OpenTelemetry SDK: `ModuleNotFoundError: No module named 'opentelemetry.sdk'` (only the API is installed). Python package `pgvector`: not installed (`ModuleNotFoundError`).
- PostgreSQL: `postgres (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)`. A private temporary cluster was started and stopped for the test: `select version()` worked; `pg_available_extensions` has no `vector`; `CREATE EXTENSION vector` → `ERROR: extension "vector" is not available … Could not open extension control file "/usr/share/postgresql/16/extension/vector.control": No such file or directory`.

**Raw:** `F1_probe.txt`, `F1_environment.txt`, `F1_postgres_vector.txt`. **Caveats:** this is the container's environment, not the presenting machine's.

---

## F2. Tests

**Finding:** 341 collected, 291 passed, 46 skipped, 4 failed in 63.8 s; the same 4 `test_table_footnotes.py` tests fail with pymupdf 2.1 and with 2.0 and **pass with pymupdf 1.26.7**.

**Evidence** (`python -m pytest tests -q -rfEs -p no:cacheprovider --tb=line` in the `0394a9c` worktree, started 05:15:40Z, finished 05:16:47Z; `raw/F2_pytest_full.txt`, `raw/F2_collect.txt`)
- `341 tests collected in 2.35s`; `4 failed, 291 passed, 46 skipped, 5 warnings in 63.82s (0:01:03)`.
- Skips: 42 × `pgvector is not available: extension "vector" is not available`; 3 × `OpenTelemetry SDK not installed`; 1 × `could not import 'opentelemetry.sdk.trace': No module named 'opentelemetry.sdk'`.
- Failures: `test_superscript_markers_are_split_from_values`, `test_exponents_and_unit_powers_are_not_footnotes[1.0 x 10-4]`, `[Area in m-2]`, `[Dose in cm-3]` — **the same four as before; they fail here too** with pymupdf 2.1.
- *pymupdf version check* (throwaway virtualenv `scratchpad/venv_pm126`, outside the repository; pytest, networkx, langgraph, beautifulsoup4, lxml, pydantic, langchain-core, pillow, reportlab, mcp, httpx, uvicorn, psycopg installed, then only `pymupdf` swapped; `requirements.txt` untouched). Only `tests/test_table_footnotes.py` was run:

| pymupdf | 6 tests in the file | the 4 earlier failures |
|---|---|---|
| 2.1 (in use) | 4 failed, 2 passed (full-suite run above) | all 4 fail |
| 2.0 (newest older release) | `4 failed, 2 passed` (`raw/F2_footnotes_pymupdf_2.0_cases.txt`) | all 4 fail |
| 1.26.7 (newest 1.26.x) | `6 passed` (`raw/F2_footnotes_pymupdf_1.26.7_cases.txt`) | all 4 pass |

**Raw:** `F2_pytest_full.txt`, `F2_collect.txt`, `F2_started.txt`, `F2_finished.txt`, `F2_footnotes_pymupdf_2.0_cases.txt`, `F2_footnotes_pymupdf_1.26.7_cases.txt`, `F2_pymupdf_versions.txt`, `F2_install_*.txt`.

**Caveats:** the 42 skipped table-database tests never ran, because there is no pgvector here. The version test used a reduced dependency set (no pymupdf4llm / pymupdf_layout), which is enough for that file. I did not investigate why 2.0 and 2.1 behave differently, and did not test 1.27.x/1.28.x.

---

## F3. The Oct 12 run

**Finding:** **NOT FOUND.** No agent home from a live run exists on this machine.

**Evidence** (`raw/F3_search.txt`): `find /` for `staging` directories returns only my own earlier fixture homes (`…/scratchpad/fixture/home`, `…/scratchpad/fixture_tables/home`) and pytest temporary directories; no `kg_agent_home`, no `pending.json`, `changes.jsonl` or `runs.jsonl` outside tests and those fixtures. No shell history file (`~/.bash_history`, `~/.zsh_history` absent), no cogent systemd unit, no cron entry (`/etc/cron.d`: `e2scrub_all`, `php`; `crontab` not installed). So `logs/report.md`, `logs/runs.jsonl`, `state/pending.json`, `state/alerts.json`, `config/changes.jsonl` of the live run: **not found**; no findings, confidences or auto-applied/queued counts can be reported.

**Raw:** `F3_search.txt`. **Caveats:** the real home is presumably on the presenting machine.

---

## F4. Real PDF, two engines

**Finding:** **NOT RUN** — no real standard PDF and no staging folder exist on this machine.

**Evidence** (`raw/F4_search.txt`): `find /` for `*.pdf` outside site-packages, venvs and pytest temp returns one file, `/usr/lib/libreoffice/share/xpdfimport/xpdfimport_err.pdf` (not a standard); no PDF larger than 200 KB exists anywhere. Both engines are importable here (`pymupdf.layout importable`; pymupdf4llm 2.1), so only the document is missing. No page counts, node counts or furniture lists were produced.

**Raw:** `F4_search.txt`, `F1_environment.txt`.

---

## F5. Real tables

**Finding:** **NOT RUN / NOT FOUND** — there is no table database (no pgvector) and no real standard staged; no A572 check against real data was possible.

**Evidence:** `CREATE EXTENSION vector` fails (F1), so the table database cannot exist here; no real staging folder (F3). Nothing from fixtures is reported for this item.

**Raw:** `F1_postgres_vector.txt`, `F3_search.txt`.

---

## F6. Lookup under both profiles

**Finding:** **NOT RUN** — no real MCP server, no table database and no Ollama are available here, so `lookup_values` was not called for either profile.

**Evidence:** nothing is listening on 8765 (`raw/F10_F12_config_and_services.txt`); Ollama absent (`F1_probe.txt`); pgvector missing (`F1_postgres_vector.txt`). I did not use the fixture servers from the earlier run for this item. For the *question* itself ("What is the allowable stress for ASTM A572? Quote the source."), the code-level behaviour on main (not an observation) is that `assess_lookup` takes the subject token `A572` from the question and returns `no_confident_match` when no returned row contains it (`src/kg_tableservice.py`); I did not execute this against real rows.

**Raw:** `F10_F12_config_and_services.txt`, `F1_probe.txt`.

---

## F7. Provenance of table tools

**Finding:** **NOT RUN** — none of `list_tables`, `describe_table`, `explain_units`, `search_tables`, `query_table`, `join_tables`, `get_relationship_map` (or `evaluate_joins`) could be called against a real table database.

**Evidence:** same reasons as F6. (The earlier run's code-level reading of `_wrap` is in `evidence/raw/E11_code_table_wrap.txt` on the feature branch; it is not repeated here as an observation.)

| tool | provenance | source (doc/page/caption) | warnings | paywall flags | disclaimer |
|---|---|---|---|---|---|
| list_tables, describe_table, explain_units, search_tables, query_table, join_tables, get_relationship_map | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN |

---

## F8. Tool count as the demo will run

**Finding:** **PARTIAL.** I do not have the presenter's actual command line; with the command documented in `notes/LiveDemo.md` step 5 (plus `COGENT_TABLEDB_DSN` set, as the demo does) the server advertises **31** tools, with `--allow-sql` **not** set and `--llm-model` **not** set.

**Evidence** (`raw/F8_tools.json`, `raw/F8_server.log`, 2026-11-09T05:16Z): `COGENT_TABLEDB_DSN=postgresql://cogent_ro@127.0.0.1:1/cogent` (a closed port, no password) `python src/kg_mcp.py --home <empty temp home> serve --host 127.0.0.1 --port 18820 --public-host '*' --oauth-issuer https://example-demo.trycloudflare.com --embedder ollama --embed-model nomic-embed-text --metrics`. tools/list → `count 31`: get_overview, list_entries, get_entry_metadata, get_dependents, get_warnings, get_disclaimers, graph_summary, get_outline, search_graph, get_node, get_neighbors, get_subgraph, get_table, list_assets, get_figure, get_table_csv, list_profiles, set_session_preferences, get_session_preferences, reset_session_preferences, get_data_dictionary, get_relationship_map, get_join_evaluation, list_tables, describe_table, explain_units, search_tables, lookup_values, query_table, join_tables, export_graph. `evaluate_joins` appears only with `--llm-model`, `run_sql` only with `--allow-sql`.

**Raw:** `F8_tools.json`, `F8_server.log`, `F8_client_err.txt`. **Caveats:** empty home, unreachable database; the count reflects registration by flags, not a working demo. If the presenter's line adds `--llm-model` the count is 32, with `--allow-sql` as well 33 (earlier run, E2).

---

## F9. Embedding claim

**Finding:** **NOT RUN** — Ollama is not installed or running, so no cosine similarity with `nomic-embed-text` (or any real embedder) was computed.

**Evidence:** `ollama` binary not found; `127.0.0.1:11434` → HTTP 000 (`raw/F1_probe.txt`, `raw/F10_F12_config_and_services.txt`). The hash-embedder numbers of the earlier run are not repeated and are not evidence about real embedders.

---

## F10. Observability on the real machine

**Finding:** **PARTIAL.** Prometheus, Grafana and the MCP server are not running here (NOT RUN: targets, `/metrics`, `cogent_mcp_tool_calls_total` labels); the configured values come from the repository files: scrape interval 15 s, dashboard default range last 30 minutes, refresh 10 s.

**Evidence** (`raw/F10_F12_config_and_services.txt`)
- Services: `docker ps` → "failed to connect to the docker API at unix:///var/run/docker.sock … daemon is running"; `127.0.0.1:9090/-/ready` → HTTP 000; `127.0.0.1:3000/api/health` → HTTP 000; `127.0.0.1:8765/healthz` → HTTP 000. No targets to report; no token was created and `/metrics` was not called (F6 did not run, so there are no tool-call counters to show).
- `deploy/observability/prometheus/prometheus.yml` (`0394a9c`): `global: scrape_interval: 15s`, `evaluation_interval: 15s`; job `cogent`, `metrics_path: /metrics`, bearer token from `/etc/prometheus/mcp_token`, target `127.0.0.1:8765`, label `instance: cogent-mcp`.
- `deploy/observability/grafana/dashboards/cogent-observability.json`: title "cogent: agent and MCP server", `time {'from': 'now-30m', 'to': 'now'}`, `refresh '10s'`, 29 panels.

**Raw:** `F10_F12_config_and_services.txt`. **Caveats:** these are the repository's defaults; whether the presenting machine's Prometheus uses them is unknown.

---

## F11. Edge types on real graphs

**Finding:** **NOT RUN** — no real document graph or combined graph exists here (F3, F4). Counts from the earlier fixture graphs are not repeated.

---

## F12. Extraction recipe status

**Finding:** **PARTIAL.** The code's value is quoted below; the recipe recorded in staged data, the number of entries behind it and whether a rebuild is pending **cannot be reported** (no staged data, no server).

**Evidence** (`raw/F10_F12_config_and_services.txt`, worktree `0394a9c`): `src/kg_models.py:17: EXTRACTION_RECIPE = "10"`. No staged state exists (`state.json` per document, `meta.json`, `graph.extraction_recipe` in `get_entry_metadata`): NOT FOUND (F3). `get_overview` / `list_entries` could not be called against real data. For reference only (code, not an observation): a document whose `state.json` lacks `extraction_recipe` or has another value is treated as behind, and the next normal run rebuilds it once (`src/kg_agent.py`, `_sync_one`; `Store._docs_behind` in `src/kg_store.py`).

**Raw:** `F10_F12_config_and_services.txt`.

---

## Status table

| item | status | raw files (in `evidence/followup/raw/`) |
|---|---|---|
| F0 Repository state | FOUND | F0_repo_state.txt |
| F1 Environment | FOUND (container environment; pgvector and OTel SDK absent) | F1_probe.txt, F1_environment.txt, F1_postgres_vector.txt |
| F2 Tests | FOUND (4 failures reproduce on pymupdf 2.1 and 2.0; pass on 1.26.7) | F2_pytest_full.txt, F2_footnotes_pymupdf_*_cases.txt |
| F3 The Oct 12 run | NOT FOUND | F3_search.txt |
| F4 Real PDF, two engines | NOT RUN (no real PDF) | F4_search.txt |
| F5 Real tables | NOT RUN | F1_postgres_vector.txt |
| F6 Lookup under both profiles | NOT RUN | F10_F12_config_and_services.txt |
| F7 Provenance of table tools | NOT RUN | – |
| F8 Tool count for the demo | PARTIAL (documented command, 31 tools; actual demo line unknown) | F8_tools.json, F8_server.log |
| F9 Embedding claim | NOT RUN (no Ollama) | F1_probe.txt |
| F10 Observability | PARTIAL (config values from files; services not running) | F10_F12_config_and_services.txt |
| F11 Edge types on real graphs | NOT RUN | – |
| F12 Extraction recipe status | PARTIAL (code value only) | F10_F12_config_and_services.txt |
