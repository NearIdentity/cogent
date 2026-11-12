# Fetching politely: user agent, cooldowns, browser route, manual links, ADAMS

Some hosts (www.nrc.gov behind its CDN, publisher and standards stores) answer **HTTP 403** to anything they take for a bot. The agent does not try to get around that. This page says what it does instead. Code: `src/kg_hosts.py`, `src/kg_tools.py` (`fetch`), `src/kg_ingest.py` (`_fetch_http`), `src/kg_adams.py`.

## What the agent does

* **Honest user agent.** `cogent-kg-agent/<version>` on every request, overridable with `KG_USER_AGENT` (for example `my-lab-bot/2 (ops@example.org)` so a site operator can contact you). No `Mozilla/...` string, no `Referer`, no browser-style headers, no `--disable-blink-features=AutomationControlled`.
* **No automatic headless browser.** A 403 is not retried with Chromium. A browser fallback is opt-in (`KG_BROWSER_FALLBACK=1`), is tried once per request and never during a cooldown.
* **Cooldown, stored on disk** (`<home>/state/hosts.json`). A **429** or a **503 with `Retry-After`** puts the whole host in a cooldown. A **403** is attributed to one *area* of the host (its first path segment, e.g. `www.nrc.gov/docs`, or `host/` for files at the root), because a 403 is often a rule on one kind of resource: on 2026-11-11 the NRC refused its `/docs/ML….pdf` files while its HTML pages answered the same agent in the same minutes. Other areas keep working. Three refused areas of one host are treated as the whole host. A cooldown lasts 6 hours, doubling each time the host blocks again after a cooldown (12 h, 24 h ... capped at 7 days), and never shorter than `Retry-After`. Nothing is requested from the host (or area) during a cooldown, in this run or later ones, and nothing is retried or polled. A good response after a cooldown resets the ladder. To lift a cooldown by hand, delete its entry (`www.nrc.gov/docs` or `www.nrc.gov`) from `hosts.json`. Entries written by earlier versions are whole-host entries and block every area: delete them.
* **Deferral, not failure.** Documents on a blocked host are reported as `skipped` ("deferred: host is blocking this client"). They keep their previous KG and do **not** increase `consecutive_errors`, so nothing becomes `STALE` because of a block. Trackers are handled the same way.
* **Pacing and interleaving.** At least `--min-interval` seconds between requests to one host (default 1), with +/-25 % jitter; built-in defaults of 4 s for `www.nrc.gov` and 2 s for `adams-api.nrc.gov`; override per host with `--host-interval HOST=SECONDS` (repeatable). Work is ordered round-robin over hosts, so the wait imposed on one host is spent on the others.
* **A person's browser as the fallback.** At a terminal the agent offers to open a refused URL in your browser, after showing the ADAMS record; the saved file is verified against ADAMS (sections below). Unattended runs only list what could not be downloaded.
* **Images.** Figures are fetched with the same user agent and cooldowns; a blocked host gets one warning per run, not one per image.

## Reporting

* The run summary has `fetch_problems` and `docs_deferred`; `logs/report.md` gets a section "Fetch problems by host" (host or area, problem, number deferred, requests made, cooldown end, URLs that no longer exist) and, when documents were refused, "Could not be downloaded automatically" (URL and the path to save a browser download to).
* A 403 reads "blocked by a bot filter (HTTP 403 from host)". A 404 or 410 on a tracker reads "tracker URL not found (HTTP 404): it may be gone (update `update_tracking_urls`), or the host may hide it from this client; open it in a browser or run `kg_agent.py fetchcheck`". Some hosts answer 404 to clients they dislike, so a 404 is not proof that a URL is dead.
* Alerts are logged at their own severity (info alerts are no longer WARNING lines).
* A 403 on a store page of a paywalled document (`ascelibrary.org`, `webstore.ansi.org`, ...) is the info alert `store_page_blocked`, not a run error: the previous KG is kept and the alert asks you to look at the page in a browser.

## Manual links

