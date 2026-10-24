"""Per-session preference storage for the MCP server (the overlays set with ``set_session_preferences``).

* ``memory`` (default) lives in one process and is lost on restart: right for a single server process.
* ``sqlite:<path>`` is shared by every worker process on one host (and survives restarts).
* ``redis://host:port/db`` is shared by servers on several hosts (needs the optional ``redis`` package).

All three expose ``get(key) -> dict``, ``set(key, prefs)``, ``clear(key) -> bool`` and ``count()``; entries expire after ``ttl`` seconds.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SESSION_TTL = 8 * 3600


class MemorySessions:
    kind = "memory"

    def __init__(self, ttl: float = SESSION_TTL):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, (_, exp) in self._d.items() if exp < now]:
            del self._d[k]

    def get(self, key: str) -> dict[str, Any]:
        with self._lock:
            self._purge()
            return dict(self._d.get(key, ({}, 0))[0])

    def set(self, key: str, prefs: dict[str, Any]) -> None:
        with self._lock:
            self._d[key] = (dict(prefs), time.time() + self.ttl)

    def clear(self, key: str) -> bool:
        with self._lock:
            return self._d.pop(key, None) is not None

    def count(self) -> int:
        with self._lock:
            self._purge()
            return len(self._d)


class SqliteSessions:
    """One SQLite file (WAL mode) shared by the worker processes of a host. A connection per call keeps it safe across threads and forks."""
    kind = "sqlite"

    def __init__(self, path: str | Path, ttl: float = SESSION_TTL):
        self.path, self.ttl = str(path), ttl
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def get(self, key: str) -> dict[str, Any]:
        with self._db() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            row = c.execute("SELECT data FROM sessions WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set(self, key: str, prefs: dict[str, Any]) -> None:
        with self._db() as c:
            c.execute("INSERT INTO sessions (key, data, expires) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET data = excluded.data, expires = excluded.expires",
                      (key, json.dumps(prefs), time.time() + self.ttl))

    def clear(self, key: str) -> bool:
        with self._db() as c:
            return c.execute("DELETE FROM sessions WHERE key = ?", (key,)).rowcount > 0

    def count(self) -> int:
        with self._db() as c:
            return c.execute("SELECT count(*) FROM sessions WHERE expires >= ?", (time.time(),)).fetchone()[0]


class RedisSessions:
    """Sessions in Redis, for servers on several hosts. ``client`` may be any object with get / setex / delete / scan_iter (tests pass a stub)."""
    kind = "redis"
    PREFIX = "cogent:session:"

    def __init__(self, url: str = "", ttl: float = SESSION_TTL, client: Optional[Any] = None):
        self.ttl = int(ttl)
        if client is None:
            try:
                import redis  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("redis sessions need the 'redis' package: pip install redis") from exc
            client = redis.Redis.from_url(url, decode_responses=True)
        self.r = client

    def get(self, key: str) -> dict[str, Any]:
        raw = self.r.get(self.PREFIX + key)
        return json.loads(raw) if raw else {}

    def set(self, key: str, prefs: dict[str, Any]) -> None:
        self.r.setex(self.PREFIX + key, self.ttl, json.dumps(prefs))

    def clear(self, key: str) -> bool:
        return bool(self.r.delete(self.PREFIX + key))

    def count(self) -> int:
        return sum(1 for _ in self.r.scan_iter(match=self.PREFIX + "*"))


def make_sessions(spec: Optional[str] = None, ttl: float = SESSION_TTL):
    """``None`` / ``memory`` / ``sqlite:/path/sessions.db`` / ``redis://host:6379/0``."""
    spec = (spec or "memory").strip()
    if spec == "memory":
        return MemorySessions(ttl)
    if spec.startswith("sqlite:"):
        return SqliteSessions(spec[len("sqlite:"):], ttl)
    if spec.startswith(("redis://", "rediss://")):
        return RedisSessions(spec, ttl)
    raise ValueError(f"unknown session store {spec!r}; use memory, sqlite:<path> or redis://host:port/db")
