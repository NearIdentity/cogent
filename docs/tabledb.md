# Table database: looking up design values in the tables of the standards

`src/kg_tabledb.py` loads the tables that the KG agent extracted from the staged documents into **PostgreSQL + pgvector**, so that
design values and parameters can be looked up by meaning (semantic search) *and* by exact filter, joined across tables, and
documented in data dictionaries. The MCP server (`kg_mcp.py`) is the query front-end.

```
KG agent (kg_agent.py run)                    table database agent (kg_tabledb.py)                          MCP server
staging/documents/<id>/kg.json      ──┐      ┌───────────────────────────────────────────┐        list_tables / describe_table
staging/documents/<id>/assets/…/    ──┼────► │ discover → load → prune → relmap → report │ ─────► search_tables / lookup_values
   tables/*.csv + manifest.json     ──┘      └───────────────────────────────────────────┘        query_table / join_tables / run_sql*
                                                │ PostgreSQL schema kgtables                    get_data_dictionary / get_relationship_map
                                                │ tabledb/<docid>/{data_dictionary,relationships}.{json,md,mmd}
```

## Quick start
```bash
cp deploy/tabledb.env.example deploy/.env      # set COGENT_DB_PASSWORD and COGENT_RO_PASSWORD (deploy/.env is git-ignored)
docker compose -f deploy/docker-compose.tabledb.yml up -d                  # PostgreSQL 16 + pgvector (or install both yourself)
ollama pull nomic-embed-text                                               # the embedding model (768 dimensions)
export COGENT_TABLEDB_DSN=postgresql://cogent:<COGENT_DB_PASSWORD>@127.0.0.1:5432/cogent    # never commit a real one
pip install -r requirements.txt

python src/kg_agent.py run --tables            # stage as usual, then build / refresh the table database as the last stage
python src/kg_tabledb.py build                 # or on its own (incremental); --force re-embeds everything, --only standard:22
python src/kg_tabledb.py status
python src/kg_tabledb.py --dsn postgresql://cogent_ro:…@127.0.0.1/cogent check     # can the MCP server's read-only login connect and read? (exit 1 if not)
python src/kg_tabledb.py search "damping value for welded steel structures"

python src/kg_mcp.py serve --tabledb-dsn postgresql://cogent_ro:…@127.0.0.1/cogent     # query front-end (see below)
```
`kg_agent.py run --tables` runs the table stage *after* staging (inside the agent's lock, before the report is written); a problem
there (database down, Ollama not running) is reported and never fails the staging run. `kg_daemon.py … -- --tables` does it every
day. `--force` of the KG agent also forces the table stage. Offline / CI: `--embedder hash` (deterministic feature hashing, no model).

## What is built

**One database table per extracted table** (`kgtables.kgt_<docid>_<id>`):

| column | content |
|---|---|
| `_row_no` | row number (1-based) |
| `_raw` (jsonb) | the original cell text, keyed by the original header: nothing is lost to type conversion |
| `<column>` | one typed column per CSV column: `bigint`, `double precision` or `text`. A column is numeric when ≥ 90 % of its non-empty cells parse as numbers (`1,200`, `<0.5`, `5 %`, `1.0E-4`, `1.0 × 10^-4 g`); other cells of a numeric column are NULL here and intact in `_raw` |
| `_embed_text` | exactly the text that was embedded (audit and lexical fallback) |
| `_embedding vector(DIM)` | the row embedding, **same model, same dimension and same recipe in every table**; HNSW index (cosine) |

**Which "tables" get in.** Layout detectors (PDF table finders, OCR layout models) report two-column running text, sentence lists and rosters as tables. Before the graph is built, `src/kg_tablecheck.py` scores each candidate (long cells, no numbers, headers that read like sentence fragments, two columns of running text, a single text column) and sends the false ones back as paragraphs, in reading order (left column, then right), marked `demoted_table` with the reasons; the graph records `tables_demoted`. A table with a "Table N" caption, and numeric tables, need much stronger evidence to be rejected. Lists of person names are kept but flagged, and name columns are never offered as join keys. `KG_TABLE_CHECK=off` disables the check. The thresholds are heuristics tuned on constructed examples, not yet on a corpus of scanned standards: check `tables_demoted` and the demoted paragraphs on your documents.

**Front matter.** A table of contents (dot leaders, or section numbers with a rising page-number column, or a "Contents" caption) and an acronym list (two columns: all-caps acronym, multi-word expansion) are flagged `front_matter` (`toc` / `glossary`) on the graph's Table node and are **not exported** as data tables (the asset manifest lists them under `skipped`, so they never reach the table database; a table already loaded is dropped on the next build). Two more kinds are flagged the same way: `index` (a table whose caption or section is a subject / numerical / interpretation index) and `store` (a web shop's order box: "Add to cart" cells, two shop column names such as Price + SKU, or the bare `Quantity | Item` pair of a table with no "Table N" caption). A caption that is only a piece of the header row (`| Millions`) is replaced by the heading of the section the table sits in (the original stays as `caption_fragment` on the node). Captions are normalised before the table label is read: every dash variant (including the non-breaking hyphen and the minus sign) and `--` become `-`, so `Table 5‑1` and `Table 5‑2` stay distinct, and a glued acronym gets its space back (`andSDC-2` → `and SDC-2`). `list_tables` and the other table tools show the entry's name instead of a generic document label such as `PDF`.

