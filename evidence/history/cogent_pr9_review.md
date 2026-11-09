# Review: NearIdentity/cogent PR #9, "Updates based on the latest MCP client-side testing (2026-10-26)"

**Commit:** `a434240` (one commit on top of `main` @ `af5630d`) · 15 files, +471 / −53
**Reviewed against:** `cogent_fix_list_2026-10-26.md` (items R6-01 … R6-15)
**How:** read the full diff; ran the test suite; probed the new heuristics directly with edge-case inputs (scripts and outputs summarised below).

## Verdict

**Approve after 4 small changes.** The PR addresses 13 of the 15 items, and most fixes are well targeted:
- an extraction-recipe version that triggers rebuilds
- a lexical gate on `lookup_values`
- value-driven joins with placeholder headers
- `abbreviation_of` edges and table → entity links
- clearer errors
- `dropped_tables`

But two fixes don't do what the fix list (and the PR's own docstrings) say, one change creates a false negative in normal use, and one item has the mechanism without the config change that activates it.

**Must fix before merge**
1. `assess_lookup` requires *every* identifier in the question, so ordinary questions return `no_confident_match` (A1).
2. The ordinal junk join from R6-05 still passes (B1).
3. `caption_fragment` misses the Table 1-1 caption it was written for, and replaces real short captions (C1).
4. R6-02 needs a `scope` on standard:28's link; without it nothing changes (D1).

**Should fix (can follow)**
- Index detection drops real "Index …" data tables (C2).
- Hyphenated acronyms (DBA, LBE) aren't recognised (E1).
- `README` refers to a `tests/test_round6.py` that isn't in the PR (F1).
- Several new heuristics have no tests (F2).

## Tests

| Run | Result |
|---|---|
| Full suite, no PostgreSQL | 257 passed, 46 skipped, 5 failed |
| The 5 failures (`test_table_footnotes.py` ×4, `test_ocr.py::test_real_rapidocr`) | **Same 5 fail on `main`**: environment (PyMuPDF / RapidOCR versions), not this PR |
| Table, MCP, agent and extraction tests **with** PostgreSQL 16 + pgvector | **203 passed**, 1 skipped (no OpenTelemetry SDK), including the new `test_lookup_of_an_identifier_no_table_mentions_is_no_confident_match` |

## Coverage of the fix list

| Item | Status in PR | Notes |
|---|---|---|
| R6-01 lookup "not found" | **Partial** | Lexical gate for identifier-like terms ✓; similarity floor added but **off by default** (`COGENT_LOOKUP_MIN_SIMILARITY=0`); see A1 |
| R6-02 Part 50 tables under "Appendix B" | **Mechanism only** | `scope` / `scope_end` on links ✓, but `config/standards.json` isn't changed; see D1 |
| R6-03 A-6 → Table 1-1 join | **Likely fixed** | Values-only path with ≥5 shared, ≥80 % containment and one key side gives ≈0.85 for this pair. The "+0.10, header names the table" bonus won't fire while Table 1-1's caption is garbage (C1). Not verified on data |
| R6-04 tables not reloaded | **Fixed** (mechanism) | `EXTRACTION_RECIPE` + `recipe_refresh` rebuild every KG once; tables reload when their CSVs change. Visible Table 1-1 symptom remains (C1) |
| R6-05 junk joins | **Partial** | Cross-reference keys ✓, placeholder headers ✓; **ordinal keys with identical headers still pass** (B1) |
| R6-06 non-data tables | **Mostly** | `store` ✓, `index` ✓ (too broad, C2), "\| Millions" ✓ |
| R6-07 ANS identifiers | **Fixed** | New `ANS` family covers `ANSI/ANS-2.26-2004`, `ASME/ANS`, `(R2017)`-style suffix not needed |
| R6-08 plural duplicates | **Fixed** | `entity_key` singularises the last word; aliases kept. Minor: `analyses → analyse` (E2) |
| R6-09 entity ↔ table links | **Fixed** | `link_table_entities` (caption, header, first column) |
| R6-10 circular SDC edges | **Fixed** | One `abbreviation_of` edge; except hyphenated expansions (E1) |
| R6-11 suggestions on `combined` | **Fixed** | Same `_near_ids` for every ref |
| R6-12 noisy suggestions | **Fixed** | Cutoff 0.8; "nonexistent" → none; `entry:standrd:3` → `entry:standard:3`; ~0.15–0.25 s on a 9 k-node graph |
| R6-13 unknown entry | **Fixed** | "unknown entry … (valid entries: report:1-15, standard:1-30)" + close matches |
| R6-14 stale builds | **Fixed** | Recipe stamp; `get_overview.entries_behind_extraction_recipe`; per-document recipe in metadata |
| R6-15 dropped tables | **Fixed** | `dropped_tables` (≤100) in the run summary and report |

---

## Annotations

### A. `src/kg_tableservice.py`: lookup confidence

