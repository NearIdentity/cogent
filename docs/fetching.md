# Fetching politely: user agent, cooldowns, manual links, ADAMS

Some hosts (www.nrc.gov behind its CDN, publisher and standards stores) answer **HTTP 403** to anything they take for a bot. The agent does not try to get around that. This page says what it does instead. Code: `src/kg_hosts.py`, `src/kg_tools.py` (`fetch`), `src/kg_ingest.py` (`_fetch_http`), `src/kg_adams.py`.

## What the agent does

* **Honest user agent.** `cogent-kg-agent/<version>` on every request, overridable with `KG_USER_AGENT` (for example `my-lab-bot/2 (ops@example.org)` so a site operator can contact you). No `Mozilla/...` string, no `Referer`, no browser-style headers, no `--disable-blink-features=AutomationControlled`.
* **No automatic headless browser.** A 403 is not retried with Chromium. A browser fallback is opt-in (`KG_BROWSER_FALLBACK=1`), is tried once per request and never during a cooldown.
* **Per-host cooldown, stored on disk** (`<home>/state/hosts.json`). The first 403, a 429, or a 503 with `Retry-After` puts the host in a cooldown: 6 hours, doubling each time the host blocks again after a cooldown (12 h, 24 h ... capped at 7 days), and never shorter than `Retry-After`. Nothing is requested from the host during a cooldown, in this run or later ones, and nothing is retried or polled. A good response after a cooldown resets the ladder. To lift a cooldown by hand, delete the host's entry from `hosts.json`.
* **Deferral, not failure.** Documents on a blocked host are reported as `skipped` ("deferred: host is blocking this client"). They keep their previous KG and do **not** increase `consecutive_errors`, so nothing becomes `STALE` because of a block. Trackers are handled the same way.
* **Pacing and interleaving.** At least `--min-interval` seconds between requests to one host (default 1), with +/-25 % jitter; built-in defaults of 4 s for `www.nrc.gov` and 2 s for `adams-api.nrc.gov`; override per host with `--host-interval HOST=SECONDS` (repeatable). Work is ordered round-robin over hosts, so the wait imposed on one host is spent on the others.
* **Images.** Figures are fetched with the same user agent and cooldowns; a blocked host gets one warning per run, not one per image.

## Related documents

README ("The update agent", "Paywalled documents", environment variables), `docs/scheduling.md` (blocked hosts and retries), `docs/observability.md` (spans and failure counters), `CHANGELOG.md`.

## Reporting

* The run summary has `fetch_problems` and `docs_deferred`; `logs/report.md` gets a section "Fetch problems by host" (problem, number deferred, requests made, cooldown end, URLs that no longer exist).
* A 403 reads "blocked by a bot filter (HTTP 403 from host)". A 404 or 410 on a tracker reads "tracker URL not found (HTTP 404): it may be gone (update `update_tracking_urls`), or the host may hide it from this client; open it in a browser or run `kg_agent.py fetchcheck`". Some hosts answer 404 to clients they dislike, so a 404 is not proof that a URL is dead.
* Alerts are logged at their own severity (info alerts are no longer WARNING lines).
* A 403 on a store page of a paywalled document (`ascelibrary.org`, `webstore.ansi.org`, ...) is the info alert `store_page_blocked`, not a run error: the previous KG is kept and the alert asks you to look at the page in a browser.

## Manual links

Add `"manual": true` to a link in `config/*.json` and the agent never requests that URL:

```json
{"text": "RG 1.233 Rev. 1", "url": "https://www.nrc.gov/docs/ML2xx/ML2xxxxxxxx.pdf", "manual": true}
```

Download the file in your browser and save it in the entry's local-copy folder (`<home>/local/<standards|reports>/<id>/`, or the entry's `local_copy`). Until it is there the report carries the warning **MANUAL DOWNLOAD NEEDED** with the URL to download and the exact path to save to; the folder is created for you.

## `fetchcheck`: one request, no polling

```bash
python3 src/kg_agent.py fetchcheck https://www.nrc.gov/docs/ML18/ML18019A922.pdf
```

Makes **one** request and prints the client used, the status, the final URL after redirects, the page title, the headers that identify a filter (`Server`, `Via`, `Retry-After`, `x-reference-error`, ...), a snippet of the body, and a verdict. A block it sees starts a cooldown like any other. If the host is in a cooldown it makes no request (`--ignore-cooldown` overrides; that can prolong a block). Run it once, not in a loop: repeated requests while blocked can make a block last longer.

## ADAMS Public Search API (change detection only)

For `nrc.gov/docs/ML....pdf` links, the agent can ask the sanctioned ADAMS Public Search API whether the record changed instead of requesting the PDF.

1. Register for a key at the NRC's ADAMS API page and keep it **out of the repository**: `export NRC_ADAMS_API_KEY=...` in the shell or service environment that runs the agent (or a git-ignored env file). The key is sent in the `Ocp-Apim-Subscription-Key` header and is never logged, printed, written to reports or stored in state.
2. With the key set, each ML link, and each `update_tracking_urls` entry that is an ML PDF, costs one API request (`GET /aps/api/search/{accession}`, one every 2 s). The fingerprint is `AccessionNumber | DateAddedTimestamp | DocumentDate | hash of content`. Same as last time: the document is "unchanged (ADAMS record ... unchanged)" and www.nrc.gov is not contacted. Different, or the API fails: the PDF is fetched as usual.
3. The API text is not used to build graphs (the PDF stays the source) and `DocumentType` is not trusted. Both response shapes (the record itself, or an envelope with `document`) are accepted; the undocumented `DocumentSize` field is ignored. Requests are counted in the summary (`adams_requests`).
4. `python3 src/kg_agent.py adamscheck ML18019A922` makes one request and prints a redacted summary (top-level keys, fingerprint fields, never the key or the text).

Without the variable, ADAMS is off and nothing changes.

## What was not verified

Everything above was tested against local mock servers (`tests/test_fetch_blocking.py`, `tests/test_adams.py`). It has **not** been run against www.nrc.gov, adams-api.nrc.gov, ascelibrary.org or webstore.ansi.org: whether an honest user agent is accepted by the NRC's CDN, how long a real block lasts, and the exact shape of live APS responses beyond the sample used to build the synthetic test fixture are open until you run it. The search body of `AdamsClient.search` follows the APS guide from memory and is untested against the live API; the agent itself uses only `get_document`. The dead energy.gov tracker `ehss/nuclear-and-facility-safety-directives` (404 in a browser too) was removed from entries 11 to 13 of `config/standards.json`; their other URLs were checked with `fetchcheck` from a developer machine and return 200.
