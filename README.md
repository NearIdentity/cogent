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
config/standards.json  config/reports.json     the document configurations (30 standards, 15 report formats)
src/kg_ingest.py     PDF / HTML / eCFR-XML (file or URL) → networkx KG; extractors; export/save/load; assets
src/kg_ocr.py        OCR for scanned PDF pages (RapidOCR / Tesseract engines, page → headings/paragraphs/tables)
src/kg_show.py       interactive HTML (vis-network) and static PNG viewer for a KG
src/kg_agent.py      the daily agent: LangGraph workflow, staging, archive, config edits, version tracking, alerts, report
src/kg_tools.py      the agent's hard-coded tools: conditional fetch, fingerprints, identifier parsers, trackers, KG merge/diff
src/kg_models.py     Pydantic models (configuration views, state, findings, alerts, version_tracking block)
tests/               test_agent.py, test_ocr.py + mock_site.py (a local mock of the sources); no internet needed
requirements.txt     dependencies (optional ones are commented)      
venv-setup.sh   uv-based virtualenv setup
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
python src/kg_ingest.py paper.pdf out.graphml --assets-dir kg_assets      # + figures (PNG) and tables (CSV) as files

# look at it
python src/kg_show.py https://www.nrc.gov/docs/ML1924/ML19241A472.pdf --open

# the daily agent
python src/kg_agent.py run --dry-run --only standard:22,report:1       # check only, prints the report, writes nothing
python src/kg_agent.py run                                             # stage / update everything under ./kg_agent_home
python src/kg_agent.py status                                          # staged documents, version table, open warnings
python src/kg_agent.py probe https://www.nrc.gov/regulations-legislation/regulatory-guides-by-division/division-1---power-reactors
python src/kg_agent.py daemon --at 06:00                               # every day at 06:00 local time
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

Subcommands: `run [--dry-run]`, `daemon --at HH:MM`, `probe <url>` (run one tracker and print what it parsed: use this to
check a tracker against a live page), `status`. Common options: `--standards`, `--reports`, `--home` (default
`kg_agent_home`), `--only standard:22,report:1`, `--extractor rules|ollama|hybrid`, `--model`, `--llm`, `--no-auto-apply`,
`--no-assets`, `--keep-archive N`, `--local-dir`, `--min-interval` (seconds between requests to one host), `--fetch`,
`--pdf-engine`, `--ocr*`, `-v`.

### What the agent writes (`--home`)

```
config/history/<run>_<name>.json   snapshot after every change       config/changes.jsonl   audit log of configuration edits (JSON lines)
staging/documents/<id>/{state.json,kg.json,assets/}    one per unique URL (assets = figure PNGs + table CSVs + manifest)
staging/{standards,reports}/<n>/{kg.json,meta.json}    the KG of each entry
archive/documents/<id>/<ts>_<sha>/   archive/entries/<key>/<ts>/    superseded versions
local/{standards,reports}/<id>/      purchased copies you drop in (folders are created for paywalled entries)
state/{trackers,identities,pending,alerts,config_hashes}.json       logs/{report.md,runs.jsonl}
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

The agent is **not** scheduled by itself. `daemon --at HH:MM` (default `06:00`, local time; `kg_agent.py` → `daemon()` /
`_next_run()`) is a foreground loop that sleeps until the next time and calls `run`; it does not survive a restart and does
not catch up on missed runs. For something sturdier use cron or a systemd timer:

```
0 6 * * *  cd /path/to/cogent && ./cogent/bin/python src/kg_agent.py run >> kg_agent_home/logs/cron.log 2>&1
```

A second run while one is active stops with an "another run is active" error (lock file).

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
python -m pytest tests          # 19 tests, no internet: they run against tests/mock_site.py
```

They cover a three-day simulation of the agent (bootstrap, quiet day, new RG revision / DOE edition / eCFR amendment /
Federal Register rule), configuration propagation, archiving, audit log and manual-edit detection, dry-run, identifier
parsing and edition normalisation, version tracking, paywall warnings, report/standard cross-references, and OCR (fake
engines, mixed documents, tables, columns, scan fingerprints, and RapidOCR when installed).

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
