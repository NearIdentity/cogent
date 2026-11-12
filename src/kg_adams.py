"""Small adapter for the NRC ADAMS Public Search (APS) API, used for CHANGE DETECTION only.

For an ``https://www.nrc.gov/docs/ML....pdf`` link the agent asks the API about the accession
number instead of downloading the PDF: if the record's date added, document date and a hash of its
text are the same as last time, the document did not change and nrc.gov is not contacted at all.
The API text is never turned into a knowledge graph here (the PDF stays the source of the graph),
and its ``DocumentType`` is not trusted.

* endpoint  ``GET  {base}/aps/api/search/{accessionNumber}``  (one record)
            ``POST {base}/aps/api/search``                    (search; body as in the APS guide)
* key       environment variable ``NRC_ADAMS_API_KEY``, sent as ``Ocp-Apim-Subscription-Key``. It is
            never logged, printed, written to reports or kept in any file by this module.
* pacing    one request every 2 s (``kg_hosts.DEFAULT_INTERVALS``); 429 / 403 / 503+Retry-After put the
            API host in the same persisted cooldown as any other host.

``python3 src/kg_agent.py adamscheck ML18019A922`` prints a redacted summary of a real response.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import kg_hosts as hosts

log = logging.getLogger("kg_agent.adams")

ADAMS_BASE = "https://adams-api.nrc.gov"
ADAMS_HOST = "adams-api.nrc.gov"
KEY_ENV = "NRC_ADAMS_API_KEY"
# two accession number forms exist: ML18019A922 (ML + YY DDD letter NNN) and ML070630003 (ML + 9 digits)
_ACCESSION = re.compile(r"\b(ML(?:\d{9}|\d{5}[A-Z]\d{3}))\b", re.I)
_DOCS_HOSTS = ("www.nrc.gov", "nrc.gov", "adamswebsearch2.nrc.gov")


def accession_from_url(url: str) -> Optional[str]:
    """The accession number of an ``nrc.gov/docs/ML....`` URL (upper-cased), else None."""
    u = urlparse(url)
    if u.netloc.lower() not in _DOCS_HOSTS or "/docs/" not in u.path.lower():
        return None
    m = _ACCESSION.search(u.path)
    return m.group(1).upper() if m else None


def fingerprint(payload: dict) -> tuple[str, dict[str, Any]]:
    """Change-detection fingerprint of an APS response and the (non-secret, non-text) fields it came from.

    Both response shapes are accepted: the record itself, or an envelope with the record under ``document``."""
    d = payload.get("document", payload) if isinstance(payload, dict) else {}
    if not isinstance(d, dict) or not d.get("AccessionNumber"):
        raise ValueError("response has no AccessionNumber")
    content = d.get("content") or ""
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    fields = {"AccessionNumber": d.get("AccessionNumber"), "DateAddedTimestamp": d.get("DateAddedTimestamp"),
              "DocumentDate": d.get("DocumentDate"), "content_sha": hashlib.sha256(str(content).encode("utf-8")).hexdigest()[:16],
              "content_chars": len(str(content))}
    fp = "|".join(str(fields[k] or "") for k in ("AccessionNumber", "DateAddedTimestamp", "DocumentDate", "content_sha"))
    return fp, fields


@dataclass
class AdamsResult:
    ok: bool = False
    status: int = 0
    accession: str = ""
    fp: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    deferred: bool = False
    raw_keys: list[str] = field(default_factory=list)
    content: str = field(default="", repr=False)            # the record's text (memory only: never printed, logged or stored)
    info: dict[str, Any] = field(default_factory=dict)       # title, pages, URL ... for display; DocumentType is shown but never trusted


class AdamsClient:
    def __init__(self, key: Optional[str] = None, base: Optional[str] = None, timeout: float = 30.0):
        self._key = key if key is not None else os.environ.get(KEY_ENV, "")
        self.base = (base or os.environ.get("NRC_ADAMS_BASE") or ADAMS_BASE).rstrip("/")
        self.host = hosts.host_of(self.base)
        self.timeout = timeout
        self.requests = 0

    @classmethod
    def from_env(cls) -> Optional["AdamsClient"]:
        """A client when ``NRC_ADAMS_API_KEY`` is set, else None (ADAMS change detection is then off)."""
        return cls() if os.environ.get(KEY_ENV) else None

    # -- plumbing
    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _call(self, path: str, body: Optional[dict] = None) -> tuple[int, Any, str]:
        """(status, parsed JSON | None, error). Never raises; never includes the key in the error text."""
        pol = hosts.HOSTS
        if pol.cooldown(self.host):
            pol.defer(self.host, self.base + path)
            return 0, None, hosts_deferred()
        pol.pace(self.host)
        self.requests += 1
        headers = {"Ocp-Apim-Subscription-Key": self._key, "Accept": "application/json", "User-Agent": hosts.user_agent()}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(self.base + path, data=data, headers=headers, method="POST" if body is not None else "GET"),
                         timeout=self.timeout) as r:
                pol.success(self.host)
                return r.status, json.loads(r.read().decode("utf-8")), ""
        except HTTPError as exc:
            hdrs = {k: v for k, v in (exc.headers.items() if exc.headers else []) if k.lower() in ("retry-after", "server", "via")}
            reason = hosts.block_reason(exc.code, hdrs)
            if reason:
                pol.block(self.host, reason, hosts.parse_retry_after(hdrs.get("Retry-After", "")), url=self.base + path, headers=hdrs)
                return exc.code, None, reason
            if exc.code == 401:
                return 401, None, "ADAMS API key rejected (HTTP 401); check NRC_ADAMS_API_KEY"
            return exc.code, None, f"HTTP {exc.code} {exc.reason}"
        except (URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
            return 0, None, self._redact(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")

    # -- API
    def get_document(self, accession: str) -> AdamsResult:
        """One record, reduced to its change-detection fingerprint."""
        acc = accession.strip().upper()
        if not _ACCESSION.fullmatch(acc):
            return AdamsResult(accession=acc, error=f"not an ADAMS accession number: {accession!r}")
        status, payload, err = self._call(f"/aps/api/search/{acc}")
        if err or payload is None:
            return AdamsResult(status=status, accession=acc, error=err or "empty response",
                               deferred=err.startswith("deferred"))
        try:
            fp, fields = fingerprint(payload)
        except ValueError as exc:
            return AdamsResult(status=status, accession=acc, error=str(exc))
        d = payload.get("document", payload)
        content = d.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(str(x) for x in content)
        info = {k: d.get(k) for k in ("DocumentTitle", "DocumentType", "DocumentDate", "DateAddedTimestamp", "EstimatedPageCount",
                                      "DocumentSize", "Url", "AuthorName", "DocketNumber") if d.get(k) not in (None, "")}
        return AdamsResult(True, status, acc, fp, fields, raw_keys=sorted(payload.keys()) if isinstance(payload, dict) else [],
                           content=str(content), info=info)

    def search(self, body: dict) -> tuple[list[dict], str]:
        """POST a search (``body`` as documented in the APS guide); returns (records, error)."""
        status, payload, err = self._call("/aps/api/search", body)
        if err or not isinstance(payload, dict):
            return [], err or "unexpected response"
        recs = payload.get("results") or payload.get("documents") or []
        return [r.get("document", r) if isinstance(r, dict) else {} for r in recs], ""


def hosts_deferred() -> str:
    return "deferred: host is blocking this client"


# ---------------------------------------------------------------------------
# Verifying a local copy against its ADAMS record
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

OVERLAP_MIN = 0.6              # share of the ADAMS text's 4-word phrases that must occur in the file's own text
DATE_SLACK_DAYS = 2            # a file may not have been written later than this after the record was added to ADAMS
_MIN_WORDS = 50                # below this a text comparison means nothing


def accession_in_name(name: str) -> Optional[str]:
    m = _ACCESSION.search(name)
    return m.group(1).upper() if m else None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _shingles(words: list[str], n: int = 4) -> set[int]:
    return {hash(tuple(words[i:i + n])) for i in range(0, max(0, len(words) - n + 1))}


def text_overlap(adams_text: str, file_text: str) -> Optional[float]:
    """Share of the ADAMS text's 4-word phrases that also occur in the file's text (None when either is too short to compare).
    Text extraction differs between ADAMS and a PDF reader, so this is a similarity, not an equality."""
    a, f = _words(adams_text)[:30000], _words(file_text)
    if len(a) < _MIN_WORDS or len(f) < _MIN_WORDS:
        return None
    sa = _shingles(a)
    return len(sa & _shingles(f)) / len(sa) if sa else None


def _pdf_date(value: str) -> Optional[_dt.datetime]:
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", value or "")
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    try:
        return _dt.datetime(y, mo or 1, d or 1, h, mi, se, tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def _iso(value: Any) -> Optional[_dt.datetime]:
    try:
        t = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def file_facts(path) -> dict[str, Any]:
    """Hash, size, times and (for PDFs) page count, metadata dates and text of a local copy."""
    from pathlib import Path as _P
    p = _P(path)
    data = p.read_bytes()
    st = p.stat()
    facts: dict[str, Any] = {"sha256": hashlib.sha256(data).hexdigest(), "size": st.st_size, "mtime": int(st.st_mtime), "text": "", "pdf": {}}
    if data[:5] == b"%PDF-":
        try:
            import pymupdf
            doc = pymupdf.open(stream=data, filetype="pdf")
            md = doc.metadata or {}
            facts["pdf"] = {"pages": doc.page_count, "created": md.get("creationDate") or "", "modified": md.get("modDate") or ""}
            facts["text"] = "\n".join(pg.get_text() for pg in doc)
        except Exception as exc:                     # a damaged PDF is itself worth a warning
            facts["pdf"] = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}
    else:
        try:
            from bs4 import BeautifulSoup
            facts["text"] = BeautifulSoup(data, "html.parser").get_text(" ")
        except Exception:
            pass
    return facts


def verify_local_copy(path, record: "AdamsResult", previous: Optional[dict] = None) -> dict[str, Any]:
    """Compare a local copy with its ADAMS record. Returns {ok, failures, notes, sha256, size, mtime, pdf, adams_fp, accession, checked}.

    Checks: the accession in the file name; the text (phrase overlap with ADAMS); the page count (ADAMS gives an estimate); the PDF's
    own dates against the date the record was added to ADAMS; and, against the previous verification of the same path, a changed
    content with an unchanged modification time. These are heuristics for catching a wrong or altered file; they are not proof."""
    from pathlib import Path as _P
    p = _P(path)
    facts = file_facts(p)
    failures: list[str] = []
    notes: list[str] = []
    name_acc = accession_in_name(p.name)
    if name_acc and name_acc != record.accession:
        failures.append(f"the file name says {name_acc} but the ADAMS record is {record.accession}")
    ov = text_overlap(record.content, facts["text"])
    if ov is None:
        notes.append("text not compared (no text layer, or too little text on either side)")
    elif ov < OVERLAP_MIN:
        failures.append(f"only {ov:.0%} of the ADAMS text occurs in the file (at least {OVERLAP_MIN:.0%} expected)")
    est, pages = record.info.get("EstimatedPageCount"), facts["pdf"].get("pages")
    try:
        est_n = int(est) if est is not None else None
    except (TypeError, ValueError):
        est_n = None
    if est_n and pages and abs(pages - est_n) > max(2, 0.2 * est_n):
        failures.append(f"the file has {pages} pages; ADAMS estimates {est_n}")
    added = _iso(record.info.get("DateAddedTimestamp"))
    for label in ("created", "modified"):
        t = _pdf_date(facts["pdf"].get(label, ""))
        if t and added and t > added + _dt.timedelta(days=DATE_SLACK_DAYS):
            failures.append(f"the PDF's {label} date ({t:%Y-%m-%d}) is after the record was added to ADAMS ({added:%Y-%m-%d})")
    if previous and previous.get("sha256") and previous["sha256"] != facts["sha256"] and previous.get("mtime") == facts["mtime"]:
        failures.append("the file content changed but its modification time did not")
    if facts["pdf"].get("error"):
        failures.append(f"the PDF could not be read ({facts['pdf']['error']})")
    facts.pop("text")
    return {**facts, "ok": not failures, "failures": failures, "notes": notes, "adams_fp": record.fp, "accession": record.accession,
            "checked": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