**Entities and tables.** Each data table gets `MENTIONS` edges (`via`: caption, header or key cell) to the entities named in its caption, its header and its first column, so `Seismic Design Category` or `SDC-1` leads to the tables that use them; front-matter tables are left out. Rows of contents, glossary, index and shop tables produce no row facts. A glossary row (`SSC | Structures, Systems and Components`) and the text pattern `Seismic Design Category (SDC)` give one `abbreviation_of` edge (acronym to expansion) instead of a `related_to` / `has_value` circle. Surface forms that differ only in a plural (`SDC` / `SDCs`) are one entity; the other spellings are listed in the node's `aliases`.

**Footnote markers and repeated headers.** In the basic PDF parser a raised, smaller trailing run in a cell (`NDC-2` + superscript `4`) is a footnote marker, not part of the value: it is removed from the cell (so `NDC-2`, not `NDC-24`) and kept as `footnotes` `[{row, col, marker}]` on the Table node and in the asset manifest; exponents (`10^-4`) and unit powers (`m²`) are left alone. Header blocks (1-3 rows) repeated inside a table or at a page break are removed, also when OCR garbled them (`repeated_header_rows_removed`). Not covered: the `pymupdf-layout` engine and OCR'd pages carry no font information, so their tables are unchanged.

Header detection: the first row is the header unless it is mostly numeric (then `column 1…n`); empty columns and rows are dropped; duplicate headers get
suffixes; names are ASCII snake_case, never reserved words (`limit` → `c_limit`).

**Registry tables**: `kgt_tables` (one row per table: source document and URL, page, caption, section path, intro, notes, KG entities, entries that use
it, `paywalled`, hashes, the `table_embedding`), `kgt_columns` (header, type, role, unit, distinct counts, key candidate, samples, range),
`kgt_joins` (inferred joins and same-schema pairs with score and evidence), `kgt_meta` (embedder spec, dimension, recipe version).