The browser route below is the default. Add `"manual": true` to a link in `config/*.json` only for a URL the agent should never request at all (for example a document that is superseded, or a host you know will refuse):

```json
{"text": "RG 1.233 Rev. 1", "url": "https://www.nrc.gov/docs/ML2xx/ML2xxxxxxxx.pdf", "manual": true}
```

Download the file in your browser and save it in the entry's local-copy folder (`<home>/local/<standards|reports>/<id>/`, or the entry's `local_copy`). Until it is there the report carries the warning **MANUAL DOWNLOAD NEEDED** with the URL to download and the exact path to save to; the folder is created for you.

**Watching manual NRC links.** With `NRC_ADAMS_API_KEY` set, a manual link on `nrc.gov/docs/ML…` is still watched through the ADAMS API (one request per run, never to www.nrc.gov). The first fingerprint seen is the baseline. If the record later changes, the warning **MANUAL DOCUMENT CHANGED** names the accession, the URL to download and the path to save to, and stays open until the saved file is newer than the change (state: `state/adams_manual.json`). Without the key nothing is watched.

## When a host never lets the agent in

Deferred documents are not `STALE`, so a permanent block would be silent. If documents of an entry on a blocked host have not been checked for `--deferred-warn-days` days (default 7; counted from the last successful check, or from the first block), the warning **NOT CHECKED FOR DAYS** appears in the report, the log and `get_warnings`. Practical answers: the browser route (at a terminal), `"manual": true` links with the ADAMS watch, or asking the host to allow-list the agent's user agent.

## When a download is refused: the browser route (instead of forcing `manual`)

You do not have to mark links `"manual": true` for the agent to be useful when a host refuses it.

