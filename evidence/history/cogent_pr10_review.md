# Review: PR #10 "Updated fixes post-testing" (NearIdentity/cogent)

- **Commit:** 624271c, on top of main 9da80ec, where PR #9 is merged
- **Size:** 12 files, +587/−66
- **Reviewed against:** my PR #9 review (cogent_pr9_review.md)
- **Date:** 2026-10-27

## Verdict

**Approve after one config change. Two smaller follow-ups are recommended.**

Every code item from the PR #9 review is addressed, and each has a test. I re-ran the probes that found the original bugs, and they now behave correctly. One item is still open:

- **D1:** link scope for standard:28 is not done. The PR doesn't touch `config/standards.json`.

The probes also turned up one new correctness issue, and it is worth fixing in the same pass:

- **N1:** bare document numbers (`RA-S-1.4` becomes `1.4`) give false "lexical" matches.

## Test run

| Run | Passed | Skipped | Failed |
|---|---|---|---|
| Full suite (PG16 + pgvector) | 329 | 4 | 5 |

- The 5 failures are the same ones that fail on main: `test_table_footnotes` ×4 and `test_ocr::test_real_rapidocr`. They come from library versions in my environment, not from this PR.
- The new `tests/test_round6.py` (~30 tests) passes in full.

## Status of each PR #9 review item

| # | Item | Status | Evidence (direct probe on the PR branch) |
|---|---|---|---|
| A1 | All-token gate gave false negatives | **Fixed** | `"HC-3 threshold quantity for Cs-137"` against the Cs-137 row of Table 1-1 now returns `partial`, with `missing_terms=['HC-3']` (it was `none`). A row with only Co-60 returns `none`. |
| A2 | Similarity floor only settable through an env var | **Fixed** | Added a `min_similarity` preference (validated 0..1), passed per call and set per profile (strict = 0.70). The effective floor is the larger of the preference and the env var. The docs call 0.70 provisional, which is the right framing. |
| A3 | RA-S identifiers not recognised | **Fixed, but see N1** | `RA-S-1.4` is now detected. It is reduced to its bare number, though. |
| A4 | Neighbouring cells could join into a false hit | **Fixed** | `_fields()` matches each field on its own. |
| B1 | Ordinal headers ("1.", "2)") passed as real names | **Fixed** | `placeholder("1.")` is True. |
| B2 | `_PLACEHOLDER` too broad | **Fixed** | `C1` and `f1` are no longer placeholders. `c_3` still is. |
| C1 | Rdilid caption fragment missed; "Damping Values" wrongly flagged | **Fixed** | `caption_fragment(Rdilid…)` is True, and `recover_label` returns `Table 1-1`. `"Damping Values"` is False. `"TABLE 3 \| -2 Specified Damping"` recovers to `Table 3-2`. |
| C2 | Index regex matched "Index Properties of Soils" | **Fixed** | That title returns None now. `SUBJECT INDEX`, `A. INDEX` and `… > NUMERICAL INDEX` are still detected. A caption beginning with "Table 9-1: Index of symbols" is exempt, as intended. |
| D1 | standard:28 ingests all of Part 50, not just Appendix B | **Open** | `config/standards.json` is unchanged. The `Appendix B` link at about line 1622 has no `"scope"`. The scope code paths are in place, tested and documented; they just aren't switched on for this entry. |
| D2 | Entries disagreeing about a scope were ignored silently | **Fixed** | The agent now records an error saying the entries disagree and the first scope is used. |
| E1 | DBA / LBE not detected as abbreviations | **Fixed** | DBA, LBE, BDBE, SSC and AOO are detected. |
| E2 | analyses → analyse | **Fixed** | Gives analysis, criterion, phenomenon, index, hypothesis. Dose, house, clause and release are unchanged. |
| E3 | Single-word entity links too loose | **Fixed** | A single-word entity now needs a digit, all caps, or an inner capital. |
| F1/F2 | Missing regression tests | **Fixed** | Added `tests/test_round6.py`. |
| F3 | Comment out of place | **Fixed** | The PAYWALL_HOSTS comment is back above its definition. |
| — | Recipe version | Bumped | `EXTRACTION_RECIPE = "10"`, so existing KGs are rebuilt once. That is expected, given the singular/abbreviation changes. |

