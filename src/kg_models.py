"""Pydantic models for the KG update agent (configuration views, state, findings,
audit records). The JSON configuration files stay the source of truth and are
edited as raw dictionaries (to preserve their layout); these models validate
them and give the agent typed views."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal["standard", "report"]

# Publishers that sell standards: a link on one of these hosts is a store/landing page,
# not the document itself.
PAYWALL_HOSTS = ("ascelibrary.org", "pubs.asce.org", "sp360.asce.org", "webstore.ansi.org",
                 "asme.org", "store.accuristech.com", "ans.org")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class Link(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str = ""
    url: str


class DocEntry(BaseModel):
    """Typed view of one standard or report from the JSON configuration."""

    model_config = ConfigDict(extra="ignore")
    kind: Kind
    id: int
    name: str
    links: list[Link] = Field(default_factory=list)          # documents to ingest
    tracking_urls: list[str] = Field(default_factory=list)   # pages to watch for updates
    format_of_data: str = ""
    complies_with: list[int] = Field(default_factory=list)   # reports -> standard ids
    paywalled: bool = False                                   # document must be bought -> needs a local copy
    local_copy: Optional[str] = None                          # file/dir with the purchased copy (optional)
    overlaps: list[int] = Field(default_factory=list)         # ids of standards whose text overlaps this one

    @field_validator("tracking_urls", mode="before")
    @classmethod
    def _drop_blank(cls, v: Any) -> Any:
        return [u for u in (v or []) if isinstance(u, str) and u.strip()]

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def from_raw(cls, kind: Kind, raw: dict[str, Any]) -> "DocEntry":
        if kind == "standard":
            links = raw.get("links", [])
            hosts = [u["url"].split("/")[2].lower() if "//" in u.get("url", "") else "" for u in links]
            inferred = "paywalled" in raw.get("format_of_data", "").lower() or (
                bool(hosts) and all(any(h == x or h.endswith("." + x) for x in PAYWALL_HOSTS) for h in hosts))
            return cls(kind=kind, id=raw["id"], name=raw["document"], links=links,
                       tracking_urls=raw.get("update_tracking_urls", []),
                       format_of_data=raw.get("format_of_data", ""),
                       paywalled=bool(raw.get("paywalled", inferred)), local_copy=raw.get("local_copy"),
                       overlaps=[o["id"] for o in raw.get("overlaps_with", []) if isinstance(o, dict) and "id" in o])
        return cls(kind=kind, id=raw["id"], name=raw["report"], links=raw.get("template_links", []),
                   tracking_urls=raw.get("update_tracking_urls", []),
                   format_of_data=raw.get("format_of_data", ""),
                   complies_with=raw.get("standards_to_comply_with_ids", []))


# --------------------------------------------------------------------------
# Fetching / fingerprints / persisted state
# --------------------------------------------------------------------------


class DocState(BaseModel):
    """What we know about one downloaded document (one per unique URL)."""

    url: str
    fetch_url: str = ""               # what was actually downloaded (e.g. eCFR XML API URL)
    content_kind: str = ""            # pdf | html | xml
    etag: str = ""
    last_modified: str = ""
    raw_sha: str = ""                 # hash of the downloaded bytes
    text_sha: str = ""                # hash of the normalised text (what decides "changed")
    first_seen: str = ""
    last_checked: str = ""
    last_changed: str = ""
    kg_built: str = ""
    kg_nodes: int = 0
    kg_edges: int = 0
    versions: int = 0                 # number of KG versions built so far
    consecutive_errors: int = 0
    last_error: str = ""
    identities: dict[str, Optional[str]] = Field(default_factory=dict)  # "ASCE 43" -> "19" (as stated in the text)
    ocr_pages: list[int] = Field(default_factory=list)                  # pages read by OCR (no text layer)
    last_forced: str = ""                                               # last forced (unconditional) re-read
    forced_runs: int = 0
    last_forced_result: str = ""                                        # created | updated | refreshed | error


class TrackerState(BaseModel):
    """State of one update-tracking URL. ``data`` is tracker specific."""

    url: str
    tracker: str = ""
    last_checked: str = ""
    last_changed: str = ""
    page_sha: str = ""
    etag: str = ""
    last_modified: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    consecutive_errors: int = 0
    last_error: str = ""
    last_forced: str = ""                      # last forced (unconditional) re-read
    forced_runs: int = 0


class Finding(BaseModel):
    """Something a tracker found out about a tracked document."""

    type: Literal["new_edition", "baseline", "version_bump", "new_rulemaking", "page_changed"]
    entry: str                        # "standard:22"
    identity: str = ""                # e.g. "RG 1.233"
    old: Optional[str] = None
    new: Optional[str] = None
    new_url: Optional[str] = None
    new_text: Optional[str] = None    # full identifier text of the new edition
    tracker_url: str = ""
    evidence: str = ""
    confidence: float = 0.0
    applied: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ConfigChange(BaseModel):
    """One audited edit to a configuration file."""

    ts: str = Field(default_factory=utcnow)
    run_id: str = ""
    file: str                         # standards | reports
    entry: str                        # "standard:22"
    path: str                         # e.g. "links[0].url"
    old: Any = None
    new: Any = None
    reason: str = ""
    source: str = ""                  # url that triggered it ("external" for manual edits)
    auto: bool = True


class DocAction(BaseModel):
    """What happened to one document during a run."""

    url: str
    entries: list[str]
    action: Literal["created", "updated", "unchanged", "refreshed", "error", "skipped"]   # refreshed = forced re-read, nothing changed
    detail: str = ""
    diff: dict[str, Any] = Field(default_factory=dict)
    archived: Optional[str] = None
    forced: bool = False                       # the run was a forced update
    purged: dict[str, Any] = Field(default_factory=dict)   # stale asset files removed by a forced re-read {files, bytes, dirs, names}
    shas: list[str] = Field(default_factory=list)   # [text sha before, after] (12 chars) for forced re-reads


class EntryAction(BaseModel):
    entry: str
    action: Literal["created", "rebuilt", "unchanged", "refreshed", "error"]
    detail: str = ""
    archived: Optional[str] = None


class RunNarrative(BaseModel):
    """Optional local-LLM narration of the (already structured) run results."""

    summary: str
    highlights: list[str] = Field(default_factory=list)


class Alert(BaseModel):
    """A condition a human should act on (persisted until it is resolved)."""

    kind: Literal["no_local_copy", "new_edition_for_purchase", "local_copy_outdated",
                  "local_copy_unidentified", "store_page_changed"]
    entry: str
    identity: str = ""
    severity: Literal["warning", "info"] = "warning"
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""
    runs: int = 1
    is_new: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.entry}|{self.identity}"


# --------------------------------------------------------------------------
# The ``version_tracking`` block the agent maintains inside every configuration entry
# --------------------------------------------------------------------------


class LatestVersion(BaseModel):
    """Newest edition known to exist (from trackers), per identifier."""

    identity: str                       # "RG 1.233", "10 CFR Part 50"
    edition: Optional[str] = None       # "1", "2026", "2026-09-01" (eCFR issue date); None if unknown
    label: str = ""                     # "RG 1.233 Rev. 1"
    url: Optional[str] = None           # where to get it
    detected: Optional[str] = None      # when the agent first saw it
    source: Optional[str] = None        # tracker page that showed it


class StagedDocument(BaseModel):
    url: str                            # download URL, or "local:<file name>" for a purchased copy
    kind: str = ""                      # pdf | html | xml
    role: Optional[str] = None          # "store page" for the landing page of a paywalled standard
    local: bool = False
    fetched: Optional[str] = None       # when this content was ingested
    text_sha: str = ""                  # first 12 hex of the content fingerprint
    as_of: Optional[str] = None         # eCFR: the date of the XML that was staged
    ocr_pages: Optional[list[int]] = None   # pages that had no text layer and were read by OCR
    editions: dict[str, Optional[str]] = Field(default_factory=dict)   # identifiers the text states about itself


class StagedEditions(BaseModel):
    edition: Optional[str] = None
    source: Literal["document", "configuration"] = "document"   # read from the text vs assumed from the configuration


class StagedVersion(BaseModel):
    kg: Optional[str] = None            # path of the entry KG relative to the agent home
    kg_built: Optional[str] = None
    documents: list[StagedDocument] = Field(default_factory=list)
    editions: dict[str, StagedEditions] = Field(default_factory=dict)


class VersionTracking(BaseModel):
    status: Literal["current", "update_available", "not_staged", "partially_staged", "no_local_copy", "no_documents"]
    latest: list[LatestVersion] = Field(default_factory=list)
    staged: StagedVersion = Field(default_factory=StagedVersion)
