"""Login for the chat web app: one user, credentials in a git-ignored ``.env`` file on the server.

    CHAT_USERNAME=alice
    CHAT_PASSWORD=a-long-passphrase                 # or, better, a hash:  python src/kg_chat_app.py hash-password
    CHAT_PASSWORD_HASH=scrypt:32768:8:1$...         # if present it is used and CHAT_PASSWORD is ignored
    FLASK_SECRET_KEY=...                            # optional: keeps sessions valid across restarts

The file is parsed here (no dependency, and nothing is copied into the process environment). The password is compared in constant time, a failed
login says nothing about which part was wrong, and repeated failures from one address are slowed down by a growing lock-out. The password is never
logged and never leaves this module except as the result of a comparison.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from werkzeug.security import check_password_hash, generate_password_hash

import kg_telemetry as telemetry

MIN_PASSWORD_CHARS = 10          # shorter plaintext passwords are accepted but warned about at start-up
_DUMMY_HASH = generate_password_hash("not-the-password")


class AuthConfigError(RuntimeError):
    """The credentials are missing or unusable; the message says how to fix it (and never contains a secret)."""


def parse_env(text: str) -> dict[str, str]:
    """``KEY=value`` lines: ``#`` comments, an optional ``export``, single or double quotes around the value, last duplicate wins."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key or key[0].isdigit() or not all(c.isalnum() or c == "_" for c in key):
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:                                      # an inline comment after an unquoted value
            val = val.split(" #", 1)[0].rstrip()
        out[key] = val
    return out


@dataclass
class Credentials:
    username: str
    password: str = field(default="", repr=False)              # plaintext, from CHAT_PASSWORD (empty when a hash is used)
    password_hash: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_env_file(cls, path: Path) -> "Credentials":
        p = Path(path)
        if not p.is_file():
            raise AuthConfigError(f"login credentials file not found: {p}. Create it from .env.example (it is git-ignored) with CHAT_USERNAME and CHAT_PASSWORD, "
                                  "or start with --no-login on 127.0.0.1.")
        env = parse_env(p.read_text(encoding="utf-8"))
        user = env.get("CHAT_USERNAME", "").strip()
        pw, ph = env.get("CHAT_PASSWORD", ""), env.get("CHAT_PASSWORD_HASH", "").strip()
        if not user:
            raise AuthConfigError(f"{p}: CHAT_USERNAME is not set.")
        if not pw and not ph:
            raise AuthConfigError(f"{p}: set CHAT_PASSWORD (or CHAT_PASSWORD_HASH, made with: python src/kg_chat_app.py hash-password).")
        warns = []
        try:
            if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                warns.append(f"{p} is readable by other users; run: chmod 600 {p}")
        except OSError:
            pass
        if ph and ph.split(":", 1)[0] not in ("scrypt", "pbkdf2"):
            raise AuthConfigError(f"{p}: CHAT_PASSWORD_HASH is not a hash made by `hash-password`.")
        if not ph and len(pw) < MIN_PASSWORD_CHARS:
            warns.append(f"the password in {p} is shorter than {MIN_PASSWORD_CHARS} characters; use a longer one or a hash")
        return cls(username=user, password="" if ph else pw, password_hash=ph, secret_key=env.get("FLASK_SECRET_KEY", ""), warnings=warns)

    def verify(self, username: str, password: str) -> bool:
        """Constant-time check; does the same work whether the user name or the password is wrong."""
        user_ok = hmac.compare_digest(hashlib.sha256(username.encode()).digest(), hashlib.sha256(self.username.encode()).digest())
        if self.password_hash:
            pw_ok = check_password_hash(self.password_hash, password)
        else:
            pw_ok = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(self.password.encode()).digest())
            check_password_hash(_DUMMY_HASH, password)         # keep the timing of the two modes alike
        return bool(user_ok and pw_ok)


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


class LoginThrottle:
    """Slow down guessing: after ``free`` failures from one address, each further failure locks it out for ``base`` s, doubling, up to ``cap`` s.
    In memory, per process. Behind a reverse proxy every client looks like the proxy's address (run the proxy's own rate limit as well)."""

    def __init__(self, free: int = 4, base: float = 30.0, cap: float = 900.0, clock: Callable[[], float] = time.monotonic):
        self.free, self.base, self.cap, self.clock = free, base, cap, clock
        self._state: dict[str, tuple[int, float]] = {}           # key -> (failures, locked until)
        self._lock = threading.Lock()

    def wait(self, key: str) -> float:
        """Seconds the address must still wait (0 = may try)."""
        with self._lock:
            return max(0.0, self._state.get(key, (0, 0.0))[1] - self.clock())

    def failed(self, key: str) -> float:
        with self._lock:
            n, _ = self._state.get(key, (0, 0.0))
            n += 1
            until = self.clock() + min(self.cap, self.base * 2 ** (n - self.free - 1)) if n > self.free else 0.0
            self._state[key] = (n, until)
            if len(self._state) > 5000:                          # bound the memory
                for k in [k for k, (_, u) in self._state.items() if u < self.clock()][:1000]:
                    self._state.pop(k, None)
            return max(0.0, until - self.clock())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


def record(outcome: str) -> None:
    telemetry.record_chat_login(outcome)