* **At a terminal** (`--manual-prompt auto`, the default; `always` / `never` override), after a refused download the agent shows the document and asks whether to open it in your browser:
  ```
  The automated download failed: https://www.nrc.gov/docs/ML1801/ML18019A922.pdf
    reason: blocked by a bot filter (HTTP 403 from host)
    ADAMS record (read through the API; the type is shown but never trusted):
      accession        ML18019A922
      title            ...
      document date / added to ADAMS / pages (estimate) / author / docket
    full URL:  https://www.nrc.gov/docs/ML1801/ML18019A922.pdf
    save as:   kg_agent_home/local/standards/18/ML18019A922.pdf
  Open the URL in your browser and save the file there? [y]es / [n]o / [a]ll remaining / [s]kip all:
  ```
  The ADAMS record is read first (needs `NRC_ADAMS_API_KEY`; without it the form says it was not checked). On `y` the URL is opened in your default browser, the agent waits for you to save the file, checks it against the ADAMS record, and ingests it in the same run. From then on the link counts as satisfied by the saved file (same file name as the URL's last segment): it is not requested again, and it is watched through ADAMS like a manual link.
* **Unattended** (cron, systemd, `kg_agent.py daemon`, `kg_daemon.py`): never prompts. The report gets a section "Could not be downloaded automatically" with each URL and the path to save it to.

## Verifying local copies against ADAMS (`--verify-local warn|strict|off`)

A local copy whose file name is an accession number (`ML18019A922.pdf`, wherever it came from: your browser, a copy, the prompt above) is compared with its ADAMS record on every run (one API request per accession; needs the key). The file's SHA-256, size, modification time, PDF page count and PDF dates are stored with the verdict in `state/local_verification.json`; an unchanged file whose record is unchanged is not re-checked. The checks:

| check | fails when |
|---|---|
| file name | it names a different accession than the record |
| text | fewer than 60 % of the 4-word phrases of the ADAMS text occur in the file's text (a similarity: ADAMS and a PDF reader extract differently); skipped for scans and very short texts |
| pages | the page count differs from the ADAMS estimate by more than 2 pages and 20 % |
| dates | the PDF's creation or modification date is more than 2 days after the record was added to ADAMS |
| tampering | the content changed since the last verification but the modification time did not |

A failure raises the warning **LOCAL COPY DOES NOT MATCH ADAMS** (details: the failed checks, the accession, the file hash). In `warn` mode (default) the file is still ingested; in `strict` mode it is not. These are heuristics for catching a wrong or altered file (including a planted one); they are not proof of authenticity, and a document whose ADAMS text is missing is only checked by name, pages and dates. The thresholds are untested against real NRC documents.

## `fetchcheck`: one request, no polling

```bash
python3 src/kg_agent.py fetchcheck https://www.nrc.gov/docs/ML1801/ML18019A922.pdf
```

Makes **one** request and prints the client used, the status, the final URL after redirects, the page title, the headers that identify a filter (`Server`, `Via`, `Retry-After`, `x-reference-error`, ...), a snippet of the body, and a verdict. A block it sees starts a cooldown like any other. If the host is in a cooldown it makes no request (`--ignore-cooldown` overrides; that can prolong a block). Run it once, not in a loop: repeated requests while blocked can make a block last longer.

## ADAMS Public Search API (change detection, watch and verification)

For `nrc.gov/docs/ML....pdf` links, the agent can ask the sanctioned ADAMS Public Search API whether the record changed instead of requesting the PDF.

1. Register for a key at the NRC's ADAMS API page and keep it **out of the repository**: `export NRC_ADAMS_API_KEY=...` in the shell or service environment that runs the agent (or a git-ignored env file). The key is sent in the `Ocp-Apim-Subscription-Key` header and is never logged, printed, written to reports or stored in state.
2. With the key set, each ML link, and each `update_tracking_urls` entry that is an ML PDF, costs one API request (`GET /aps/api/search/{accession}`, one every 2 s). The fingerprint is `AccessionNumber | DateAddedTimestamp | DocumentDate | hash of content`. Same as last time: the document is "unchanged (ADAMS record ... unchanged)" and www.nrc.gov is not contacted. Different, or the API fails: the PDF is fetched as usual.
3. The API text is not used to build graphs (the PDF stays the source) and `DocumentType` is not trusted. Both response shapes (the record itself, or an envelope with `document`) are accepted; the undocumented `DocumentSize` field is ignored. Requests are counted in the summary (`adams_requests`).
4. `python3 src/kg_agent.py adamscheck ML18019A922` makes one request and prints a redacted summary (top-level keys, fingerprint fields, never the key or the text).

Without the variable, ADAMS is off and nothing changes: no change detection, no watch, no verification, and the prompt's form says the record was not checked. The record's text is kept in memory only for the comparison with a local copy; it is never printed, logged or stored.

## What was not verified

Everything above was tested against local mock servers (`tests/test_fetch_blocking.py`, `tests/test_adams.py`, `tests/test_manual_watch.py`, `tests/test_manual_prompt.py`). It has **not** been run against adams-api.nrc.gov beyond `adamscheck` and `GetDocument`, nor against ascelibrary.org or webstore.ansi.org. Not checked live: the interactive prompt with a real browser; the verification checks on a real NRC PDF and its ADAMS record (the thresholds for text overlap, page count and dates are guesses); the `AdamsClient.search` body (written from memory; the agent uses only `get_document`).

What the maintainer's machine showed (2026-11-12): the 12 NRC PDFs were staged on 2026-10-12 and checked without errors in every run through 2026-10-27, and fail with "HTTP 403 Forbidden; browser fallback failed: HTTP 403" in every run from 2026-11-11 (the first run after a two-week gap), so the refusal began in that window and applies to headless Chromium as well. In the same runs the NRC HTML pages were fetched without errors, and a `fetchcheck` of one returned 200 with the honest user agent. A browser loads the PDFs; plain `curl` (default user agent), Python `urllib` and the honest agent all got 403 for a PDF from two networks (one a phone hotspot). Not established: why the rule changed, or whether it will change again. The dead energy.gov tracker `ehss/nuclear-and-facility-safety-directives` (404 in a browser too) was removed from entries 11 to 13 of `config/standards.json`; their other URLs return 200.

## Related documents

README ("The update agent", "Paywalled documents", environment variables), `docs/scheduling.md` (blocked hosts and retries), `docs/observability.md` (spans and failure counters), `CHANGELOG.md`.