### Embedding recipe (`kg_tables.py`, version `v2`)
Row text = `document | section path | caption` + `Columns: <headers with units>` + `<header>: <cell>; …` (**every cell, numbers included**: row mode `full`,
the default) + KG context (`Context:` the paragraph in front of the table, `Notes:` footnotes after it, `Related:` entities and cited identifiers the KG
links to the table). `--row-mode text` restores the v1 behaviour (numbers left out, `Values given for:` column names instead). The mode is part of the recipe id
(`v2-full` / `v2-text`) stored in `kgt_meta`; a database built with another recipe is refused until `--rebuild`. Entities that are really the table's own cells
(`TableRow` / `Value` nodes, numbers) are left out. The table as a whole is embedded too for the first retrieval stage. Models with task prefixes get them
(`nomic-embed-text`: `search_document:` / `search_query:`). Numbers are blurred by embedding models (`0.5` ≈ `5.0`): use `query_table` for exact values.

### Incremental behaviour
A table is reloaded only when its CSV hash, its KG-context hash or the embedder spec changed (or `--force`). Tables whose source disappeared are dropped
(with their joins and published files); a run limited by `--only` never drops anything outside its scope. A different embedder or recipe is **refused**
(vectors of different models are not comparable) until you rebuild with `--rebuild`. If Ollama is unreachable the run stops embedding, leaves every table as it
was and reports the failure; the next run picks up. Each run is logged in `logs/tabledb.jsonl` and `logs/tabledb_report.md`.

## Joins, data dictionary, relationship map (`kg_relmap.py`)
Within each source document every pair of tables is compared. A candidate join column pair is scored

`score = 0.40 · name similarity + 0.45 · value containment + 0.05 · same type + 0.10 · KG boost` (kept at ≥ 0.55)

* **name similarity**: near-identical after normalisation (case, punctuation, units in brackets, `no.` = `number`, plural). A header that only numbers the column (`column_1`, `c_4`, empty) has no similarity to anything, itself included;
* **value containment**: share of the smaller column's distinct values found in the other (decimal columns are never join keys). When the headers say nothing, values alone can carry a join: text columns, at least 5 shared values, at least 80 % containment, and one side unique (a key); the score is then `0.60 · containment + 0.15 + 0.05 · same type + 0.10 · min(1, shared / 20) + 0.10 · KG boost`;
* **not keys**: columns whose shared values are all list markers (`2.`, `3.`) unless the names match strongly, shared values that are mostly cross-references (`table 1:`, `see ...`, `entire repo`), columns that repeat on both sides (a shared vocabulary), and person-name columns;
* **table named in a header**: a header or first cell that names the other table (`Isotope Name Used in Table 1-1`) adds 0.10;
* **KG boost**: same section (0.5) or top-level section (0.25), one table refers to the other (`Table 3-1` in the other's caption / intro / notes or a
  paragraph that cites it; 1.0), two or more shared entities (0.5).

Cardinality (`1:1`, `1:N`, `N:M`) follows from key-uniqueness; tables with (nearly) identical columns are reported as `same_schema` (a table split in parts).
Published per document under `<home>/tabledb/<docid>/`: `data_dictionary.json|md` (tables, columns, types, units, samples, joins per column),
`relationships.json` (edges with score and evidence) and `relationships.mmd` (Mermaid ER diagram). They are regenerated when the document's tables change.

## LLM join inference with an LLM judge (`kg_llm.py`, `kg_llmjoin.py`)
Enabled with `--llm-joins` (`kg_agent.py run --tables --llm-joins`, or `kg_tabledb.py build --llm-joins`); models: `--join-model`, `--join-judge-model`
(default `llama3.1:8b` via Ollama `/api/chat`, structured JSON output). For up to `--join-max-pairs` table pairs of one document (deterministic hits, KG-related
and header-overlapping pairs first):

1. **suggest**: `--join-samples` prompts in three styles (schema-only, with sample rows, with KG context) propose join column pairs with a confidence;
2. **judge**: a second model call per candidate (`--join-judge-runs`, original and swapped column order, majority vote) says accept / reject / uncertain, for
   the LLM's suggestions *and* the deterministic joins;
3. **reconcile**: each candidate gets a status (`confirmed` = deterministic and LLM agree, `deterministic_only`, `llm_only`, `uncertain`, `disputed`, `rejected`) and
   `final_score = 0.35 deterministic + 0.25 LLM agreement + 0.40 judge` (a judge rejection caps it at 0.30);
