# PR: handle HTTP 403 politely (`claude/http403-fixes`)

## 1. Features developed

**Problem.** `python3 src/kg_agent.py run --tables` produced many HTTP 403 errors on www.nrc.gov (and on publisher store pages), plus 404s on energy.gov trackers and noisy log lines. The agent posed as Chrome, retried, then escalated to headless Chromium after a 403; the NRC's CDN (403 with `x-reference-error`) blocks non-browser clients, and the block outlasted the run.

**Phase 1: polite fetching** (`src/kg_hosts.py` new; `kg_tools.py`, `kg_ingest.py`, `kg_agent.py`, `kg_models.py`)
* Honest user agent `cogent-kg-agent/<version>`, overridable with `KG_USER_AGENT` (now honoured by `kg_tools.fetch`, which ignored it). No browser disguise, no `Referer`, no automation-hiding Chromium flag.
* No automatic headless-browser fallback after a 403; opt-in with `KG_BROWSER_FALLBACK=1`, once per request, never during a cooldown.
* Persisted per-host cooldown (`<home>/state/hosts.json`) after a 403, a 429, or a 503 with `Retry-After`: 6 h, doubling to a 7-day cap, never shorter than `Retry-After`; no retries or polling inside it; resets after a good response.
* Deferral: documents and trackers on a blocked host are `skipped` ("deferred: host is blocking this client"), keep their previous KG and do not increment `consecutive_errors` / `STALE`.
* Per-host pacing with jitter and `--host-interval HOST=SECONDS` (defaults: www.nrc.gov 4 s, adams-api.nrc.gov 2 s, otherwise `--min-interval`); work interleaved round-robin across hosts.
* `"manual": true` links: never requested; the report lists the URL to download and the path to save to (alert `manual_download_needed`).
* `kg_agent.py fetchcheck URL`: one request, prints status, filter headers, body snippet, user agent, verdict.
* Reporting: "blocked by a bot filter (HTTP 403 from host)"; tracker 404/410 says to update `update_tracking_urls`; per-host "Fetch problems" section and `fetch_problems` / `docs_deferred` in the summary; info alerts logged at INFO; a 403 on a paywalled store page is the info alert `store_page_blocked`, not an error; one image warning per host and run.

**Phase 2: ADAMS Public Search adapter** (`src/kg_adams.py`)
* `get_document` / `search` against the APS API with `NRC_ADAMS_API_KEY` (header `Ocp-Apim-Subscription-Key`); accession parsing for both `ML` forms.
* Change detection only: fingerprint = accession, date added, document date, hash of content. Unchanged means nrc.gov is not contacted. API failure falls back to the normal fetch. Without the key it is off.
* `kg_agent.py adamscheck ML…`: one request, redacted summary. Requests counted in the summary (`adams_requests`).

**Tests and docs.** `tests/test_fetch_blocking.py` (19) and `tests/test_adams.py` (10), mock servers only. New `docs/fetching.md`; README, `docs/observability.md`, `docs/scheduling.md`, `CHANGELOG.md`, MCP alert notes (`kg_store.ALERT_NOTES`) updated.

**Test status.** Full suite: 320 passed, 46 skipped, 4 failed; the 4 are the pre-existing `test_table_footnotes.py` failures with pymupdf 2.x.

**Not changed / not verified.** Nothing was run against the live nrc.gov, adams-api.nrc.gov, ascelibrary.org or webstore.ansi.org. Open: whether the CDN accepts the honest agent, real block durations, live APS response shape beyond the sample used for the synthetic fixture, and the `search` body (written from memory, unused by the agent). The energy.gov URLs of entries 11–13 are unchanged (404, correct URL unknown).

## 2. Choices and rationale