## Remaining issues

### Must fix

**D1. Set the scope on standard:28's Appendix B link.**

```json
{ "text": "Appendix B",
  "url": "https://www.nrc.gov/reading-rm/doc-collections/cfr/part050/part050-appb",
  "scope": "Appendix B to Part 50" }
```

- Without this, the KG for entry 28 still overlaps with the whole of Part 50 (entry 1), not just the QA appendix.
- After the change, check that the scope matches a heading on the page. If it doesn't, the agent's new "scope not found" error will say so. Then rebuild entry 28.

### Should fix

**N1 (new). Bare document numbers give false lexical matches.**

`_CODE_B` keeps only the number group, so `RA-S-1.4` becomes `1.4`, which normalises to `14`. A substring check then matches unrelated cells.

| Question | Cell value | Result |
|---|---|---|
| "RA-S-1.4 requirements" | `140` | lexical ✗ |
| "RA-S-1.4 requirements" | `2014` | lexical ✗ |
| "RG 1.233 requirements" | `140` / `2014` | none ✓ |

- Any short number has the same problem: RG 1.9 becomes `19` and RG 1.28 becomes `128`.
- Other identifiers lose their prefix too: `NEI 18-04` becomes `18-04`, `DOE-STD-1271-2025` becomes `1271-2025`, and `RA-Sb-2013` becomes `Sb-2013`.
- Suggested fix: keep the family prefix in the needle (`rg128`, `ras14`). Alternatively, match the bare number only on token boundaries in the normalised field and with at least 3 digits.
- Add tests for "RA-S-1.4" against `140` / `2014`, and for "RG 1.9" against `19.5`.

**N2. `_NOT_CODES` drops nuclides written without a hyphen.**

- `u235`, `u238` and `pu239` are listed as non-codes, so "threshold for U235 in grams" yields no subject. "U-235" works.
- These spellings are common in questions. Recommendation: remove them from `_NOT_CODES`, or map `U235` to `U-235`.

### Nits

- **`bases` → `basis`:** This is right for "licensing bases", but it also merges the plural of "base" (column bases, base plates). It's acceptable for this corpus. Consider applying the rule only when the singular candidate is a known `-sis` word.
- **`_EXPAND` covers only hc and sdc:** The other `_CATEGORY` prefixes (ndc, pdc, ldc, risc, ql…) rank rows but never match their spelled-out forms. Add expansions where the meaning is unambiguous, e.g. `ldc` → "limit state" is *not* safe, but `risc` → "risk-informed safety class" is.
- **Numeric-only headers are now placeholders:** `placeholder("10")` and `placeholder("100")` are True. That's fine for join inference, which is the only caller (kg_relmap). Note, though, that ASME stress tables use temperature columns ("100", "200"…), so two such tables won't be suggested as a name-based join. That may be the desired behaviour, but say so in a comment.
- **Real two-pipe captions:** `caption_fragment` flags any caption with two or more "|", e.g. "Response | Level 1 | Level 2". Real captions almost never contain pipes, so this is low risk.
- **Hyphen-split label recovery:** `recover_label("| | | Ta | ble A-6 – R | adio…")` returns `''`, because the word "Table" is itself split. It's a rare case and can be left for later.
- **More abbreviations that still fail:** DID / "Defense-in-Depth" (the stopword "in" is dropped) and NSRSS. Consider keeping short connector words when the abbreviation has a letter for them.

## Summary for the author

The PR is clean, well tested and does what the PR #9 review asked. Before merging:

1. Add the `scope` to standard:28's Appendix B link and rebuild entry 28 (D1).
2. Preferably also fix the bare-number matching (N1) and the hyphen-less nuclides (N2), with tests.

The nits can wait for a later round.

*Note: I couldn't post this on the PR, because this session has no write access to NearIdentity/cogent.*