4. **metrics** (`join-evaluation`): agreement across the LLM runs, LLM vs deterministic overlap, judge accept rates per origin, judge self-consistency.

All candidates and evidence are stored; the **approach** is chosen at query time and recomputes nothing: `deterministic`, `llm`, `hybrid` (default: any
candidate the judge did not reject), `consensus` (proposed by both and accepted by the judge). The judge is another model reading the same samples: its verdicts are
evidence, **not ground truth**. If Ollama is unreachable the stage is skipped and the deterministic joins stand. Cost grows with pairs × samples × judge runs
(`--join-max-seconds` bounds it).

## Units of numeric columns (`kg_units.py`)
Columns get *unit hypotheses with provenance*: the header (`Stress (ksi)`), a consistent cell suffix, a header phrase, declarations in the caption / introduction / notes
(`all stresses in ksi`), then weaker evidence: the section text, KG entities, the document's dominant unit, the customary unit of the quantity. Counts, ids and
years get none. Nothing is converted.

* `strict` (narrow): explicit and declared units only; two competing units give no unit and a conflict note;
* `broad` (diverse): adds the weaker evidence, flagged `inferred` with confidence and alternatives;
* `off`: only what the header or the cells say.

## Tailoring per session and per task
Everything above is stored in full; what is *used* is decided per call. Resolution order (last wins): server default profile (`--default-profile`) < the session's saved
preferences (`set_session_preferences`) < a `profile` in the call < explicit arguments (`approach`, `min_score`, `include_rejected`, `min_agreement`, `units`, `retrieval`).
Sessions are keyed by token / OAuth subject plus `session_id` or the MCP session id. Profiles: `fast`, `balanced` (default), `strict`, `exploratory`, `llm`
(`list_profiles`). Prompts `find_design_value`, `strict_design_lookup`, `explore_table_relationships`, `audit_units` set a task-specific posture; `retrieval` is
`vector`, `lexical` or `hybrid` (reciprocal-rank fusion).

## MCP front-end
Registered when the server has `--tabledb-dsn` (or `COGENT_TABLEDB_DSN`); at start-up the server logs whether the database is usable, and `get_overview` reports `table_database` (`reachable`, `built`, `can_select`, an `error` and a `hint`); the dictionaries and maps are served from the published files and need no database.

| tool | use |
|---|---|
| `list_tables(ref?, query?)`, `describe_table(table_id)` | what exists; columns, types, units, sample rows |
| `search_tables(question, ref?)` | which tables are about this |
| `lookup_values(question, ref?, table_ids?)` | two-stage semantic search: best tables, then best rows; returns original cell text with document, page, caption. Identifier-like terms in the question are split into *subjects* (`A572`, `SA-516`, `Cs-137`, `RG 1.233`) and *category labels* (`HC-3`, `SDC-2`, `NDC-2`, ... which tables often spell out, `Hazard Category 3`). At least one subject must occur in a returned row (checked cell by cell, column names, caption, section); category labels only rank rows (`HC-3` also matches `Hazard Category 3`) and are reported as `missing_terms` with `match_quality: partial` when absent. If no row mentions a subject the result says `no_confident_match: true` with a `message`, an empty `rows` and the closest rows under `nearest_neighbours` (a nearest-neighbour search otherwise always returns something). `match_quality` is `lexical`, `partial`, `semantic`, `weak` or `none`. A similarity floor is the preference `min_similarity` (0..1; profile `strict` 0.70, the others 0 = off; call argument or session preference), `COGENT_LOOKUP_MIN_SIMILARITY` sets a server-wide minimum. The 0.70 is provisional: it comes from one observation (irrelevant rows scored 0.68 with `nomic-embed-text`) and has not been calibrated |
| `query_table(table_id, where=[{column, op, value}], …)` | exact filters (`= != < <= > >= like in between is_null not_null`), numeric columns compare numerically |
| `join_tables(left, right, [columns])` | join along the inferred relationship |
| `get_data_dictionary(ref, format)`, `get_relationship_map(ref, format)` | JSON / Markdown / Mermaid, for the session's approach and units mode |
| `evaluate_joins(ref)`, `explain_units(table_id)` | LLM join evaluation (also on request, with the judge); every unit hypothesis with evidence |
| `set/get/reset_session_preferences`, `list_profiles` | tailoring (above) |
| `run_sql(query)` | only with `--allow-sql`: one read-only SELECT |

