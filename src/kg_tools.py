"""Reusable, hard-coded tools for the KG update agent.

Everything here is deterministic (no LLM, almost no free-text processing):

* ``fetch``            conditional HTTP GET (ETag / Last-Modified, retries, polite
                       per-host delay, headless-browser fallback for bot-blocked sites)
* ``fingerprint``      content hashes that ignore volatile page furniture
* identity families    regex parsers for RG / NEI / DOE-STD / DOE O / NUREG / ASCE /
                       QME / RA-S / CFR identifiers with comparable editions
* ``page_records``     table rows / list items / paragraphs of an HTML page, with links
* trackers             eCFR version history (JSON), Federal Register (JSON),
                       "identifier watcher" for index pages (RG lists, DOE standards
                       and directives libraries, NRC pages, ...)
* KG tools             merge graphs, identifier citations, CFR cross-references, KG diff
* ``json_diff``        audit-friendly diff of configuration files

Endpoint shapes for eCFR / Federal Register are written from the public API
documentation; ``kg_agent.py probe <url>`` runs a tracker once and prints what it
parsed, so they can be verified against the live services.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import networkx as nx
from bs4 import BeautifulSoup

from kg_models import DocEntry, Finding, TrackerState, utcnow

log = logging.getLogger("kg_agent.tools")

# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
BLOCKED = {401, 403, 406}          # statuses that usually mean "bot filter" -> try a browser
_last_hit: dict[str, float] = {}


@dataclass
class FetchResult:
    url: str
    status: int = 0                # 200, 304, other HTTP status, 0 = transport error
    data: bytes = b""
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    final_url: str = ""
    error: str = ""
    via: str = "http"

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def _polite(url: str, min_interval: float) -> None:
    host = urlparse(url).netloc
    wait = _last_hit.get(host, 0.0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def fetch(url: str, etag: str = "", last_modified: str = "", timeout: float = 45.0,
          retries: int = 2, backoff: float = 2.0, min_interval: float = 1.0,
          browser_fallback: bool = True, accept: str = "*/*") -> FetchResult:
    """GET ``url`` conditionally. 304 -> ``not_modified``. Transient errors are
    retried; bot-filter responses fall back to headless Chromium (see kg_ingest)."""
    headers = {"User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    res = FetchResult(url)
    for attempt in range(retries + 1):
        _polite(url, min_interval)
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as r:
                return FetchResult(url, 200, r.read(), r.headers.get("ETag", ""),
                                   r.headers.get("Last-Modified", ""),
                                   r.headers.get("Content-Type", ""), r.geturl())
        except HTTPError as exc:
            if exc.code == 304:
                return FetchResult(url, 304, etag=etag, last_modified=last_modified, final_url=url)
            res = FetchResult(url, exc.code, error=f"HTTP {exc.code} {exc.reason}")
            if exc.code in BLOCKED and browser_fallback:
                return _browser_fetch(url, res)
            if exc.code < 500 and exc.code != 429:
                return res
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            res = FetchResult(url, 0, error=f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    return res


def _browser_fetch(url: str, previous: FetchResult) -> FetchResult:
    try:
        import kg_ingest
        data = kg_ingest._fetch_browser(url, 60.0)
        return FetchResult(url, 200, data, final_url=url, via="browser")
    except Exception as exc:  # Playwright missing, site refuses browsers too, ...
        previous.error += f"; browser fallback failed: {str(exc)[:160]}"
        return previous


# --------------------------------------------------------------------------
# Content kinds and fingerprints
# --------------------------------------------------------------------------

VOLATILE_LINE = re.compile(
    r"(last\s+(?:reviewed|updated|modified)|page\s+last|updated\s+on|current\s+as\s+of|"
    r"\bcopyright\b|©|csrf|nonce|session\s*id)", re.I)


def sniff_kind(data: bytes, content_type: str = "", url: str = "") -> str:
    head = data[:2048].lstrip()
    ctype = content_type.lower()
    if head.startswith(b"%PDF") or "pdf" in ctype:
        return "pdf"
    if head.startswith(b"{") or head.startswith(b"[") or "json" in ctype:
        return "json"
    if (head.startswith(b"<?xml") or "xml" in ctype) and b"<html" not in head[:1024].lower():
        return "xml"
    return "html"


def html_text(data: bytes) -> str:
    """Visible text of an HTML page without scripts/navigation/volatile lines."""
    soup = BeautifulSoup(data, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer", "form", "iframe", "svg", "header"]):
        t.decompose()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (soup.get_text("\n") or "").splitlines()]
    return "\n".join(ln for ln in lines if ln and not VOLATILE_LINE.search(ln))


def _sha(b: bytes | str) -> str:
    return hashlib.sha256(b.encode("utf-8") if isinstance(b, str) else b).hexdigest()


def fingerprint(data: bytes, kind: str) -> tuple[str, str]:
    """(raw_sha, text_sha). ``text_sha`` decides whether a document really
    changed: PDFs are compared by extracted text (a re-rendered file with the
    same words is not an update), HTML by visible text without page furniture."""
    raw = _sha(data)
    if kind == "pdf":
        try:
            import pymupdf
            doc = pymupdf.open(stream=data, filetype="pdf")
            text = " ".join(" ".join(p.get_text("text").split()) for p in doc)
            if len(text) < 10 * max(len(doc), 1):
                # scanned: the text layer says nothing, so compare the page images (a re-saved file with
                # the same scans, new metadata or object ids is not an update); fall back to the bytes
                h = hashlib.sha256()
                for page in doc:
                    for img in page.get_images(full=True):
                        try:
                            h.update(doc.extract_image(img[0])["image"])
                        except Exception:
                            return raw, raw
                return raw, h.hexdigest() if h.digest() != hashlib.sha256().digest() else raw
            return raw, _sha(text)
        except Exception:
            return raw, raw
    if kind == "html":
        return raw, _sha(html_text(data))
    if kind == "xml":
        return raw, _sha(re.sub(rb"\s+", b" ", data))
    return raw, raw


# --------------------------------------------------------------------------
# Document identities (editions) -- hard-coded regex families
# --------------------------------------------------------------------------

_REV = re.compile(r"Rev(?:ision|\.)?\s*(\d+)", re.I)


def _int(ed: Optional[str]) -> int:
    try:
        return int(ed)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


def _year(ed: Optional[str]) -> int:
    """Year editions written with 2 or 4 digits ('19' == '2019')."""
    n = _int(ed)
    return n + 2000 if 0 <= n < 100 else n


def _like_year(obs: str, known: Optional[str]) -> str:
    """Spell ``obs`` in the style (2- or 4-digit year) of ``known``."""
    if known and obs and len(known) == 2 and len(obs) == 4:
        return obs[2:]
    if known and obs and len(known) == 4 and len(obs) == 2:
        return "20" + obs
    return obs


def _letter(ed: Optional[str]) -> int:
    return ord(ed.upper()) - 64 if ed and ed[:1].isalpha() else -1


@dataclass
class Family:
    name: str
    ident: re.Pattern                      # parse identifiers from config text: groups key[, ed, pre]
    search: Callable[[str], re.Pattern]    # key -> pattern locating the identifier in a page record
    rank: Callable[[Optional[str]], int]
    fmt: Callable[[str, Optional[str]], str]
    edition_from: Callable[[re.Match, str], Optional[str]] = lambda m, text: (
        m.groupdict().get("ed"))          # edition of a record match
    tracks_editions: bool = True
    like: Callable[[str, Optional[str]], str] = lambda obs, known: obs   # edition spelling


def _rev_after(m: re.Match, text: str) -> Optional[str]:
    rm = _REV.search(text, m.end())
    if rm and rm.start() - m.end() <= 60:
        return rm.group(1)
    rm = _REV.search(text)
    return rm.group(1) if rm else None


FAMILIES: dict[str, Family] = {f.name: f for f in [
    Family("RG",
           re.compile(r"(?:NRC\s+)?(?:RG|Regulatory\s+Guide)\s*(?P<key>\d\.\d{1,3})"
                      r"(?:\s*,?\s*\(?Rev(?:ision|\.)?\s*(?P<ed>\d+)\)?)?", re.I),
           lambda k: re.compile(rf"(?<![\d.]){re.escape(k)}(?![\d])"),
           _int, lambda k, e: f"RG {k}" + (f" Rev. {e}" if e is not None else ""), _rev_after),
    Family("NEI",
           re.compile(r"NEI\s*(?P<key>\d{2}-\d{2})(?:\s*,?\s*Rev(?:ision|\.)?\s*(?P<ed>\d+))?", re.I),
           lambda k: re.compile(rf"NEI\s*{re.escape(k)}", re.I),
           _int, lambda k, e: f"NEI {k}" + (f" Rev. {e}" if e is not None else ""), _rev_after),
    Family("DOE-STD",
           re.compile(r"(?P<pre>DOE|NE)-STD-(?P<key>\d{4})(?:-(?P<ed>\d{4}))?"),
           lambda k: re.compile(rf"(?:DOE|NE)-STD-{re.escape(k)}(?:-(?P<ed>\d{{4}}))?"),
           _int, lambda k, e: f"DOE-STD-{k}" + (f"-{e}" if e else "")),
    Family("DOE-O",
           re.compile(r"DOE\s*O\s*(?P<key>\d{3,4}\.\d{1,2})(?P<ed>[A-Z])?(?![A-Za-z0-9])"),
           lambda k: re.compile(rf"(?<![\d.]){re.escape(k)}(?P<ed>[A-Z])?(?![\w.])"),
           _letter, lambda k, e: f"DOE O {k}{e or ''}"),
    Family("ASCE",
           re.compile(r"ASCE(?:/SEI)?\s*(?P<key>\d+)-(?P<ed>\d{2,4})"),
           lambda k: re.compile(rf"ASCE(?:/SEI)?\s*{re.escape(k)}-(?P<ed>\d{{2,4}})"),
           _year, lambda k, e: f"ASCE/SEI {k}-{e}", like=_like_year),
    Family("QME",
           re.compile(r"QME-(?P<key>1)(?:-(?P<ed>\d{4}))?"),
           lambda k: re.compile(rf"QME-{re.escape(k)}(?:-(?P<ed>\d{{4}}))?"),
           _int, lambda k, e: f"QME-{k}" + (f"-{e}" if e else "")),
    Family("NQA",
           re.compile(r"NQA-(?P<key>1)[a-z]?(?:-(?P<ed>\d{4}))?"),
           lambda k: re.compile(rf"NQA-{re.escape(k)}[a-z]?(?:-(?P<ed>\d{{4}}))?"),
           _int, lambda k, e: f"NQA-{k}" + (f"-{e}" if e else "")),
    Family("RA-S",
           re.compile(r"RA-S-(?P<key>1\.4)-(?P<ed>\d{4})"),
           lambda k: re.compile(rf"RA-S-{re.escape(k)}-(?P<ed>\d{{4}})"),
           _int, lambda k, e: f"RA-S-{k}-{e}"),
    Family("NUREG",
           re.compile(r"NUREG-(?P<key>\d{4})(?:/CR-\d+)?(?:\s*,?\s*Rev(?:ision|\.)?\s*(?P<ed>\d+))?"),
           lambda k: re.compile(rf"NUREG-{re.escape(k)}"),
           _int, lambda k, e: f"NUREG-{k}" + (f" Rev. {e}" if e is not None else ""), _rev_after),
    Family("CFR",
           re.compile(r"10\s*CFR\s*(?:Part\s*|§\s*)?(?P<key>\d+(?:\.\d+)?)"),
           lambda k: re.compile(rf"10\s*CFR\s*(?:Part\s*|§\s*)?{re.escape(k)}"),
           _int, lambda k, e: f"10 CFR {k}", tracks_editions=False),
]}


@dataclass(frozen=True)
class Identity:
    family: str
    key: str
    edition: Optional[str] = None
    raw: str = ""

    @property
    def label(self) -> str:
        return f"{self.family} {self.key}"


def parse_identities(*texts: str) -> list[Identity]:
    """All identifiers in the given texts, one per (family, key); when several
    editions of the same document appear, the highest one is kept."""
    best: dict[tuple[str, str], Identity] = {}
    for text in texts:
        for fam in FAMILIES.values():
            for m in fam.ident.finditer(text or ""):
                ed = m.groupdict().get("ed")
                ident = Identity(fam.name, m.group("key"), ed, m.group(0))
                cur = best.get((fam.name, ident.key))
                if cur is None or fam.rank(ed) > fam.rank(cur.edition):
                    best[(fam.name, ident.key)] = ident
    return list(best.values())


def entry_identities(entry: DocEntry) -> list[Identity]:
    return parse_identities(entry.name, *[l.text for l in entry.links])


def retarget_text(text: str, old: Identity, new_raw: str) -> str:
    """Rewrite identifier ``old`` inside ``text`` to the edition spelled by ``new_raw``
    (e.g. 'DOE-STD-1271-2025' -> 'NE-STD-1271-2026'). Only parts present in both are
    replaced (edition, DOE/NE prefix)."""
    fam = FAMILIES[old.family]
    new_m = fam.ident.search(new_raw)
    if not new_m:
        return text
    out = text
    for m in reversed(list(fam.ident.finditer(text))):
        if m.group("key") != old.key:
            continue
        spans = []
        for g in ("ed", "pre"):
            if g in fam.ident.groupindex and m.group(g) is not None and new_m.group(g) is not None:
                spans.append((m.start(g), m.end(g), new_m.group(g)))
        for a, b, rep in sorted(spans, reverse=True):
            out = out[:a] + rep + out[b:]
    return out


# --------------------------------------------------------------------------
# HTML page records
# --------------------------------------------------------------------------


@dataclass
class Record:
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute href)


def page_records(data: bytes, base_url: str) -> list[Record]:
    """Table rows, list items, paragraphs and headings of a page with their links."""
    soup = BeautifulSoup(data, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    out, seen = [], set()
    for node in soup.find_all(["tr", "li", "p", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "a"]):
        if node.name == "a" and node.find_parent(["tr", "li", "p", "dt", "dd"]):
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ")).strip()
        if not text or len(text) > 1200:
            continue
        links = [(re.sub(r"\s+", " ", a.get_text(" ")).strip(), urljoin(base_url, a["href"]))
                 for a in ([node] if node.name == "a" else node.find_all("a", href=True))
                 if a.get("href") and not a["href"].startswith(("#", "mailto:", "javascript:"))]
        sig = (text, links[0][1] if links else "")
        if sig not in seen:
            seen.add(sig)
            out.append(Record(text, links))
    return out


_GOOD_LINK = re.compile(r"\.pdf($|\?)|/docs/ML|DownloadDocument|/documents/|/directives-documents/|standards-documents", re.I)


def pick_link(rec: Record, key: str) -> Optional[str]:
    for _, href in rec.links:
        if _GOOD_LINK.search(href):
            return href
    for text, href in rec.links:
        if key in text:
            return href
    return rec.links[0][1] if rec.links else None


# --------------------------------------------------------------------------
# Trackers
# --------------------------------------------------------------------------

ECFR_HOSTS = ("ecfr.gov",)
FR_HOSTS = ("federalregister.gov",)


@dataclass
class TrackerOutcome:
    state: TrackerState
    changed: bool = False
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    error: str = ""


@dataclass
class TrackerContext:
    min_interval: float = 1.0
    browser_fallback: bool = True
    ident_state: dict[str, dict[str, dict]] = field(default_factory=dict)  # entry key -> label -> {edition,...}


def _host_in(url: str, hosts: tuple[str, ...]) -> bool:
    h = urlparse(url).netloc.lower().split(":")[0]
    return any(h == x or h.endswith("." + x) for x in hosts)


def _cfr_parts(entry: DocEntry) -> set[str]:
    """CFR part numbers an entry relates to (from '10 CFR 50.69', 'Part 53', ...)."""
    parts = set()
    for ident in entry_identities(entry):
        if ident.family == "CFR":
            parts.add(ident.key.split(".")[0])
    for m in re.finditer(r"\bPart\s+(\d{2,3})\b", entry.name):
        parts.add(m.group(1))
    return parts


def _cfr_sections(entry: DocEntry) -> set[str]:
    return {i.key for i in entry_identities(entry) if i.family == "CFR" and "." in i.key}


class Tracker:
    name = "base"

    def matches(self, url: str) -> bool:
        raise NotImplementedError

    def check(self, url: str, prev: Optional[TrackerState], watchers: list[DocEntry],
              ctx: TrackerContext) -> TrackerOutcome:
        raise NotImplementedError

    def _new_state(self, url: str, prev: Optional[TrackerState]) -> TrackerState:
        st = prev.model_copy(deep=True) if prev else TrackerState(url=url, tracker=self.name)
        st.tracker, st.last_checked = self.name, utcnow()
        return st

    def _error(self, url: str, prev: Optional[TrackerState], msg: str) -> TrackerOutcome:
        st = self._new_state(url, prev)
        st.consecutive_errors += 1
        st.last_error = msg
        return TrackerOutcome(st, error=msg)


class EcfrVersionsTracker(Tracker):
    """``/api/versioner/v1/versions/title-N.json?part=P``: structured amendment
    history. Reports which sections were amended since the previous run."""

    name = "ecfr-versions"
    _path = re.compile(r"/api/versioner/v1/versions/title-(\d+)\.json")

    def matches(self, url: str) -> bool:
        return _host_in(url, ECFR_HOSTS) and bool(self._path.search(urlparse(url).path))

    def check(self, url, prev, watchers, ctx):
        r = fetch(url, min_interval=ctx.min_interval, browser_fallback=False, accept="application/json")
        if not r.ok:
            return self._error(url, prev, r.error or f"HTTP {r.status}")
        try:
            versions = json.loads(r.data).get("content_versions", [])
            dates = [v.get("issue_date") or v.get("amendment_date") or v.get("date") for v in versions]
            latest = max((d for d in dates if d), default="")
        except (ValueError, AttributeError) as exc:
            return self._error(url, prev, f"unparseable eCFR JSON: {exc}")
        st = self._new_state(url, prev)
        st.consecutive_errors, st.last_error = 0, ""
        prev_latest = (prev.data.get("latest_issue_date") if prev else "") or ""
        st.data = {"latest_issue_date": latest, "n_versions": len(versions)}
        part = parse_qs(urlparse(url).query).get("part", [""])[0]
        out = TrackerOutcome(st)
        if prev and latest > prev_latest:
            changed = sorted({v.get("identifier", "") for v in versions
                              if (v.get("issue_date") or v.get("amendment_date") or "") > prev_latest
                              and v.get("identifier")})
            st.last_changed = utcnow()
            out.changed = True
            out.summary = f"eCFR title {self._path.search(url).group(1)} part {part}: new version {latest} (was {prev_latest}); {len(changed)} section(s) amended"
            for e in watchers:
                sections = _cfr_sections(e)
                relevant = sorted(s for s in changed if any(s == x or s.startswith(x + "(") for x in sections))
                out.findings.append(Finding(
                    type="version_bump", entry=e.key, identity=f"10 CFR Part {part}",
                    old=prev_latest, new=latest, tracker_url=url, confidence=1.0,
                    evidence=out.summary,
                    details={"changed_sections": changed[:50], "relevant_sections": relevant,
                             "affects_entry": (not sections) or bool(relevant)}))
        return out


class FederalRegisterTracker(Tracker):
    """``federalregister.gov/agencies/<slug>``: new rules/proposed rules via the
    public JSON API, matched to entries by CFR part."""

    name = "federal-register"
    _path = re.compile(r"^/agencies/([\w-]+)/?$")

    def matches(self, url: str) -> bool:
        return _host_in(url, FR_HOSTS) and bool(self._path.match(urlparse(url).path))

    def check(self, url, prev, watchers, ctx):
        u = urlparse(url)
        slug = self._path.match(u.path).group(1)
        q = [("conditions[agencies][]", slug), ("conditions[type][]", "RULE"),
             ("conditions[type][]", "PRORULE"), ("order", "newest"), ("per_page", "20")]
        q += [("fields[]", f) for f in ("document_number", "title", "type", "publication_date",
                                        "html_url", "cfr_references", "effective_on")]
        api = f"{u.scheme}://{u.netloc}/api/v1/documents.json?{urlencode(q)}"
        r = fetch(api, min_interval=ctx.min_interval, browser_fallback=False, accept="application/json")
        if not r.ok:
            return self._error(url, prev, r.error or f"HTTP {r.status}")
        try:
            docs = json.loads(r.data).get("results", [])
        except (ValueError, AttributeError) as exc:
            return self._error(url, prev, f"unparseable Federal Register JSON: {exc}")
        st = self._new_state(url, prev)
        st.consecutive_errors, st.last_error = 0, ""
        seen = set(prev.data.get("seen", [])) if prev else set()
        st.data = {"seen": sorted({d.get("document_number", "") for d in docs} | seen)[-300:]}
        out = TrackerOutcome(st)
        if not prev:
            return out
        new = [d for d in docs if d.get("document_number") not in seen]
        if new:
            st.last_changed = utcnow()
            out.changed = True
            out.summary = f"Federal Register ({slug}): {len(new)} new rule document(s)"
            for d in new:
                refs = {str(c.get("part")) for c in (d.get("cfr_references") or []) if isinstance(c, dict)}
                for e in watchers:
                    hit = _cfr_parts(e) & refs
                    if hit:
                        out.findings.append(Finding(
                            type="new_rulemaking", entry=e.key, identity="10 CFR Part " + sorted(hit)[0],
                            new=d.get("document_number"), new_url=d.get("html_url"), tracker_url=url,
                            confidence=1.0, evidence=f"{d.get('type')}: {d.get('title', '')[:160]} ({d.get('publication_date')})",
                            details={"effective_on": d.get("effective_on")}))
        return out


class PageIdentityTracker(Tracker):
    """Fallback for any page: watch the identifiers of the entries that point at it.

    Looks for the entries' document identifiers (RG numbers, NEI numbers, DOE
    standards/orders, ...) in the page's table rows / list items and compares the
    edition shown with the one recorded for the entry. A higher edition becomes a
    ``new_edition`` finding with the link found in the same row."""

    name = "page-identity"

    def matches(self, url: str) -> bool:
        return True

    def check(self, url, prev, watchers, ctx):
        r = fetch(url, prev.etag if prev else "", prev.last_modified if prev else "",
                  min_interval=ctx.min_interval, browser_fallback=ctx.browser_fallback)
        if r.not_modified:
            st = self._new_state(url, prev)
            st.consecutive_errors, st.last_error = 0, ""
            return TrackerOutcome(st)
        if not r.ok:
            return self._error(url, prev, r.error or f"HTTP {r.status}")
        st = self._new_state(url, prev)
        st.consecutive_errors, st.last_error = 0, ""
        st.etag, st.last_modified = r.etag, r.last_modified
        kind = sniff_kind(r.data, r.content_type, url)
        if kind != "html":
            sha = _sha(r.data)
            out = TrackerOutcome(st, changed=bool(prev and prev.page_sha and prev.page_sha != sha))
            st.page_sha = sha
            if out.changed:
                st.last_changed = utcnow()
                out.summary = f"{kind.upper()} changed at {url}"
            return out

        records = page_records(r.data, r.final_url or url)
        idents = {e.key: [i for i in entry_identities(e) if FAMILIES[i.family].tracks_editions]
                  for e in watchers}
        all_keys = {(i.family, i.key) for v in idents.values() for i in v}
        # records mentioning any watched identifier -> fingerprints for change detection
        relevant = {}
        for rec in records:
            if any(FAMILIES[f].search(k).search(rec.text) for f, k in all_keys):
                relevant[_sha(rec.text)[:16]] = rec
        prev_hashes = set(prev.data.get("relevant", [])) if prev else set()
        st.page_sha = _sha("\n".join(rec.text for rec in records))
        st.data = {"relevant": sorted(relevant)}
        out = TrackerOutcome(st)

        for e in watchers:
            for ident in idents[e.key]:
                fam = FAMILIES[ident.family]
                cands = []
                for rec in records:
                    m = fam.search(ident.key).search(rec.text)
                    if m:
                        cands.append((fam.edition_from(m, rec.text), rec, m))
                if not cands:
                    continue
                known = ctx.ident_state.get(e.key, {}).get(ident.label, {}).get("edition", ident.edition)
                best = max(cands, key=lambda c: (fam.rank(c[0]), bool(c[1].links)))
                obs, rec, m = best
                if obs is None:
                    continue
                obs = fam.like(obs, known)
                if known is None:
                    out.findings.append(Finding(
                        type="baseline", entry=e.key, identity=ident.label, new=obs, tracker_url=url,
                        evidence=rec.text[:200], confidence=1.0, applied=True))
                elif fam.rank(obs) > fam.rank(known):
                    link = pick_link(rec, ident.key)
                    new_text = fam.fmt(ident.key, obs) if ident.family in ("RG", "NEI", "NUREG", "DOE-O", "ASCE") else m.group(0)
                    out.findings.append(Finding(
                        type="new_edition", entry=e.key, identity=ident.label, old=known, new=obs,
                        new_url=link, new_text=new_text, tracker_url=url,
                        evidence=rec.text[:200], confidence=0.9 if link else 0.5))

        new_relevant = [rec for h, rec in relevant.items() if h not in prev_hashes] if prev else []
        found_for = {f.entry for f in out.findings if f.type == "new_edition"}
        if prev and (new_relevant or st.page_sha != prev.page_sha):
            st.last_changed = utcnow() if (new_relevant or out.findings) else st.last_changed
            out.changed = bool(new_relevant or out.findings)
            if new_relevant:
                out.summary = f"{len(new_relevant)} new/changed record(s) mentioning tracked documents"
                for e in watchers:
                    if e.key in found_for:
                        continue
                    mine = [rec for rec in new_relevant
                            if any(FAMILIES[i.family].search(i.key).search(rec.text) for i in idents[e.key])]
                    if mine:
                        out.findings.append(Finding(
                            type="page_changed", entry=e.key, tracker_url=url, confidence=0.3,
                            evidence="; ".join(rec.text[:120] for rec in mine[:3]),
                            details={"records": len(mine)}))
        return out


TRACKERS: list[Tracker] = [EcfrVersionsTracker(), FederalRegisterTracker(), PageIdentityTracker()]


def tracker_for(url: str) -> Tracker:
    return next(t for t in TRACKERS if t.matches(url))


# --------------------------------------------------------------------------
# eCFR content API helpers
# --------------------------------------------------------------------------

_ECFR_PAGE = re.compile(r"^/current/title-(?P<title>\d+)/(?:chapter-[\w-]+/)?(?P<kind>part|section)-(?P<id>[\w.]+)/?$")


def ecfr_api_url(url: str, date: str) -> Optional[str]:
    """Human eCFR URL (``.../current/title-10/chapter-I/part-50`` or ``.../section-50.69``)
    -> versioner XML API URL for ``date`` (None if ``url`` isn't of that form)."""
    u = urlparse(url)
    m = _ECFR_PAGE.match(u.path)
    if not (m and _host_in(url, ECFR_HOSTS)):
        return None
    q = {"part": m["id"]} if m["kind"] == "part" else {"part": m["id"].split(".")[0], "section": m["id"]}
    return f"{u.scheme}://{u.netloc}/api/versioner/v1/full/{date}/title-{m['title']}.xml?{urlencode(q)}"


def ecfr_latest_date(url: str, ctx: TrackerContext) -> Optional[str]:
    """Latest issue date of the title ``url`` belongs to, via ``titles.json``."""
    u = urlparse(url)
    m = _ECFR_PAGE.match(u.path)
    if not m:
        return None
    r = fetch(f"{u.scheme}://{u.netloc}/api/versioner/v1/titles.json", min_interval=ctx.min_interval,
              browser_fallback=False, accept="application/json")
    if not r.ok:
        return None
    try:
        for t in json.loads(r.data).get("titles", []):
            if str(t.get("number")) == m["title"]:
                return t.get("up_to_date_as_of") or t.get("latest_issue_date")
    except (ValueError, AttributeError):
        pass
    return None


# --------------------------------------------------------------------------
# KG tools
# --------------------------------------------------------------------------


def merge_graphs(parts: list[tuple[str, nx.MultiDiGraph]], entry: DocEntry,
                 standard_names: Optional[dict[int, str]] = None) -> nx.MultiDiGraph:
    """Combine per-document KGs into the KG of one standard/report.

    Structural nodes get a per-document prefix (``<tag>:p0001``); entity nodes
    (``ent:...``) are shared across documents, so the same concept links them.
    Adds an entry node, ``HAS_DOCUMENT`` edges and, for reports,
    ``complies_with`` edges to the standards they must comply with."""
    g = nx.MultiDiGraph(entry=entry.key, built=utcnow(), sources=[])
    eid = f"entry:{entry.key}"
    g.add_node(eid, type=entry.kind.capitalize(), label=entry.name)

    def rename(tag: str, n: str) -> str:
        return n if n.startswith("ent:") else f"{tag}:{n}"

    for tag, src in parts:
        for n, d in src.nodes(data=True):
            nn = rename(tag, n)
            if nn in g:
                g.nodes[nn].update({k: v for k, v in d.items() if k not in ("type",)})
            else:
                g.add_node(nn, **d, **({"doc": tag} if not n.startswith("ent:") else {}))
        for u, v, k, d in src.edges(keys=True, data=True):
            d = dict(d)
            if "source" in d:
                d["source"] = rename(tag, d["source"])
            key = k
            if "|" in k:
                rel, elem = k.split("|", 1)
                key = f"{rel}|{rename(tag, elem)}"
            nu, nv = rename(tag, u), rename(tag, v)
            if not g.has_edge(nu, nv, key):
                g.add_edge(nu, nv, key=key, **d)
        for n, d in src.nodes(data=True):
            if d.get("type") == "Document":
                g.add_edge(eid, rename(tag, n), key="HAS_DOCUMENT", relation="HAS_DOCUMENT")
        g.graph["sources"].append({"tag": tag, "source": src.graph.get("source", ""),
                                   "text_sha": src.graph.get("text_sha", "")})
    for rel, ids in (("complies_with", entry.complies_with), ("overlaps_with", entry.overlaps)):
        for sid in ids:
            sn = f"entry:standard:{sid}"
            if sn not in g:
                g.add_node(sn, type="Standard", label=(standard_names or {}).get(sid, f"standard {sid}"))
            g.add_edge(eid, sn, key=rel, relation=rel)
    return g


def _text_of(d: dict) -> str:
    return " ".join(str(d.get(k, "")) for k in ("label", "text") if d.get(k))


def add_identifier_citations(g: nx.MultiDiGraph) -> int:
    """Deterministic cross-document links: every RG/NEI/DOE/NUREG/CFR identifier
    mentioned in a paragraph, heading, table or figure caption becomes an
    ``ident:`` node with a ``CITES`` edge from that element."""
    n_added = 0
    for n, d in list(g.nodes(data=True)):
        if d.get("type") not in ("Paragraph", "Heading", "Table", "Figure"):
            continue
        for ident in parse_identities(_text_of(d), d.get("rows", "") if d.get("type") == "Table" else ""):
            tgt = f"ident:{ident.label}"
            if tgt not in g:
                g.add_node(tgt, type="Identifier", label=FAMILIES[ident.family].fmt(ident.key, None))
            if not g.has_edge(n, tgt, "CITES"):
                g.add_edge(n, tgt, key="CITES", relation="CITES")
                n_added += 1
    return n_added


_SEC_HEAD = re.compile(r"^\s*§+\s*(\d+\.\d+)")
_SEC_REF = re.compile(r"§§?\s*(\d+\.\d+)(?:\([\w]+\))*"
                      r"((?:\s*(?:,|and|or|through)\s+(?:§\s*)?\d+\.\d+(?:\([\w]+\))*)*)")


def add_cfr_crossrefs(g: nx.MultiDiGraph) -> int:
    """For CFR text: ``§ 50.69`` headings become addressable sections and every
    ``§ 50.2``-style reference inside a section becomes a ``references`` edge
    between sections (or to a ``10 CFR n.n`` identifier node if the target section
    isn't in the same document). Pure regex, no LLM."""
    sec_of_head = {}
    for n, d in g.nodes(data=True):
        if d.get("type") == "Heading":
            m = _SEC_HEAD.match(d.get("text", "") or d.get("label", ""))
            if m:
                sec_of_head[n] = m.group(1)
    by_doc: dict[tuple[str, str], str] = {(g.nodes[n].get("doc", ""), s): n for n, s in sec_of_head.items()}
    parents = {}
    for u, v, k in g.edges(keys=True):
        if k == "CONTAINS":
            parents[v] = u
    added = 0
    for n, d in list(g.nodes(data=True)):
        if d.get("type") != "Paragraph":
            continue
        cur, src = parents.get(n), None
        while cur and src is None:               # climb to the enclosing § heading
            src = cur if cur in sec_of_head else None
            cur = parents.get(cur)
        if src is None:
            continue
        doc = d.get("doc", "")
        for m in _SEC_REF.finditer(d.get("text", "")):
            nums = [m.group(1)] + re.findall(r"\d+\.\d+", m.group(2) or "")
            for num in nums:
                if num == sec_of_head[src]:
                    continue
                tgt = by_doc.get((doc, num))
                if tgt is None:
                    tgt = f"ident:CFR {num}"
                    if tgt not in g:
                        g.add_node(tgt, type="Identifier", label=f"10 CFR {num}")
                key = f"references|{tgt}"
                if g.has_edge(src, tgt, key):
                    g.edges[src, tgt, key]["count"] += 1
                else:
                    g.add_edge(src, tgt, key=key, relation="references", count=1, source=n)
                    added += 1
    return added


def _section_hashes(g: nx.MultiDiGraph) -> dict[str, str]:
    children: dict[str, list[str]] = {}
    for u, v, k in g.edges(keys=True):
        if k == "CONTAINS":
            children.setdefault(u, []).append(v)
    out: dict[str, str] = {}
    for n, d in g.nodes(data=True):
        if d.get("type") != "Heading":
            continue
        parts = []
        for c in children.get(n, []):
            cd = g.nodes[c]
            if cd.get("type") in ("Paragraph", "Table", "Figure"):
                parts.append(str(cd.get("rows") or cd.get("text") or ""))
        title = d.get("label", n)
        while title in out:
            title += " #2"
        out[title] = _sha("\n".join(parts))[:12]
    return out


def kg_diff(old: Optional[nx.MultiDiGraph], new: nx.MultiDiGraph, limit: int = 15) -> dict[str, Any]:
    """Structured difference between two versions of a document KG."""
    out: dict[str, Any] = {"nodes": [old.number_of_nodes() if old else 0, new.number_of_nodes()],
                           "edges": [old.number_of_edges() if old else 0, new.number_of_edges()]}
    if old is None:
        return out
    a, b = _section_hashes(old), _section_hashes(new)
    out["sections_added"] = sorted(set(b) - set(a))[:limit]
    out["sections_removed"] = sorted(set(a) - set(b))[:limit]
    out["sections_modified"] = sorted(t for t in set(a) & set(b) if a[t] != b[t])[:limit]
    ea = {d.get("label") for n, d in old.nodes(data=True) if n.startswith("ent:")}
    eb = {d.get("label") for n, d in new.nodes(data=True) if n.startswith("ent:")}
    out["entities_added"], out["entities_removed"] = len(eb - ea), len(ea - eb)
    out["entities_added_sample"] = sorted(eb - ea)[:limit]
    return out


# --------------------------------------------------------------------------
# JSON diff (configuration audit)
# --------------------------------------------------------------------------


def json_diff(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
    """Leaf-level differences between two JSON values. Lists of objects with an
    ``id`` are matched by id (``standards[id=22].links[0].url``)."""
    diffs: list[dict[str, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a:
                diffs.append({"path": p, "old": None, "new": b[k]})
            elif k not in b:
                diffs.append({"path": p, "old": a[k], "new": None})
            else:
                diffs += json_diff(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list):
        if all(isinstance(x, dict) and "id" in x for x in a + b) and (a or b):
            ia, ib = {x["id"]: x for x in a}, {x["id"]: x for x in b}
            for i in sorted(set(ia) | set(ib), key=str):
                p = f"{path}[id={i}]"
                if i not in ia:
                    diffs.append({"path": p, "old": None, "new": ib[i]})
                elif i not in ib:
                    diffs.append({"path": p, "old": ia[i], "new": None})
                else:
                    diffs += json_diff(ia[i], ib[i], p)
        else:
            for i in range(max(len(a), len(b))):
                p = f"{path}[{i}]"
                if i >= len(a):
                    diffs.append({"path": p, "old": None, "new": b[i]})
                elif i >= len(b):
                    diffs.append({"path": p, "old": a[i], "new": None})
                else:
                    diffs += json_diff(a[i], b[i], p)
    elif a != b:
        diffs.append({"path": path, "old": a, "new": b})
    return diffs
