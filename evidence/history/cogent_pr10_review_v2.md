# Re-review: PR #10, after the follow-up commit (NearIdentity/cogent)

- **Commits:** 624271c "Updated fixes post-testing" and 0659c5c "Follow-up fixes", on top of main 9da80ec
- **Follow-up commit:** 4 files, +57/−10 (kg_tableservice, kg_ingest, kg_relmap, test_round6)
- **Reviewed against:** my first review of this PR (cogent_pr10_review.md)
- **Date:** 2026-10-27

## Verdict

**Approve.** D1 is deferred at the author's request, because `config/standards.json` is rewritten by the code at runtime. N1b and the nits can go in a later round.

The follow-up commit addresses N1, N2 and three of the nits, each with a test. Two things remain:

- **D1** is still open: `config/standards.json` hasn't changed in either commit.
- **N1** is mostly fixed. A bare number such as `1.4` now has to stand as a whole token, but it still matches any whole `1.4` (see N1b).

## Test run

| Run | Passed | Skipped | Failed |
|---|---|---|---|
| Full suite (PG16 + pgvector) | 332 | 4 | 5 |
| `tests/test_round6.py` | 33 | 0 | 0 |

- The 5 failures are the same environment ones that fail on main: `test_table_footnotes` ×4 and `test_ocr::test_real_rapidocr`.
- The follow-up adds 3 tests, so the totals moved from 329 to 332 passed.

## Status of the items from my first review

| # | Item | Status | Evidence |
|---|---|---|---|
| D1 | Set the scope on standard:28's Appendix B link | **Open** | No change under `config/`. The link still has no `"scope"`. |
| N1 | Bare document numbers matched as substrings | **Mostly fixed** | A bare number must now be a whole token. `RA-S-1.4` no longer matches `140`, `2014`, `1.40` or `1.4.2.1`. `RG 1.9` doesn't match `19.5`, `RG 1.28` doesn't match `128`, and `NEI 18-04` doesn't match `1804` or `2018-04-01`. `NUREG-0800` doesn't match `800`. All other identifiers still match on normalised substrings, so `A572` still matches `ASTM A 572`. |
| N2 | Nuclides written without a hyphen | **Fixed** | `U235`, `U238` and `Pu239` are no longer in `_NOT_CODES`. `U235` matches `U-235` and the reverse; `Pu239` matches `Pu-239`. `H2O`, `CO2` and `mm2` are still excluded. |
| Nit | `_EXPAND` covered only hc and sdc | **Fixed** | Added `risc` → "risk informed safety class". `RISC-3` matches "Risk Informed Safety Class 3". |
| Nit | Numeric headers count as placeholders | **Documented** | `placeholder()` has a comment explaining that temperature-column tables get no name-based join. |
| Nit | DID / "Defense-in-Depth" not detected | **Fixed** | `_initials` gained a third form that keeps connector words. DID is detected, and "Design Basis" is still rejected. |
| Nit | `bases` → `basis` | Not changed | Acceptable for this corpus. |
| Nit | Captions with two pipes flagged as fragments | Not changed | Low risk. |
| Nit | Hyphen-split label recovery (`Ta \| ble A-6`) | Not changed | Rare; can wait. |
| Nit | NSRSS not detected | Not changed | Unusual acronym. |

## Remaining issues

### Must fix

**D1. The Appendix B scope on standard:28 is still missing.**

The suggested change is the same as before:

```json
{ "text": "Appendix B",
  "url": "https://www.nrc.gov/reading-rm/doc-collections/cfr/part050/part050-appb",
  "scope": "Appendix B to Part 50" }
```

After the change, rebuild entry 28 and check that the run doesn't report "scope not found".

If the author has decided to keep ingesting the whole page, say so in the entry's `note` and close D1. As it stands, the config and the stated intent ("Appendix B covers QA") disagree.

### Should fix (can go in a later round)

**N1b. A bare number still matches any whole token that equals it.**

`_CODE_B` still throws away the family name, so "RA-S-1.4" and "RG 1.4" both reduce to `1.4`. Whole-token matching then accepts:

| Question | Where `1.4` appears in the row | Result |
|---|---|---|
| "RA-S-1.4 requirements" | data cell `Damping = 1.4` | lexical ✗ |
| "RA-S-1.4 requirements" | caption "Table 1.4 Damping values" | lexical ✗ |
| "RA-S-1.4 requirements" | section "1.4 Scope" | lexical ✗ |
| "RG 1.4 requirements" | cell `RA-S-1.4` (another document) | lexical ✗ |

- The new test asserts that `"section 1.4"` gives `lexical`, so it codifies one of these false matches.
- The risk is narrow, since only questions that cite a document number are affected. When it does happen, though, the lookup reports a confident match on a row about something else.
- Suggested fix, either of:
  - Treat a bare-number hit as `partial` unless the same field also contains the family name or a known alias (`RA-S`, `RG`/`Regulatory Guide`, `NEI`, `NUREG`). `"Regulatory Guide 1.9"` and `"NEI 18-04 Rev. 1"` would still be lexical.
  - Or keep the family in the needle, with aliases (`rg` ↔ `regulatorguide`), and match the normalised field.
- Then change the `"section 1.4"` case in `test_bare_document_numbers_match_whole_tokens_only` to expect `partial` or `none`.

### Nits

- **Keeping connector words loosens abbreviation matching.** The third initials form makes more acronyms match: TOC, SOC, DOT, POA, IAT and ITA all match their expansions now. Those are all correct, but an acronym and an unrelated phrase that share initials only once connector words count will now get an `abbreviation_of` edge. Consider using that form only when the short form has a lowercase letter or exactly matches a hyphenated long form (DiD, Defense-in-Depth).
- **Unused fixture in `test_more_abbreviations_and_risc_label`.** `row = _row(...)` is built and then only checked with `assert row`. Either assert something about it (e.g. that `RISC-2` matches its caption) or remove it.
- **`_fields` docstring.** Fields are now only lower-cased, not normalised. Non-numeric tokens are normalised inside `_in_fields`. That is correct, but the docstring could say where normalisation now happens.

## Summary for the author

The PR is in good shape: 332 tests pass, and every code finding from both reviews is fixed or documented.

1. Before merging, resolve D1: either add the scope to standard:28's Appendix B link and rebuild entry 28, or document that the whole page is intended.
2. N1b, making a bare-number hit need its family name to be lexical, can go in the next round along with the nits.

*I couldn't post this on the PR, because this session has no write access to NearIdentity/cogent.*