**A1 (must fix): all identifiers must match, so normal questions fail.** `kg_tableservice.py:102`
```python
hits = [r for r in rows if all(w in hay(r) for w in want)]
```
`code_tokens` treats category labels as identifiers, and tables rarely spell those out:

| Question | Tokens | Result against the actual Table 1-1 row for Cs-137 |
|---|---|---|
| "HC-3 threshold quantity for Cs-137" | `HC-3`, `Cs-137` | **`none`**, `missing_terms: ["HC-3"]` |
| "threshold quantity for Cs-137" | `Cs-137` | `lexical` ✓ |

Table 1-1's headers say "Hazard Category 3", never "HC-3", so a perfectly good answer is withheld, and the tool tells the agent to say the value wasn't found. The same applies to `HC-2`, `NDC-2`, `PDC-3` and similar.

Suggested change: separate *subject* identifiers (material, nuclide, document numbers: A572, SA-516, Cs-137, RG 1.233) from *category / qualifier* labels (`HC-n`, `SDC-n`, `NDC-n`, `FDC-n`, `PDC-n`, `WDC-n`, `VDC-n`, `RISC-n`, `Class n`). Then:
- require at least one subject token to match lexically; or
- use `any` instead of `all`, and report `matched_terms` / `missing_terms` with `match_quality: "partial"` instead of emptying `rows`.

Expanding known labels before matching (HC-3 ↔ "Hazard Category 3") also helps.

**A2 (should fix): similarity floor is off by default.** `kg_tableservice.py:428`. Questions with no identifier ("damping for welded steel") still get no confidence signal, which was half of R6-01. Calibrate a default for `nomic-embed-text` (the A572 test rows sat at ~0.68 and true matches higher). Read it from session prefs/profile (as `min_score` is) instead of an environment variable, so `strict` can tighten it.

**A3 (nit): tokeniser oddities.** `kg_tableservice.py:58`.
- "RA-S-1.4" yields the token `S-1.4`. It works only because "ras14" contains "s14".
- "COVID-19" is a token.

Consider adding the RA-S, ASCE x-yy and NQA families to `_CODE_B`, and tokenising on the existing `kg_tools.FAMILIES` identifier patterns so the server and the extractor agree on what an identifier is.

**A4 (nit): matching across cell boundaries.** `hay()` concatenates caption, section and `key value` pairs and strips all punctuation and spaces before a substring test. Adjacent fragments can join into a false hit. In my test the column name between cells prevented it, but a per-field check (or keeping a separator character in `_norm`) is safer.

### B. `src/kg_relmap.py`: join inference

**B1 (must fix): the ordinal junk join from R6-05 still passes.** `kg_relmap.py:210`
```python
if xref or (ordinal and not strong_name) or not (strong_name or strong_vals):
```
The example in the fix list was `tbl0333.c_1 = tbl0339.c_1` with header **"1."** on both sides and values "2.", "3.", "4.". `placeholder("1.")` is `False` and `name_similarity("1.", "1.") == 1.0`, so `strong_name` is true and the `ordinal` exemption lets it through. Fix either:
- treat a header that is itself an ordinal (`_ORDINAL.match(header)`) as a placeholder; or
- drop `and not strong_name`, since identical *ordinal* headers are no evidence.

Add the case to a test.

**B2 (should fix): real headers treated as placeholders.** `kg_relmap.py:49`. `_PLACEHOLDER` matches `C1`, `C2`, `F1`, `f1`, which are real variable names in seismic tables (ASCE 43 has `f1`, `c_1` = "C1 coefficient"). Restrict the single-letter forms to the extractor's own generated names (`c_\d+`, `column_\d+`), or only when the original header was empty.

**B3 (note): ref bonus depends on a clean caption.** `names_table` looks for "Table N" in the *other* table's `table_label` or caption. Table 1-1 currently has neither (C1), so the +0.10 doesn't fire for R6-03. The join should still clear 0.55 via the values-only path; confirm on the real data after the rebuild.

### C. `src/kg_tablecheck.py`: front matter and captions

**C1 (must fix): `caption_fragment` misses its own example and hits real captions.** `kg_tablecheck.py:226–232`. The docstring names `'Rdilid | TABLE 1 Hazard Ca'` as a fragment, but the rule is "starts or ends with `|`, or ≤ 2 words and < 16 chars".

