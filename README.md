# `cogent`: documents → knowledge graphs, kept up to date

`cogent` fetches and stages the contents of the regulatory and reporting frameworks a (hypothetical) licensing engineer in
the nuclear-reactor space has to work with: 10 CFR, NRC regulatory guides, NEI guidance, DOE orders and standards, and
consensus standards. It turns each PDF / HTML / eCFR document into a **knowledge graph (KG)**, stages one KG for every
standard and every report format listed in two JSON configuration files, and runs a **daily update agent** that detects new
editions, refreshes the KGs, archives the old ones, updates the configuration and appends a report of what it did.

It prefers local tools (LangGraph, Pydantic, Ollama via LangChain, PyMuPDF, RapidOCR/Tesseract) and keeps free-text
processing to a minimum: update detection is done by hard-coded parsers and trackers, not by a language model.

* [Repository layout](#repository-layout)
* [Installation](#installation)
* [Quick start](#quick-start)
* [The configuration files](#the-configuration-files)
* [Building a KG from one document](#building-a-knowledge-graph-from-one-document) (`kg_ingest.py`)
* [Viewing a KG](#viewing-a-kg) (`kg_show.py`)
* [The update agent](#the-update-agent) (`kg_agent.py`)
* [Paywalled documents](#paywalled-documents-warnings) · [Version tracking](#version-tracking-in-the-configuration) · [OCR](#ocr-for-scanned-pdfs)
* [Scheduling](#scheduling) · [Environment variables](#environment-variables) · [Tests](#tests) · [Status and limits](#status-and-limits)

## Repository layout

```
config/standards.json  reports.json     the document configurations (30 standards, 15 report formats)
src/kg_ingest.py     PDF / HTML / eCFR-XML (file or URL) → networkx KG; extractors; export/save/load; assets
src/kg_ocr.py        OCR for scanned PDF pages (RapidOCR / Tesseract engines, page → headings/paragraphs/tables)
src/kg_show.py       interactive HTML (vis-network) and static PNG viewer for a KG
src/kg_daemon.py     the daemon: schedule, catch-up, retries, supervised runs, signals, start/stop/status, systemd notify
src/kg_agent.py      the daily agent: LangGraph workflow, staging, archive, config edits, version tracking, alerts, report
src/kg_assets.py     PNG conversion, downscaling and copyright watermark for figures served over MCP
src/kg_combine.py    secondary staging: combined entry-level graph + dependents index (reports <-> standards)
src/kg_tools.py      the agent's hard-coded tools: conditional fetch, fingerprints, identifier parsers, trackers, KG merge/diff
src/kg_models.py     Pydantic models (configuration views, state, findings, alerts, version_tracking block)
deploy/              cogent-run.{service,timer} (systemd timer), cogent-daemon.service (systemd), cogent.cron (cron)
docs/scheduling.md   daemon vs. cron vs. systemd timer: comparison and recommendation
tests/               test_agent.py, test_ocr.py, test_daemon.py + mock_site.py (a local mock of the sources); no internet needed
requirements.txt     dependencies (optional ones are commented)      venv-setup.sh   uv-based virtualenv setup
```

All commands below are run from the repository root so the default `config/…` paths resolve.

## Installation

Python 3.10 or newer (developed on 3.12 / 3.13).

```bash
uv venv --python 3.12 cogent && source cogent/bin/activate          # or see venv-setup.sh; plain venv + pip works too
uv pip install -r requirements.txt
playwright install chromium                                          # headless browser for sites that block scripted downloads
```

| optional piece | for | how |
|---|---|---|
| `pymupdf-layout`, `pymupdf4llm` | layout-aware PDF parsing (falls back to plain PyMuPDF if absent). AGPL-3.0 or Artifex commercial licence | `requirements.txt` |
| `playwright` + Chromium | URL downloads through a headless browser (default `--fetch browser`); falls back to plain HTTP if absent | `playwright install chromium` |
| `rapidocr-onnxruntime` **or** the Tesseract program | OCR of scanned pages | `pip` / `apt install tesseract-ocr` |
| `pyvis`, `matplotlib` | offline vis-network library for `kg_show.py` (else a CDN `<script>`), PNG output | `requirements.txt` |
| Ollama + `langchain-ollama` | `--extractor ollama\|hybrid` (triples) and `--llm` (report narration) | `ollama pull llama3.1` |
| `pytest`, `reportlab` | the tests | `requirements.txt` |

## Quick start

```bash
# one document → KG (file or URL), saved under kg_store/<sha256 of the input>.json
python src/kg_ingest.py https://www.nrc.gov/docs/ML1924/ML19241A472.pdf
python src/kg_ingest.py paper.pdf out.graphml --assets-dir kg_assets      # + figures (PNG) and tables (CSV) as files; a manifest.json lists them

# look at it
python src/kg_show.py https://www.nrc.gov/docs/ML1924/ML19241A472.pdf --open

# the daily agent
python src/kg_agent.py run --dry-run --only standard:22,report:1       # check only, prints the report, writes nothing
python src/kg_agent.py run                                             # stage / update everything under ./kg_agent_home
python src/kg_agent.py status                                          # staged documents, version table, open warnings
python src/kg_agent.py probe https://www.nrc.gov/regulations-legislation/regulatory-guides-by-division/division-1---power-reactors
python src/kg_daemon.py start --at 06:00 --tz Europe/London -- --extractor rules   # detached daemon; status | run-now | stop
```

## The configuration files

`config/standards.json` (`{"meta": …, "standards": [...]}`) and `config/reports.json` (`{"meta": …, "reports": [...]}`).
Each entry is a standard or a report format; the agent reads these fields:

| field | meaning |
|---|---|
| `id`, `document` / `report` | numeric id and name. The name carries the edition (`NRC RG 1.233`, `DOE-STD-1271-2025, …`, `ASME NQA-1-2026, …`) |
| `links` / `template_links` | `[{"text", "url"}]`: the documents to ingest (PDF, HTML or an eCFR page) |
| `update_tracking_urls` | pages watched for new editions (see [trackers](#the-update-agent)) |
| `standards_to_comply_with_ids` | reports → ids of the standards they must comply with (becomes `complies_with` edges) |
| `paywalled` (standards) | `true` if the document must be bought (see below); also inferred from "paywalled" in `format_of_data` |
| `local_copy` | optional file / directory / glob of a purchased copy (default `<home>/local/<standards\|reports>/<id>/`) |
| `overlaps_with` | `[{"id", "scope", "note"}]`: entries whose text overlaps (e.g. 28 ↔ 1 on 10 CFR 50 Appendix B); becomes an `overlaps_with` edge |
| `previous_versions` | superseded editions, kept for reference but not staged |
| `version_tracking` | **written by the agent**: latest edition available vs. what is staged (see below) |

Other keys (`description`, `group`, `format_of_data`, …) are kept untouched. The agent edits these files only in the
ways described below, and every edit is logged.

## Building a knowledge graph from one document

`kg_ingest.file_to_knowledge_graph(path_or_url, extractor=None, figure_captioner=None, assets_dir=None, doc_label=None)`
returns a `networkx.MultiDiGraph`. It is a LangGraph pipeline `load → structure → [export] → extract → build`:

* **load** – PDF (layout-aware parser, else basic PyMuPDF, then OCR for pages without text), HTML (BeautifulSoup) or eCFR
  versioner XML → ordered `Element`s: headings (with levels), paragraphs, tables (rows), figures (+ captions). URLs are read
  through a headless browser by default (`--fetch browser|http|auto`; HTML is the rendered DOM) with a plain-HTTP fallback.
* **structure** – every element gets its enclosing section from the heading hierarchy.
* **export** (with `assets_dir`) – figures as PNG, tables as CSV, plus a `manifest.json`, under `<assets_dir>/<sha256(source)>/`.
* **extract** – triples from text, tables and captions: `RuleBasedExtractor` (default, no dependencies), `OllamaExtractor`
  (local model, JSON-schema output, rule-based fallback), `HybridExtractor` (LLM first, rules fill the gaps), or any
  `prompt → str` function via `LLMExtractor`. Table rows become *row key → column header → value* facts.
* **build** – merges duplicate entities, then emits the graph:
  node types `Document, Heading, Paragraph, Table, Figure` and extracted entities;
  edges `CONTAINS` (hierarchy), `NEXT` (reading order), `MENTIONS`, and the extracted relations.

The agent additionally adds deterministic links: `CITES` edges to identifier nodes (RG / NEI / DOE / NUREG / NQA / CFR
identifiers found in the text) and `references` edges between `§ 50.x` sections of eCFR documents.

Other public helpers: `export_graph`, `save_knowledge_graph` / `load_knowledge_graph` (files named by the SHA-256 of the
source path or URL, in `$KG_OUTPUT_DIR` or `./kg_store`), `export_assets`, `export_tables_from_graph`, `make_extractor`,
`set_fetch_mode`, `set_pdf_engine`, `set_ocr`.

```python
from kg_ingest import file_to_knowledge_graph, make_extractor, save_knowledge_graph
kg = file_to_knowledge_graph("https://www.nrc.gov/docs/ML1924/ML19241A472.pdf",
                             extractor=make_extractor("hybrid", model="llama3.1"), assets_dir="kg_assets")
save_knowledge_graph(kg)
```

## Viewing a KG

`python src/kg_show.py <saved .json/.graphml | PDF/HTML path or URL> [-o kg.html] [--png kg.png] [--open]`

A single self-contained HTML page (vis-network): search, node-type and edge-group filters (hierarchy / reading order /
mentions / extracted relations), counts, and a details panel with every attribute of the clicked node or edge (tables are
rendered as tables) so a fact can be traced back to its source element and page. `--png` writes a static matplotlib image.

## The update agent

`python src/kg_agent.py run` runs this LangGraph workflow once:

```
init → track → apply → sync_docs → assemble → versions → alerts → finalize
```

1. **init** – validates both JSON files (Pydantic), takes a lock (`<home>/.lock`), records a snapshot, and logs any edit
   made to the configuration outside the agent since the last run.
2. **track** – every `update_tracking_urls` page goes through a hard-coded tracker:
   * **eCFR versions** (`…/api/versioner/v1/versions/title-N.json?part=P`): latest issue date and *which sections were amended*;
   * **Federal Register** (`federalregister.gov/agencies/<slug>`): new rules / proposed rules via the JSON API, matched to
     entries by CFR part;
   * **identifier watcher** (any other page: NRC regulatory-guide lists, DOE standards and directives libraries, NRC
     pages, publisher pages): finds the table rows / list items that mention the entry's identifiers and compares the
     edition shown with the one on record. Families: `RG`, `NEI`, `DOE-STD`/`NE-STD`, `DOE O`, `ASCE/SEI`, `QME`, `NQA`,
     `RA-S`, `NUREG`, `10 CFR`. Editions compare numerically (revisions, years, order letters); `43-19` equals `43-2019`.
     A higher edition with a link in the same row is a `new_edition` finding.
3. **apply** – a `new_edition` finding with a link and confidence ≥ 0.8 rewrites the entry's link (URL and text) and the
   edition in its name, and every other entry that shared the old URL (e.g. reports). Lower-confidence findings go to
   "Pending (needs human review)" and are retried on later runs. `--no-auto-apply` never edits the configuration.
4. **sync_docs** – each unique document URL (or local copy) is fetched conditionally (ETag / Last-Modified) and
   fingerprinted by *content*: PDF text (page images for scans), visible HTML text without volatile page furniture,
   eCFR XML. A re-rendered file or a changed footer is not an update. eCFR pages are read from the versioner XML API
   instead of the JavaScript page. A changed document is re-ingested; its previous KG is **archived**; a structured diff
   (modified / new / removed sections, entity counts) is recorded.
5. **assemble** – the KG of each standard / report whose documents (or links, name, compliance / overlap lists) changed is
   rebuilt by merging its document KGs (shared entities connect them) with `HAS_DOCUMENT`, `complies_with` and
   `overlaps_with` edges; the old entry KG is archived.
6. **versions** – writes the `version_tracking` block of every entry (below).
7. **alerts** – warnings for paywalled documents (below).
8. **finalize** – persists state and **appends** a section to `logs/report.md`: summary, warnings, version status,
   configuration changes, document KGs created / updated (with diffs), entry KGs rebuilt, tracker findings, pending
   review, errors, trackers that keep failing. Optionally a local LLM (`--llm`) narrates the structured results at the top.

Subcommands: `run [--dry-run] [--force]`, `daemon --at HH:MM`, `probe <url>` (run one tracker and print what it parsed: use this to
check a tracker against a live page), `status`. Common options: `--standards`, `--reports`, `--home` (default
`kg_agent_home`), `--only standard:22,report:1`, `--extractor rules|ollama|hybrid`, `--model`, `--llm`, `--no-auto-apply`,
`--no-assets`, `--keep-archive N`, `--local-dir`, `--min-interval` (seconds between requests to one host), `--fetch`,
`--pdf-engine`, `--ocr*`, `-v`.

### Forced update (`run --force`)

A normal run is conditional: tracking pages and documents are fetched with `If-None-Match` / `If-Modified-Since`, and a
document whose text fingerprint is unchanged is skipped. `python src/kg_agent.py run --force` refreshes everything from the
sources instead:

* every tracking page is re-read in full (no conditional request) and re-evaluated, so the newest edition on offer is
  found; as usual a `new_edition` finding is applied to the configuration (confidence ≥ 0.8, unless `--no-auto-apply`)
  *before* the documents are read, so **the latest edition is the one that gets read**;
* every source document (remote URL, eCFR XML, purchased local copy) is downloaded again and re-parsed, **even when
  nothing changed**, and its KG is rebuilt; entry KGs and the combined graph are rebuilt as well;
* an unchanged source is recorded as **`refreshed`** (not `updated`): no new version is counted and nothing is
  archived. If the text changed, or the rebuilt KG differs (e.g. after a parser improvement), it is `updated` as in a normal
  run, the old KG is archived and the diff is recorded.

Scope it with `--only standard:22,report:1`; combine with `--dry-run` to see what would be re-read without writing
anything. The flag applies to that run only (it is not sticky). Be considerate: it re-downloads every source once
(`--min-interval` still spaces requests per host). For the daemon, `kg_daemon.py … -- --force` would force *every* scheduled
run; usually a one-off `run --force` is what you want.

Logging and reporting of forced updates:

| where | what |
|---|---|
| `logs/forced.jsonl` | one JSON line per re-read: `kind` = `document` (url, docid, entries, `result` created / updated / refreshed / error, text sha before → after, archive path), `tracker` (url, tracker, result, findings) and a closing `run` record with the summary; written only for forced runs and never in a dry run |
| `logs/report.md` | the run header says **(FORCED UPDATE)**; a *Forced update* section lists every document (result, sha before → after, detail) and every tracking page (result, findings), plus the entry KGs rebuilt |
| `logs/runs.jsonl` | the run summary carries `forced`, `docs_refreshed`, `entries_refreshed` |
| `staging/documents/<id>/state.json`, `state/trackers.json` | `last_forced`, `forced_runs` (and `last_forced_result` for documents) per source |
| `status` | a `forced` column with the date of the last forced re-read of each document |
| MCP | `get_entry_metadata` shows `last_forced_reread`, `forced_rereads`, `last_forced_result` per document; `get_overview` shows `agent_last_forced_run` |

### What the agent writes (`--home`)

```
config/history/<run>_<name>.json   snapshot after every change       config/changes.jsonl   audit log of configuration edits (JSON lines)
staging/documents/<id>/{state.json,kg.json,assets/}    one per unique URL (assets = figure PNGs + table CSVs + manifest)
staging/{standards,reports}/<n>/{kg.json,meta.json}    the KG of each entry
archive/documents/<id>/<ts>_<sha>/   archive/entries/<key>/<ts>/    superseded versions
local/{standards,reports}/<id>/      purchased copies you drop in (folders are created for paywalled entries)
state/{trackers,identities,pending,alerts,config_hashes}.json       logs/{report.md,runs.jsonl,forced.jsonl}
```

## Paywalled documents (warnings)

Standards sold by publishers (ASCE, ASME, ANSI, …) cannot be downloaded. Entries marked `"paywalled": true` (20, 21, 25, 30
in the shipped configuration, also inferred from the text of `format_of_data`) are processed from a **local copy** you
place in `<home>/local/standards/<id>/` (or `local_copy` / `--local-dir`); it is staged like any other document. Open
warnings appear at the top of each report section, in the log, and in `state/alerts.json` (`status` lists them):

* **NO LOCAL COPY** – nothing local to process;
* **NEW EDITION FOR PURCHASE** – a tracker saw a newer edition on a landing / store / index page; stays open until the
  local copy states that edition;
* **LOCAL COPY OUTDATED** – the local copy's own title page states an older edition than the latest known one;
* (info) *local copy not identified*, *store page changed*.

Import and stage a purchased copy with one command (copies it into the right folder, skips identical files,
optionally stages immediately and prints the entry's remaining warnings):

```bash
python src/kg_agent.py add-local standard:30 ~/Downloads/NQA-1-2026.pdf --stage
python src/kg_agent.py add-local standard:25 ASCE43-24.pdf --replace --stage   # new edition: old copy set aside in .superseded/
python src/kg_agent.py local                                                   # list local copies and the edition each states
```
`--move` moves instead of copying. Accepted: `.pdf .html .htm .xml`; scanned PDFs are OCR'd. The daily run also stages
anything you drop into the folder by hand. The MCP server stays read-only, so copies are added on the host, not remotely.

## Version tracking in the configuration

After every run each entry of both JSON files carries a `version_tracking` block (rewritten only when it changes, and the
rewrite is logged in `config/changes.jsonl`):

```json
"version_tracking": {
  "status": "current | update_available | not_staged | partially_staged | no_local_copy | no_documents",
  "latest": [ {"identity": "RG 1.233", "edition": "1", "label": "RG 1.233 Rev. 1", "url": "…", "detected": "…", "source": "tracker page"},
              {"identity": "eCFR title 10 part 50", "edition": "2026-09-22", "label": "eCFR as of 2026-09-22", …} ],
  "staged": { "kg": "staging/standards/2/kg.json", "kg_built": "…",
              "documents": [ {"url": "…", "kind": "pdf", "local": false, "fetched": "…", "text_sha": "9f3cb3ed2d36",
                              "as_of": "eCFR only: date of the staged XML", "role": "store page (paywalled landing pages)",
                              "ocr_pages": [1, 2], "editions": {"RG 1.233": "0"}} ],
              "editions": {"RG 1.233": {"edition": "0", "source": "document | configuration"}} }
}
```

* **latest** – newest edition any tracker has seen (even if the configuration was not updated), else the configured one;
  for regulations the newest eCFR issue date.
* **staged** – what is inside the entry's KG: the documents (content fingerprint, eCFR "as of" date, local copies as
  `local:<file>`, OCR'd pages) and the edition of each identifier (read from the document's own text, else assumed from the
  configuration).
* `update_available` means latest > staged. A report format with no documents of its own shows `no_documents`.

## OCR for scanned PDFs

PDFs are always read **as text first**. Only pages that then have no usable text layer (fewer than ~40 characters while
images cover at least a quarter of the page, or a garbage layer with `(cid:…)` / replacement characters) are OCR'd; a
document with text everywhere never calls an OCR engine. Engines (both local): `rapidocr` (`pip install rapidocr-onnxruntime`,
reports a box per text cell, so tables can be rebuilt) and `tesseract` (program + language data, through PyMuPDF);
`--ocr-engine auto` prefers Tesseract when installed.

`--ocr auto|off|force` (default `auto`), `--ocr-engine auto|tesseract|rapidocr`, `--ocr-lang eng+deu` work on `kg_ingest.py`,
`kg_show.py` and `kg_agent.py`. OCR output becomes headings and paragraphs (two-column pages are read column by column;
running headers and page numbers dropped); rows with several boxes over three or more lines become tables with their
"Table N" caption. It is marked on the elements (`ocr`, `ocr_conf`), the graph (`ocr_pages`, `ocr_engine`) and in the report
and `version_tracking`. Limits: recognition accuracy (check `ocr_conf`), no table cells for engines that return whole
lines, figures on scanned pages are not extracted separately.

## Scheduling

`kg_agent.py run` is one pass; something has to call it every day. Three options are provided (comparison and
recommendation in [`docs/scheduling.md`](docs/scheduling.md)):

| | files | in short |
|---|---|---|
| **systemd timer** | `deploy/cogent-run.service`, `cogent-run.timer` | least code on Linux: catch-up (`Persistent=true`), jitter, retry, timeout, limits, journald. **Recommended where systemd exists** |
| **cron** | `deploy/cogent.cron` | everywhere, one line; no catch-up, retry or status (add `flock`, anacron / `@reboot`) |
| **Python daemon** | `src/kg_daemon.py`, `deploy/cogent-daemon.service` | portable and app-aware (catch-up, retries, `status`, hooks); needs a supervisor to be dependable |

Pick **one** scheduler. The agent's run lock and idempotent updates make an accidental double run harmless.

### `kg_daemon.py`

```bash
python src/kg_daemon.py start   --at 06:00,18:00 --tz Europe/London --jitter 300 -- --extractor hybrid   # detach, log to <home>/logs/daemon.log
python src/kg_daemon.py serve   --at 06:00                      # foreground: for systemd, Docker, tmux
python src/kg_daemon.py status                                  # next run, last run + exit, failures, open warnings (exit 0 = running)
python src/kg_daemon.py run-now                                 # SIGUSR1 to the daemon, or one run here if none is running
python src/kg_daemon.py stop | restart
```

Everything after `--` is passed to `kg_agent.py run` (so `--no-auto-apply`, `--only …`, `--ocr …` work as usual).

* **Schedule** – `--at HH:MM[,HH:MM]` in `--tz` (DST-correct), optional `--jitter` seconds (stable per slot).
* **Catch-up** – a slot missed while the daemon was down runs at start (`--no-catch-up` skips it, but never fires it later);
  a fresh install waits for the next slot unless `--run-on-start`.
* **Retries** – a failed run is retried after `--retry-delay` seconds, doubling, up to `--retries` times, never past the next slot.
* **Supervision** – the agent runs as a subprocess in its own process group, with `--run-timeout`; on stop it gets SIGTERM,
  then SIGKILL after `--stop-grace`. A crash or hang in the agent cannot take the daemon down.
* **Signals** – `SIGTERM`/`SIGINT` stop gracefully, `SIGUSR1` runs now, `SIGHUP` reopens the log after rotation.
* **One daemon per `--home`** – a PID file held with `flock` (no stale locks). State is in `<home>/daemon/state.json`.
* **Hooks** – `--on-failure CMD` (when a run failed for good) and `--on-warnings CMD` (a run left paywall / review warnings);
  the command gets `COGENT_EXIT`, `COGENT_REASON`, `COGENT_WARNINGS`, `COGENT_FAILURES`, `COGENT_REPORT`, `COGENT_HOME`.
* **systemd** – `serve` speaks `sd_notify` (`READY=1`, `STATUS=`, `WATCHDOG=1`), so `Type=notify` + `WatchdogSec=` work.
* POSIX only (fork, flock, signals). The older `kg_agent.py daemon --at HH:MM` is a minimal foreground loop kept for
  convenience.

## MCP server (serve the KGs to AI agents)

`src/kg_mcp.py` exposes the staged graphs plus meta-data (capture times, latest-vs-on-board editions, warnings,
disclaimers) over MCP streamable HTTP with bearer-token authentication:

```bash
python src/kg_mcp.py token create --name claude
python src/kg_mcp.py --home kg_agent_home serve --port 8765 --public-host kg.example.com
```
The server also serves the exported figures/tables (`list_assets`, `get_figure`, `get_table_csv`); figures from paywalled content are watermarked and carry warnings (`--paywalled-assets watermark|block|allow`). For a quick public demo without any hosting see `deploy/demo-tunnel.sh` (Cloudflare quick tunnel; docs/mcp.md). Add `--oauth-issuer https://kg.example.com` for OAuth 2.1 (claude.ai custom connectors). Remote-URL options, Claude Code / API / Desktop instructions and the authentication notes are in [docs/mcp.md](docs/mcp.md).

## Combined graph (which reports depend on which standards)

The last stage of every run derives `<home>/staging/combined/{kg.json,dependents.json,meta.json}` from the per-entry graphs and
the configuration (`src/kg_combine.py`). It is rebuilt only when inputs change, can be deleted at any time, and holds
entry-level relations only (no document text, so paywalled content is never merged).

* `complies_with`, `overlaps_with` – **curated** (`basis: config`), from the JSON configuration.
* `cites` – **inferred** (`basis: text`, with `count`, `identifiers`): an entry's text cites an identifier (e.g. `RG 1.233`)
  that is another entry's own identity. Treat it as a hint.
* `dependents.json` lists per entry `depends_on`, `declared_dependents`, `overlaps`, `cited_by`, `cites` and, for standards,
  `affects_reports` (`declared`, `via_overlap`, `inferred`).
* Alerts for a standard now end with *"Reports affected: report:9 (declared)…"* and carry `details.affects_reports`.
* MCP: tool `get_dependents(entry)`; the graph tools accept `ref="combined"`; resource `kg://combined/graph`.

Figure export notes: images are sniffed by content (PNG, JPEG, GIF, BMP, TIFF, WebP, JPEG 2000, ICO, AVIF, SVG incl. ones
with a BOM, comment or DOCTYPE first); raster formats are converted to PNG (PyMuPDF, then Pillow) and a format that cannot be
converted keeps its own extension. Content that is not an image (e.g. an HTML error page served for an `<img src>`) is not
written and is listed under `skipped` in the figure manifest. No `.bin` files are produced.

## Environment variables

| variable | effect |
|---|---|
| `KG_FETCH` | URL download mode: `browser` (default), `http`, `auto` |
| `KG_USER_AGENT`, `KG_BROWSER_PATH` | override the browser identity / Chromium executable |
| `KG_PDF_ENGINE` | `auto` (default), `layout`, `basic` |
| `KG_OCR`, `KG_OCR_ENGINE`, `KG_OCR_LANG`, `KG_OCR_DPI` | OCR mode / engine / language / render resolution (default 200) |
| `KG_OUTPUT_DIR`, `KG_ASSETS_DIR` | default folders for saved KGs (`kg_store`) and exported figures/tables (`kg_assets`) |
| `OLLAMA_HOST`, `OLLAMA_MODEL` | Ollama server (default `http://localhost:11434`) and model (default `llama3.1`) |

## Tests

```bash
python -m pytest tests          # no internet: the agent tests run against tests/mock_site.py
```

`tests/test_config.py` checks *invariants* of `config/*.json` (unique ids, parseable entries, well-formed URLs, valid
report→standard references, symmetric overlaps, valid `version_tracking`) rather than specific entries, so editing the
configuration does not break the suite. `COGENT_CONFIG_DIR=<dir>` checks another pair of files;
`COGENT_STRICT_CONFIG=1` turns dangling report→standard references from warnings into failures.

The rest cover a three-day simulation of the agent (bootstrap, quiet day, new RG revision / DOE edition / eCFR amendment /
Federal Register rule), configuration propagation, archiving, audit log and manual-edit detection, dry-run, identifier
parsing and edition normalisation, version tracking, paywall warnings, report/standard cross-references, and OCR (fake
engines, mixed documents, tables, columns, scan fingerprints, and RapidOCR when installed), and the daemon (schedule
maths across a DST change, catch-up, retry backoff, hooks, PID lock, supervision / timeout, real `serve` and `start`/`stop`
processes with signals).

## Status and limits

* The committed `config/*.json` carry `version_tracking` blocks from a full run against the live sites on 2026-10-12. The
  agent's trackers were written from the public API documentation of eCFR and the Federal Register and from the general
  structure of the NRC / DOE pages: use `kg_agent.py probe <url>` to see what a tracker parses from a page and adjust
  `src/kg_tools.py` if a layout differs. Review the first runs with `--dry-run` or `--no-auto-apply`.
* The identifier watcher needs the identifier in the page text; a page that changes without one is reported as
  `page_changed` (informational), never applied. Where an entry's name or link text states no edition, `latest` carries the
  identifier without an edition until a page shows one.
* Entries without `links` / `template_links` (many report formats, e.g. reports that only point to eCFR) have status
  `no_documents`; their KG holds the entry node and its `complies_with` relations.
* Paywalled standards can only be tracked through their public landing pages; the standard itself needs your local copy.
* The rule-based extractor is a baseline (capitalised phrases and the words between them). For richer relations use
  `--extractor ollama` or `hybrid`.
* `pymupdf-layout` / `pymupdf4llm` are AGPL-3.0 or Artifex-commercial licensed.
