# `cogent`: Documents → Knowledge Graphs, kept up to date

This agent fetches and stages the contents of regulatory and reporting frameworks used by a hypothetical Licensing Engineer in the nuclear reactor space.

All Python code lives in `src/` and the tests in `tests/` (same level); run the commands below from the repository root so the default `config/…` paths resolve.

| module | purpose |
|---|---|
| `src/kg_ingest.py` | PDF / HTML / eCFR-XML (file or URL) → `networkx` knowledge graph (LangGraph pipeline; rule-based, Ollama or hybrid triple extraction; layout-aware PDF parsing; figure/table export) |
| `src/kg_show.py` | interactive HTML / PNG viewer for a KG |
| `src/kg_agent.py` | **daily agent**: stages one KG per standard / report, detects updates, edits the JSON configuration, archives old KGs, appends a report |
| `src/kg_tools.py` | the agent's hard-coded tools (conditional fetch, fingerprints, identifier parsers, trackers, KG merge/diff, JSON diff) |
| `src/kg_models.py` | Pydantic models |
| `config/standards.json`, `config/reports.json` | the document configurations |

## Quick start

```bash
uv pip install -r requirements.txt && playwright install chromium     # browser only needed for bot-blocking sites; also cf. venv-setup.sh
python src/kg_agent.py run   --standards config/standards.json --reports config/reports.json --home kg_agent_home
python src/kg_agent.py run   --dry-run --only standard:22,report:1      # check only, print the report
python src/kg_agent.py daemon --at 06:00                               # or cron: 0 6 * * *  python src/kg_agent.py run ...
python src/kg_agent.py probe https://www.nrc.gov/regulations-legislation/regulatory-guides-by-division/division-1---power-reactors
python src/kg_agent.py status
```

Local models (optional): `--extractor ollama|hybrid --model llama3.1` builds the KGs' triples with Ollama;
`--llm llama3.1` lets a local model (LangChain `ChatOllama`, Pydantic structured output) *narrate* the
already-structured results at the top of each report section. Without them the run is fully deterministic.

## What one run does

1. **init** – validates both JSON files (Pydantic), takes a lock, and logs any edit made to the configuration since the
   last run (`config/changes.jsonl`, snapshot in `config/history/`).
2. **track** – every `update_tracking_urls` page goes through a hard-coded tracker:
   * eCFR `…/versions/title-N.json?part=P` → latest issue date and *which sections were amended*;
   * `federalregister.gov/agencies/<slug>` → new rules / proposed rules (JSON API), matched to entries by CFR part;
   * anything else (NRC RG lists, DOE standards & directives libraries, NRC pages, publisher pages) →
     *identifier watcher*: finds the rows/list items that mention the entry's identifiers (RG 1.233, NEI 18-04,
     DOE-STD-1271, DOE O 420.1, ASCE/SEI 43, QME-1, RA-S-1.4, NUREG-1537, …) and compares the edition shown with the
     recorded one. A higher edition with a link in the same row is a `new_edition` finding.
3. **apply** – a `new_edition` finding with a link and confidence ≥ 0.8 rewrites the entry's link (url + text) and
   the edition inside the name, and every other entry that shared the old URL (e.g. reports). Everything else goes to
   "Pending (needs human review)" in the report. `--no-auto-apply` turns the editing off.
4. **sync_docs** – each unique document URL is fetched conditionally (ETag / Last-Modified), then fingerprinted by
   *text* (PDF text, visible HTML text without page furniture, eCFR XML), so a re-rendered file or a changed footer is
   not an update. eCFR pages are read from the versioner XML API (deterministic section structure + `§` cross-reference
   edges). A changed document is re-ingested, its previous KG is **archived**, and a structured diff (modified / new /
   removed sections, entity counts) is computed.
5. **assemble** – the KG of every standard/report whose documents (or links, name, compliance list) changed is rebuilt
   by merging its document KGs (shared entities link them), plus `HAS_DOCUMENT` and `complies_with` edges; the old one is archived.
6. **finalize** – state saved, and a section is **appended** to `logs/report.md` (summary, configuration changes, document
   updates with section-level diffs, entry KGs rebuilt, tracker findings, pending review, errors, stale trackers).

## OCR for scanned PDFs

PDFs are always read **as text first** (layout parser, or the basic parser). Only pages that then have no usable text
layer (fewer than ~40 characters while images cover at least a quarter of the page, or a garbage layer with `(cid:…)` /
replacement characters) are OCR'd; every other page is left untouched, and a document with text everywhere never calls
an OCR engine. Everything is local:

* `rapidocr` – `pip install rapidocr-onnxruntime` (ONNX models, no system package), reports a box per text cell;
* `tesseract` – the `tesseract` program + language data (`apt install tesseract-ocr`), used through PyMuPDF.