| Caption | `caption_fragment` | Should be |
|---|---|---|
| `Rdilid \| TABLE 1 Hazard Ca \| -1: THRESHOL tegory 2(A) \| …` | **False** | True (it's the header row) |
| `\| Millions` | True | True |
| `Damping Values` | **True** | False (a real caption would be replaced by the section heading) |

Suggestions:
- flag a caption equal to (or a prefix of) `" | ".join(first row)`;
- flag captions with ≥ 2 inner `" | "` separators and no `Table N`;
- drop the "≤ 2 words" rule, or require that the text also appears in the first row.

When replacing, also recover the "TABLE 1-1" token found inside the fragment as `table_label`; that fixes R6-04's visible symptom and turns on B3's bonus.

**C2 (should fix): index detection is too broad.** `kg_tablecheck.py:171`. `^\s*index\b` and `\bgeneral\s+index\b` flag data tables:
- section "Index Properties of Soils" → `index` (geotechnical data, e.g. liquid limit / plasticity index)
- caption "Table 4-1: General Index of Seismic Parameters" → `index`

Flagged tables are dropped from the table DB. Require that the heading or caption *is* an index (`^(subject|numerical|alphabetical|keyword|author|general)?\s*index$` or `index of interpretations`), and never flag a caption that starts with "Table N".

### D. `config/standards.json`: R6-02 isn't activated

**D1 (must fix):** the PR adds `scope` / `scope_end` to links (README, `kg_agent.py`, `kg_ingest.apply_scope`), but standard:28's link is unchanged (`config/standards.json:1622`):
```json
{ "text": "Appendix B", "url": "https://www.nrc.gov/reading-rm/doc-collections/cfr/part050/part050-appb" }
```
Add `"scope": "Appendix B to Part 50"` (and probably `"scope_end": "^Appendix C to Part 50"`), or point the link at the eCFR appendix URL. Then check the run report for "scope … kept N element(s)" and not "matched no heading".

**D2 (note):** `rt.scopes` is keyed by URL and uses `setdefault`. Two entries that link the same URL with different scopes would silently share the first scope, and the second entry's content would be missing. Make the document key include the scope, or reject conflicting configs.

### E. `src/kg_ingest.py`: entities

**E1 (should fix): hyphenated expansions fail.** `kg_ingest.py:1340`. `_initials` treats "Design-Basis" as one word:

| Pair | `is_abbreviation` |
|---|---|
| SDC / Seismic Design Category | True |
| PRA / Probabilistic Risk Assessment | True |
| **DBA / Design-Basis Accident** | **False** |
| **LBE / Licensing-Basis Event** | **False** |

These are core Part 53 / NEI 18-04 terms. Split on hyphens in `_initials` (try both forms), as the docstring promises.

**E2 (nit):** `singular("analyses") == "analyse"`. Add `-ses → -sis` for `analyses`, `bases`, `hypotheses`, `diagnoses`, or a small irregulars map.

**E3 (note):** `link_table_entities` matches single capitalised words, so "Category", "Table" and "Return" in captions will link to generic entities if they exist. Consider skipping single-word entities that are common nouns (no digit, not all-caps).

### F. Tests and docs

**F1 (should fix):** `README.md:517` lists "`test_round6.py` (lookup confidence, extraction recipe, error messages)", but no such file is in the PR. Add it or fix the README.

**F2 (should fix):** the only new test is the A572 lookup. Cheap unit tests to add, all runnable without PostgreSQL:
- the cases in A1, B1, C1 and C2 above, as regression tests;
- `apply_scope` (match, no match keeps everything, `scope_end`);
- `link_abbreviations` (one edge, no circle; hyphenated pair);
- `_near_ids` ("nonexistent" → none; one-letter typo found);
- `_check_entry`;
- the recipe-behind path in `_sync_one`, using the existing mock site.

**F3 (nit):** `kg_models.py:14–19`. The new `EXTRACTION_RECIPE` block sits between the "Publishers that sell standards" comment and `PAYWALL_HOSTS`, so that comment now describes the wrong line.

### G. Operational notes

- **The first run after merge rebuilds every document KG** (47 documents, including the OCR'd ASCE 43 and NQA-1 PDFs) and makes unconditional requests to every source. Expect a long run. Consider `--only` batches, or run it off-peak.
- **`_docs_behind` reads one `state.json` per document on every `list_entries` / `get_overview` call.** That's fine at 47 documents; cache it per run ID if the catalogue grows.
- **After the rebuild, re-run the round-6 checks:**
  - `get_overview` should show `entries_behind_extraction_recipe: []`;
  - the A572 lookup should return `no_confident_match`;
  - the NE-STD-1027 relationship map should include A-6 → Table 1-1;
  - `get_neighbors` on `ent:sdc` should show `abbreviation_of` and table MENTIONS;
  - `get_node("standard:999", …)` should report an unknown entry.

## Probe scripts

Run with the PR checked out, from `src/`. The outputs quoted above came from:
- `kg_tableservice.code_tokens` and `assess_lookup` on the questions in A1/A3, with a Table 1-1 row as currently loaded;
- `kg_relmap.placeholder`, `name_similarity` and `junk_values`;
- `kg_tablecheck.caption_fragment` and `front_matter`;
- `kg_ingest.singular`, `is_abbreviation` and `entity_key`;
- `kg_store.Store._near_ids` on a synthetic 8.6 k-node graph.