Resources: `kg://tables/{docid}/dictionary`, `kg://tables/{docid}/relationships`. `ref` is an entry key (`standard:22`) or a document id.

* Questions are embedded with the same embedder the database was built with (`--embedder`, `--embed-model`, `--ollama-host`). If it is unreachable or differs from the
  database's, the search falls back to lexical matching and says so (`mode: lexical`, `note`).
* **Paywalled content.** Tables from purchased copies or publisher hosts come back with `paywalled: true`, `warnings` (copyright notice, open alerts such as *new
  edition for purchase*) and a `copyright_notice`; `--paywalled-assets block` removes them from every result (and disables `run_sql`).
* Every answer carries a disclaimer: values are machine-extracted and must be checked against the source.
* **Security**: all values are bound parameters, identifiers come from the registry; connect the server with the SELECT-only role in
  `deploy/tabledb-readonly-role.sql`. `run_sql` adds a keyword filter, a read-only transaction and a timeout, but the role is the real boundary.

## Design alternatives that were weighed
| decision | chosen | alternative | why |
|---|---|---|---|
| store | PostgreSQL + pgvector (as asked) | SQLite + sqlite-vec; DuckDB; a vector DB (Qdrant) plus files | Postgres gives typed SQL, joins and ANN in one place, with roles and backups; the others are lighter but split relational and vector queries or lack roles |
| layout | one table per CSV + registry | one long table (`row_json`) for all rows; EAV | typed columns make `damping >= 5` and joins real SQL; the long table is simpler to load and search but loses types and the data dictionary has nothing to describe |
| embedding scope | whole rows + names + metadata + KG context (`full`); `text` mode available | text cells only; labels only | whole rows keep the numbers findable by similar-looking questions, at the price of blurred magnitudes; exact values stay with SQL |
| retrieval | two-stage (table vectors → row vectors) | one global row index; hybrid BM25 + vector | the table stage keeps row search cheap and adds the table's meaning; a global index is simpler but scans every table's rows |
| join inference | names + values + KG with an explainable score | LLM-proposed joins; value-only matching; embeddings of columns | deterministic is auditable; the LLM finds semantic joins the names/values miss; the judge adds a second opinion at a cost in time, and neither is ground truth, so all candidates are kept and the approach is chosen per call |
| trigger | hook after staging + standalone CLI | a separate daemon; DB triggers | one lock, one report, no extra process; the CLI covers reruns |
| embedder | Ollama (as asked), pluggable | sentence-transformers in-process; hosted API | local, free, no data leaves the machine; hash embedder for tests |

## Limits
* Header detection assumes one header row; merged / multi-row headers and tables split across pages may need manual review (the extractor already merges page continuations).
* Join candidates are heuristics (see their scores); decimal columns are never keys; value overlap needs ≥ 2 distinct values.
* Units are labels with provenance; conversion is not attempted and broad-mode units are guesses to be checked.
* The LLM stages were tested against scripted models and a mock Ollama server, not a real model: expect to tune prompts and thresholds. Databases built with recipe v1 need `--rebuild`.
* `nomic-embed-text` truncates long text; rows are short, but very wide tables are cut at 6000 characters.
* Embeddings of rows from tables with few text cells are dominated by metadata; use `query_table` for exact lookups.
