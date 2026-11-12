"""Per-host politeness for every HTTP request the agent makes.

* an honest user agent (``cogent-kg-agent/<version>``, override with ``$KG_USER_AGENT``);
  the agent never pretends to be a browser;
* pacing: a minimum interval between requests to the same host, with jitter, and a
  conservative built-in default for hosts known to block fast clients;
* cooldowns: the first 403, a 429, or a 503 with ``Retry-After`` puts the host in a
  cooldown. Nothing is requested from that host until it ends (hours, doubling while
  blocks persist, capped), and the cooldown is stored on disk so the next run honours it
  too. A host blocked during a run stays blocked for the rest of that run;
* ``interleave``: orders work so consecutive requests go to different hosts, so the wait
  imposed on one host is used to talk to the others.

No retries and no polling happen inside a cooldown.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Hashable, Iterable, Optional, TypeVar
from urllib.parse import urlparse

log = logging.getLogger("kg_agent.hosts")

VERSION = "1.0"
DEFAULT_INTERVALS: dict[str, float] = {"www.nrc.gov": 4.0, "adams-api.nrc.gov": 2.0}
BASE_COOLDOWN_H = 6.0
MAX_COOLDOWN_H = 168.0
JITTER = 0.25                      # +/- fraction of the interval

T = TypeVar("T")


def user_agent() -> str:
    """The user agent for every request: ``$KG_USER_AGENT`` or an honest identifying string."""
    return os.environ.get("KG_USER_AGENT") or f"cogent-kg-agent/{VERSION}"


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def parse_retry_after(value: str, now: Optional[float] = None) -> Optional[float]:
    """Seconds from a ``Retry-After`` header (delta-seconds or an HTTP date); None if absent / unreadable."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, dt.timestamp() - (time.time() if now is None else now))
    except (TypeError, ValueError):
        return None


def block_reason(status: int, headers: Optional[dict] = None) -> str:
    """Why an HTTP status puts the host in a cooldown ('' = it does not)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if status == 403:
        return "blocked by a bot filter (HTTP 403 from host)"
    if status == 429:
        return "rate limited (HTTP 429)"
    if status == 503 and h.get("retry-after"):
        return "service unavailable with Retry-After (HTTP 503)"
    return ""


class HostPolicy:
    def __init__(self, path: Optional[Path] = None, intervals: Optional[dict[str, float]] = None,
                 default_interval: float = 1.0, jitter: float = JITTER, persist: bool = True,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.path = Path(path) if path else None
        self.intervals = {**DEFAULT_INTERVALS, **{k.lower(): float(v) for k, v in (intervals or {}).items()}}
        self.default_interval, self.jitter, self.persist = default_interval, jitter, persist
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._run_blocked: dict[str, str] = {}                  # blocked during THIS run (stays blocked until it ends)
        self.deferred: dict[str, list[str]] = defaultdict(list)  # host -> urls skipped this run
        self.problems: dict[str, dict] = {}                     # host -> what went wrong this run (for the report)
        self.requests: dict[str, int] = defaultdict(int)
        self.state: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self.state = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                log.warning("could not read %s; starting with no cooldowns", self.path)

    # ---- pacing
    def interval(self, host: str, fallback: Optional[float] = None) -> float:
        host = host.lower()
        if host in self.intervals:
            return self.intervals[host]
        return self.default_interval if fallback is None else fallback

    def pace(self, host: str, fallback: Optional[float] = None) -> None:
        """Wait until the host's interval (with jitter) has passed since our last request to it."""
        iv = self.interval(host, fallback)
        if iv > 0:
            iv *= 1 + random.uniform(-self.jitter, self.jitter)
        with self._lock:
            wait = self._last.get(host, 0.0) + iv - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last[host] = self.clock()
            self.requests[host] += 1

    # ---- cooldowns
    def cooldown(self, host: str) -> Optional[dict]:
        """The active cooldown of ``host`` ({until, reason, level, ...}) or None."""
        host = host.lower()
        if host in self._run_blocked:
            rec = self.state.get(host) or {}
            return {**rec, "reason": self._run_blocked[host], "until": rec.get("until", self.clock())}
        rec = self.state.get(host)
        if rec and rec.get("until", 0) > self.clock():
            return rec
        return None

    def block(self, host: str, reason: str, retry_after: Optional[float] = None, url: str = "",
              headers: Optional[dict] = None) -> dict:
        host = host.lower()
        prev = self.state.get(host) or {}
        level = int(prev.get("level", 0)) + 1 if prev else 1
        hours = min(MAX_COOLDOWN_H, BASE_COOLDOWN_H * 2 ** (level - 1))
        seconds = max(hours * 3600, retry_after or 0)
        rec = {"until": self.clock() + seconds, "level": level, "reason": reason,
               "since": datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "url": url.split("?")[0][:200], "blocks": int(prev.get("blocks", 0)) + 1}
        if headers:
            rec["headers"] = {k: str(v)[:200] for k, v in headers.items()}
        self.state[host] = rec
        self._run_blocked[host] = reason
        self.problems.setdefault(host, {})["blocked"] = reason
        log.warning("%s: %s; no requests to this host for %.0f h (cooldown level %d)", host, reason, seconds / 3600, level)
        self._save()
        return rec

    def success(self, host: str) -> None:
        """A good response: a host whose cooldown has ended starts again from level 0."""
        host = host.lower()
        if host in self.state and host not in self._run_blocked:
            del self.state[host]
            self._save()

    def defer(self, host: str, url: str) -> None:
        self.deferred[host.lower()].append(url)
        self.problems.setdefault(host.lower(), {})["blocked"] = self._run_blocked.get(host.lower()) or (self.state.get(host.lower()) or {}).get("reason", "cooldown")

    def note(self, host: str, key: str, text: str) -> None:
        """Remember a non-blocking fetch problem (404 on a tracker, ...) for the per-host summary."""
        self.problems.setdefault(host.lower(), {}).setdefault(key, []).append(text)

    def _save(self) -> None:
        if not (self.path and self.persist):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True), "utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not write %s: %s", self.path, exc)

    def report(self) -> dict[str, dict]:
        """Per-host summary of this run's fetch problems (empty when nothing went wrong)."""
        out: dict[str, dict] = {}
        for host, pr in self.problems.items():
            rec = self.state.get(host) or {}
            out[host] = {**pr, "deferred": len(self.deferred.get(host, [])), "requests": self.requests.get(host, 0),
                         "cooldown_until": datetime.fromtimestamp(rec["until"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if rec.get("until") else ""}
        return out


HOSTS = HostPolicy(persist=False)


def configure(path: Optional[Path] = None, intervals: Optional[dict[str, float]] = None, default_interval: float = 1.0,
              persist: bool = True) -> HostPolicy:
    """Install the process-wide policy (the agent does this once per run)."""
    global HOSTS
    HOSTS = HostPolicy(path, intervals, default_interval, persist=persist)
    return HOSTS


def interleave(items: Iterable[T], host: Callable[[T], Hashable]) -> list[T]:
    """Round-robin over hosts, keeping each host's items in their original order."""
    queues: dict[Hashable, deque] = {}
    for it in items:
        queues.setdefault(host(it), deque()).append(it)
    out: list[T] = []
    while queues:
        for h in list(queues):
            out.append(queues[h].popleft())
            if not queues[h]:
                del queues[h]
    return out