`--ocr auto|off|force` (default `auto`; `force` OCRs every page), `--ocr-engine auto|tesseract|rapidocr` (auto = Tesseract
if installed, else RapidOCR) and `--ocr-lang eng+deu` work on `kg_ingest.py`, `kg_show.py` and `kg_agent.py`
(or `kg_ingest.set_ocr(...)`, `$KG_OCR`, `$KG_OCR_ENGINE`, `$KG_OCR_LANG`, `$KG_OCR_DPI`).
OCR output becomes the usual headings / paragraphs (two-column pages are read column by column, running headers and page
numbers dropped, multi-box rows of 3+ lines rebuilt as tables with their "Table N" caption) and is marked: element
attribute `ocr` / `ocr_conf`, graph attributes `ocr_pages` / `ocr_engine`, and in the agent's report and the entry's
`version_tracking.staged.documents[].ocr_pages`. Scanned files are fingerprinted by their page images, so re-saving the
same scan is not an update. Limits: OCR accuracy (check low `ocr_conf`), no cell structure for tables when the engine
returns whole lines (Tesseract), figures on scanned pages are not extracted separately.

## Paywalled documents (warnings)

Standards sold by publishers (ASCE, ASME, ANSI, ...) cannot be downloaded. Mark them with `"paywalled": true` in
`standards.json` (entries 20 and 21 are; it is also inferred from "paywalled" in `format_of_data`). For each of them the
agent looks for a **local copy** in `<home>/local/<standards|reports>/<id>/` (created for you; or `--local-dir`, or a
`"local_copy"` file/dir/glob in the entry) and processes it like any other document (KG, archive, change detection).
Open warnings are listed at the top of every report section, logged, and kept in `state/alerts.json`
(`status` shows them):

* **NO LOCAL COPY** — nothing local to process;
* **NEW EDITION FOR PURCHASE** — a tracker saw a newer edition on the landing/store/index pages; stays open until the
  local copy states that edition;
* **LOCAL COPY OUTDATED** — the local copy's own title page states an older edition than the latest known one;
* (info) *local copy not identified*, *store page changed*.

## Version tracking in the configuration

After every run the agent writes a `version_tracking` block into **each entry of both JSON files** (only when its
content changed, and the edit is logged in `config/changes.jsonl`):

```json
"version_tracking": {
  "status": "current | update_available | not_staged | partially_staged | no_local_copy | no_documents",
  "latest": [ {"identity": "RG 1.233", "edition": "1", "label": "RG 1.233 Rev. 1", "url": "…", "detected": "…", "source": "tracker page"},
              {"identity": "eCFR title 10 part 50", "edition": "2026-09-01", "label": "eCFR as of 2026-09-01", …} ],
  "staged": { "kg": "staging/standards/2/kg.json", "kg_built": "…",
              "documents": [ {"url": "…", "kind": "pdf", "local": false, "fetched": "…", "text_sha": "9f3cb3ed2d36",
                              "as_of": "2026-09-01 (eCFR only)", "role": "store page (paywalled landing pages)", "editions": {…}} ],
              "editions": { "RG 1.233": {"edition": "0", "source": "document | configuration"} } }
}
```

* **latest** = newest edition a tracker has seen (even when the configuration was *not* updated, e.g. `--no-auto-apply`),
  or the configured one; for regulations the newest eCFR issue date.
* **staged** = what is actually inside the entry's KG: its documents (with content fingerprint, eCFR "as of" date,
  local copies as `local:<file>`) and the edition each identifier has there (read from the document's own text, else
  taken from the configuration).
* `update_available` means latest > staged. `python src/kg_agent.py status` prints the table, and each report lists the
  entries that are not current. Pending findings are retried on later runs when you re-enable auto-apply.

Other optional annotations the agent understands: `"paywalled": true`, `"local_copy"`, `"overlaps_with": [{"id", "scope",
"note"}]` (becomes an `overlaps_with` edge between the entry KGs; entries 1 and 28 overlap on 10 CFR 50 Appendix B) and
`"previous_versions"` (superseded editions, kept for reference but not staged).

## Directory layout (`--home`)

```
config/history/<run>_standards.json …    snapshots after every change      config/changes.jsonl   audit log (JSON lines)
staging/documents/<id>/{state.json,kg.json,assets/}   one per unique URL (assets = figures PNG + tables CSV)
staging/{standards,reports}/<n>/{kg.json,meta.json}   the KG of each entry
archive/documents/<id>/<ts>_<sha>/   archive/entries/<key>/<ts>/             superseded versions (--keep-archive N to prune)
local/{standards,reports}/<id>/   purchased copies you drop in
state/{trackers,identities,pending,alerts,config_hashes}.json   logs/{report.md,runs.jsonl}
```

## Status and limits

* Tested end to end (3 simulated days, config propagation, archive, audit, dry-run) against a local mock of the
  sources: `python -m pytest tests`. **Not yet run against the live sites** (no internet in the development sandbox).
  The eCFR / Federal Register response shapes follow their public API docs; use `src/kg_agent.py probe <url>` to check
  what each tracker parses from a real page, and adjust `src/kg_tools.py` if a layout differs.
* The identifier watcher is only as good as the page markup: rows must contain the identifier text. A page that
  changes without a recognisable identifier is reported as `page_changed` (informational), never applied.
* Paywalled standards (ASCE, ASME/ANSI) can only be tracked through their public landing pages.
* `pymupdf-layout` / `pymupdf4llm` are AGPL-3.0 or Artifex-commercial licensed.
