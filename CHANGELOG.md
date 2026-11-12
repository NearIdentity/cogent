# Change log

Reconstructed on 2026-11-09 from the git history of `main` (to `1f237f0`, PR #10) and from the code and documents at that commit; earlier commits carried no change log. Newest first, by pull request. Dates are commit dates.
The README links here for the limits of each change; the statements below describe what the code and tests at `1f237f0` do, not results on real standards (see "Known limits").

## Unreleased: HTTP 403 handling (branch `claude/http403-fixes`)

Diagnosis: nrc.gov's CDN refuses clients it takes for bots (403 with `x-reference-error`); the agent posed as Chrome, retried, and then started headless Chromium, which made blocks last longer and filled the report with errors. Details and usage: `docs/fetching.md`.

* **Honest user agent** (`cogent-kg-agent/<version>`, `KG_USER_AGENT` override) on all HTTP paths; `kg_tools.fetch` now honours `KG_USER_AGENT` (it ignored it before). No `Referer`, no browser disguise, no automation-hiding Chromium flag.
* **No automatic browser fallback**; opt-in with `KG_BROWSER_FALLBACK=1`, once per request, never during a cooldown.
* **Persisted per-host cooldowns** (`<home>/state/hosts.json`): 403, 429 or 503+`Retry-After` start 6 h, doubling to a 7-day cap; no retries or polling inside a cooldown. Blocked documents and trackers are *deferred*: previous KG kept, no `consecutive_errors`, no STALE.
* **Per-host pacing** with jitter and `--host-interval HOST=SECONDS` (defaults: www.nrc.gov 4 s, adams-api.nrc.gov 2 s); work is interleaved across hosts.
* **`"manual": true` links** are never requested; the report lists the URL to download and the path to save to (alert `manual_download_needed`).
* **`kg_agent.py fetchcheck URL`**: one request, prints status, final URL after redirects, filter headers, page title, body snippet, user agent.
* **Reporting:** "blocked by a bot filter (HTTP 403 from host)"; 404/410 on trackers says to update `update_tracking_urls`; per-host "Fetch problems" section and `fetch_problems` / `docs_deferred` in the summary; info alerts are logged as INFO; a 403 on a paywalled store page is the info alert `store_page_blocked`, not an error; one image warning per host and run.
* **ADAMS Public Search adapter** (`src/kg_adams.py`, `NRC_ADAMS_API_KEY`): change detection for `nrc.gov/docs/ML...` links from the record's date added, document date and a content hash; the API text is not used for graphs. `kg_agent.py adamscheck ML...` prints a redacted summary. One request every 2 s; requests are counted in the summary.
* ADAMS change detection also covers `update_tracking_urls` entries that are `nrc.gov/docs/ML…` PDFs (entries 19 and 21 of `config/standards.json`); `TrackerState.adams_fp`.
* 404/410 on a tracker now says "not found" and that the host may be hiding the URL from this client (a first reading of the energy.gov 404 as client blocking was wrong; see the next item).
* New tests: `tests/test_fetch_blocking.py`, `tests/test_adams.py` (mock servers only).
* **ADAMS watch for manual links** (`"manual": true` on `nrc.gov/docs/ML…`): the record is read through the API each run; a change after the baseline raises `manual_document_changed` until the saved file is newer (`state/adams_manual.json`). **`deferred_too_long`**: documents on a blocked host not checked for `--deferred-warn-days` (default 7) days raise a warning, so deferral cannot hide a permanent block. Evidence (maintainer's machine): the 12 NRC PDFs staged on 2026-10-12 and checked without errors through 2026-10-27 return 403 from 2026-11-11 on (also to the headless-Chromium fallback), while NRC HTML pages were still fetched; on 2026-11-12 curl, Python and the honest agent all got 403 for a PDF from two networks while a browser loads it.
* **Cooldowns are scoped.** A 403 puts only its area of the host (first path segment, e.g. `www.nrc.gov/docs`) in a cooldown; 429 and 503+`Retry-After` still block the whole host; three refused areas block the host. Before this, one refused PDF deferred the host's HTML pages as well. Existing `hosts.json` entries without a `/` are whole-host blocks; delete them.
* Fixed a stuck document: with a stored ETag / Last-Modified but no KG on disk, the conditional GET answered 304 and the document was reported as an error with an empty message every run, never rebuilt (present since the first commit). Conditional headers are now sent only when the KG exists.
* Configuration: entry 13 (DOE O 5480.30, 1993) is now manual upload only (`"manual": true` on its link): per the project's research it was superseded by DOE O 420.1C (2012), itself superseded by DOE O 420.1D (2026, entry 11). The report lists the URL to download and the path `<home>/local/standards/13/` to save it to.
* Configuration: removed the dead tracker `https://www.energy.gov/ehss/nuclear-and-facility-safety-directives` (404 in Firefox as well, not a block) from `update_tracking_urls` and the `update_tracking` notes of entries 11, 12 and 13; their links and the active/archived directives-library trackers were checked with `fetchcheck` and work.
* **Not verified:** nothing was run against the live nrc.gov, adams-api.nrc.gov, ascelibrary.org or webstore.ansi.org (see `docs/fetching.md`, "What was not verified").

## PR #9 and #10 (2026-10-27): test round 6 and its review

Fixes for the MCP client's fix list of 2026-10-26 (ids R6-01 to R6-15) and for the reviews of the first fixes. `tests/test_round6.py` holds the regression tests.

* **Table lookup confidence.** `lookup_values` splits identifier-like terms of a question into *subjects* (`A572`, `SA-516`, `Cs-137`, `RG 1.233`) and *category labels* (`HC-3`, `SDC-2`, ...). At least one subject must occur in a returned row, its columns, caption or section, checked cell by cell; labels only rank rows (`HC-3` also matches "Hazard Category 3"). Otherwise the result is `no_confident_match: true` with a message and the closest rows under `nearest_neighbours`; `match_quality` is `lexical`, `partial`, `semantic`, `weak` or `none`. Bare document numbers (`1.4` from `RA-S-1.4`) match only as whole tokens. A similarity floor is the preference `min_similarity` (profile `strict`: 0.70, provisional and not calibrated; others off) and `COGENT_LOOKUP_MIN_SIMILARITY`.
* **Extraction recipe.** Every document KG carries `EXTRACTION_RECIPE` (`src/kg_models.py`, currently 10). A normal run rebuilds once each document KG built with an older recipe even though its text is unchanged (`--no-recipe-refresh` opts out); entries re-assemble and changed tables reload. `get_overview`, `list_entries` and `get_entry_metadata` report entries and documents that are behind.
* **Link scope.** A link in `config/*.json` may carry `scope` (a heading pattern) and `scope_end`; only that part of the page enters the document KG. Entries that give different scopes for one URL are reported as an error. (The committed configuration does not use `scope` yet.)
* **Join inference** (`kg_relmap.VERSION` 3). Placeholder headers (`column_N`, `c_N`) and headers that are only list numbers carry no name evidence; list markers, table references and columns that repeat on both sides are not keys; value-only joins need 5 or more shared text values, 80 % containment and one unique side; a header or first cell that names the other table adds evidence.
* **Non-data tables.** Front-matter detection also flags `index` and `store` tables (not exported to the table database) and no longer produces row triples for contents, glossary, index or shop tables; header-fragment captions are replaced by the section heading and a `Table N-M` label hidden in a garbled caption is recovered. Index detection needs the caption or section to *be* an index.
* **Entities and identifiers.** New identifier family `ANS` (`ANSI/ANS-2.26-2004`, dotted numbers only; editions not tracked). Singular and plural forms are one entity (aliases kept); an acronym and its expansion are one `abbreviation_of` edge, hyphenated expansions included (`DBA` / "Design-Basis Accident"); tables link to the entities of their caption, header and first column with `MENTIONS` edges.
* **Errors.** An unknown entry says so and lists the valid keys; `get_node` suggestions need a similarity of 0.8 and say "no close node id" otherwise, also on `combined`.
* **Run report.** `tabledb.dropped_tables` lists the ids of dropped tables.
* **Documentation.** This file; README, `docs/` and `notes/` brought in line with the code (option lists, metric and file names, test counts, session store).
* Upgrade note: **the first run after this change rebuilds every staged document KG** (re-download, re-parse, OCR where used), so it takes about as long as a forced run; run it with `--tables` so the table database follows.

## PR #8 (2026-10-24 to 2026-10-26): observability

* `src/kg_telemetry.py`: OpenTelemetry tracing (optional SDK; spans for agent steps, fetches, LLM and embedding calls, join evaluation, the daemon run and every MCP tool and HTTP request) and a stdlib Prometheus registry; the agent persists its run metrics in `<home>/metrics/`, the server's `/metrics` (`--metrics`) merges them, `--pushgateway` pushes them.
* Metrics: latency p50/p95, tokens and cost (`COGENT_LLM_PRICES`), findings auto-applied / needs review / human-resolved, judge agreement, a heuristic hallucination rate, failures and retries.
* `deploy/observability/`: Prometheus config, the alert `CogentAgentFailureRateHigh` (with a promtool test), Grafana provisioning and the 29-panel dashboard, a compose file, a demo traffic generator; `docs/observability.md`, `docs/img/grafana-dashboard.png`.
* Not verified: OTLP export to a real collector; the Pushgateway only against a stub; the screenshot shows synthetic traffic (mock site, mock model).

## PR #7 (2026-10-24): scale-out

`kg_mcp.py serve`: `--workers`, `--graph-cache`, `--session-store memory|sqlite:<file>|redis://...`, `--embed-cache`, `--embed-concurrency`, `--embed-wait`, `--rate-limit` / `--rate-burst`, `--metrics`, `--state-dir`. `src/kg_snapshot.py` (`publish`, `pull`, `watch`, `status`) and `kg_agent.py run --publish-snapshot`. Named Cloudflare tunnel: `deploy/named-tunnel.sh`, `deploy/cogent-tunnel.service`, `deploy/cogent-mcp.service`. `docs/scaling.md`. Not verified: the Cloudflare commands and Redis against real services.

## PR #6 (2026-10-20 to 2026-10-21): table database and MCP table services

* `src/kg_tabledb.py`: tables from staged documents loaded into PostgreSQL + pgvector (typed rows, whole-row embeddings, inferred joins, data dictionaries and relationship maps); MCP tools `list_tables`, `describe_table`, `search_tables`, `lookup_values`, `query_table`, `join_tables`, `explain_units`, optional `evaluate_joins` and `run_sql`, `get_data_dictionary`, `get_relationship_map`, `get_join_evaluation`.
* LLM join inference with an LLM judge (`--llm-joins`), unit deduction (strict / broad / off) and per-session and per-call tailoring (profiles `fast`, `balanced`, `strict`, `exploratory`, `llm`).
* Fixes from the first rounds of MCP client testing (`33c0ed1`, `fe12f9a`, `dc6e56e`): table database health check, local paths removed from results, edition read from file names, paged `get_table`, tolerant node ids, search `match` modes, identifier lists (`RGs 1.200 and 1.247`), relation vocabulary, one canonical target per cited identifier in the combined graph, footnote markers split from PDF table cells, repeated header removal, table validity classifier, column-aware reading order, `endorsed_editions` warnings, and a fix for `--only` runs closing other entries' warnings.
* Database passwords are no longer in the repository: copy `deploy/tabledb.env.example` to `deploy/.env`.

## PR #5 (2026-10-19): forced refresh

`kg_agent.py run --force [--no-prune]`: re-read every tracker page and document without conditional requests, rebuild the KGs, log to `logs/forced.jsonl`, archive orphaned staging data.

## PR #4 (2026-10-19): extracted figures

Images are sniffed by content and converted to PNG; no `.bin` files are produced; non-image content is listed under `skipped` in the figure manifest.

## PR #2 and earlier (2026-10-12 to 2026-10-19): agent, daemon and MCP server

Initial commit (2026-10-12): the KG staging agent (trackers, auto-apply at confidence 0.8, review queue, document and entry KGs, version tracking, alerts, report). Data staging daemon (`kg_daemon.py`, 2026-10-13). MCP server with bearer-token and OAuth 2.1 authentication, graph tools, figure and table assets with paywall handling (PR #2, 2026-10-19).

## Known limits

* Extraction and table heuristics were tested on constructed examples and hand-built documents, not on the scanned ASCE 43 / NQA-1 documents; no OCR-based result is verified.
* The LLM stages were tested against scripted models and a mock Ollama server, never a real model; the judge's verdicts are evidence, not ground truth. The hallucination rate and judge agreement on the dashboard are heuristics.
* The OAuth flow was tested against the server's own test client, not claude.ai.
* No run report of a live run is committed; the committed `version_tracking` blocks record dates and editions only.