| Choice | Why |
|---|---|
| Honest user agent instead of a better disguise | The user's curl tests showed browser-style agents were refused while plain curl once worked, then all non-browser clients were blocked. A disguise is fragile, arguably deceptive, and was the likely trigger. Identifying ourselves lets an operator allow-list or contact us. |
| Dropped automatic Chromium fallback (and the Playwright-Firefox idea) | It escalated a refusal into more requests and got 403 as well. Opt-in only. |
| Persisted cooldown of hours, growing | Blocks lasted 44+ min and rate-based IP blocking was plausible; polling can extend a block. Persisting protects the next run and the daemon, whose retries would otherwise repeat refused requests. |
| Deferral is `skipped`, not `error` | A block is the host's state, not the document's. Counting it would turn healthy documents `STALE` and fire the failure-rate alert. |
| Per-host intervals, jitter, interleaving | Spend one host's wait on the others; avoid a regular request cadence. 4 s for nrc.gov is a conservative guess; configurable. |
| `manual` links | Some files can only be fetched by a person in a browser; the agent should say exactly what to download and where, not fail repeatedly. Reuses the existing local-copy mechanism. |
| `fetchcheck`, one request, no `--recover` | Diagnosis without feeding a block; a polling recovery test was shown to be counterproductive. Refuses during a cooldown unless told otherwise. |
| ADAMS API for change detection only | A sanctioned route that avoids contacting the CDN. The user decided against text-only graphs (the PDF stays the source: tables, figures, layout). `DocumentType` is not trusted. Fingerprint includes a content hash because dates alone can miss re-issues. |
| Key only from the environment, redacted everywhere | Secret hygiene: never logged, stored, or put in reports; tested. |
| 2 s between API requests | User's decision. |
| `store_page_blocked` as an info alert | A refused store page is expected and loses nothing; the user asked for it not to be a run error. |
| `FetchResult` gained `blocked` / `deferred`; `fetch` signature kept | Existing callers keep working; `browser_fallback` default changed from True to None (env opt-in). |
| Mock-server tests, synthetic APS fixture | No network and no NRC text committed. |
| energy.gov config left alone | Guessing a URL would silently track the wrong page. |

## 3. Synopsis of the interactive discussion

1. **Request.** Start from latest `main`; diagnose and fix the console errors (especially 403s); describe changes first, create `claude/http403-fixes` after approval, add unit tests.
2. **Recommendation asked** for nrc.gov 403s. I first suspected missing headers, cookies or a headless fingerprint.
3. **User's curl evidence:** the PDF opens in Firefox; plain curl returned 200 while a Chrome-style UA with Referer returned 403 with `x-reference-error`. I revised the diagnosis to a client-keyed bot filter.
4. **User:** the 403 may also be rate-based by IP (a PDF failed to load in a browser for minutes after a run). I proposed and walked through a 5-way curl test; the user ran it and every variant returned 403, so UA and rate could not be separated.
5. **Recovery loop.** A polling test ran 30+ minutes without recovery; I advised stopping, since polling can prolong a block, and moved to single checks.
6. **Side request:** a lighter LLM than llama3.1:8b for Ollama KG edge ingestion (GPU at 100% for 12–15 h), plus heavier options. Answered separately; unrelated to the code.
7. **Playwright Firefox** also got 403, so the browser-fallback idea was dropped.
8. **Sanctioned route.** The user found the ADAMS Public Search API. We reviewed the PyPI package `adamsnrc` (not adopted), the uploaded APS guide PDF, key setup (use the primary key; stored in a mode-600 curl config, never pasted), and the user ran `GetDocument` steps locally (HTTP 200, no usage limits). I analysed the real response shape (keys only) for the adapter and fixture.
9. **Plan presented** (Phase 1 politeness, Phase 2 ADAMS). User decisions: no text-only graphs but change detection is useful; one request every 2 s; "Approved to go ahead with Phases 1 and 2." Left unanswered: nrc.gov default interval (I chose 4 s) and a per-URL minimum re-check age (not added).
10. **Implementation, tests, push** (`d272764`). Then the user asked for documentation updates in all `.md` files and this summary; I updated README, `docs/observability.md`, `docs/scheduling.md`, `CHANGELOG.md`, `docs/fetching.md`, and the MCP alert notes in code.

Standing rules observed: no PR opened; secrets kept out of output and the repository; commit trailers added; results reported as measured, with live behaviour marked unverified.
