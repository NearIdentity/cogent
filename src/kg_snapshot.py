#!/usr/bin/env python3
"""Snapshots of the agent home for scale-out: one agent writes, many MCP servers read.

The update agent is a single writer. To run several MCP servers (on other hosts, in containers, on a scale-to-zero platform) without a shared disk,
the agent *publishes* a versioned bundle after a run and every server *pulls* the newest bundle and switches to it atomically.

    kg_snapshot.py publish --home kg_agent_home --dest /srv/cogent-snapshots [--standards config/standards.json --reports config/reports.json]
    kg_snapshot.py pull    --src /srv/cogent-snapshots|https://host/snapshots --link /srv/cogent/current
    kg_snapshot.py watch   --src ... --link ... --interval 300
    kg_snapshot.py status  --link /srv/cogent/current

``dest`` / ``src`` is a directory (a shared mount, an rclone- or s3fs-mounted bucket, a synced folder) or, for ``pull``, an http(s) base URL that serves
the same files. A destination holds ``cogent-snapshot-<UTC>-<sha8>.tar.gz`` bundles and ``latest.json`` (name, sha256, size, created).

``pull`` verifies the checksum, extracts into ``<link parent>/snapshots/<name>/`` and then repoints the symlink ``--link`` at it with an atomic rename,
so a server started with ``--home <link>`` never sees a half-written tree (it re-reads a graph when its file changes). Older snapshots are pruned.

What goes in: ``staging/`` (graphs, metadata, exported tables and figures, the combined graph), ``tabledb/`` (data dictionaries, relationship maps),
``state/alerts.json``, ``logs/runs.jsonl``, the agent's ``metrics/`` and the two configuration files. What never goes in: ``local/`` (purchased copies), ``archive/``, ``mcp/`` (tokens,
OAuth state), other logs and lock files. The bundle contains the staged text of paywalled documents: publish it only to places where the server itself may run.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("kg_snapshot")
LATEST = "latest.json"
PREFIX = "cogent-snapshot-"
CONFIG_DIR = "config"
INCLUDE = ("staging", "tabledb", "state/alerts.json", "logs/runs.jsonl", "metrics/agent_metrics.json", "metrics/cogent_agent.prom")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _files(home: Path, assets: bool) -> Iterator[tuple[Path, str]]:
    for rel in INCLUDE:
        base = home / rel
        if base.is_file():
            yield base, rel
        elif base.is_dir():
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.is_symlink():
                    continue
                r = p.relative_to(home).as_posix()
                if not assets and "/assets/" in r:
                    continue
                if p.name.startswith(".") and (".tmp" in p.name or p.name.endswith(".tmp")):        # half-written files of a run in progress
                    continue
                yield p, r


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def publish(home: Path, dest: Path, standards: Optional[Path] = None, reports: Optional[Path] = None, keep: int = 5, assets: bool = True) -> dict[str, Any]:
    """Write a bundle and point ``latest.json`` at it. Returns the ``latest.json`` record (plus ``path``)."""
    home, dest = Path(home), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = _utc()
    tmp = dest / f".{PREFIX}{stamp}.partial"
    n = 0
    with tarfile.open(tmp, "w:gz") as tar:
        for path, rel in _files(home, assets):
            tar.add(path, arcname=rel, recursive=False)
            n += 1
        for src, name in ((standards, "standards.json"), (reports, "reports.json")):
            if src and Path(src).is_file():
                tar.add(Path(src), arcname=f"{CONFIG_DIR}/{name}", recursive=False)
                n += 1
        info = tarfile.TarInfo("SNAPSHOT.json")
        meta = json.dumps({"created": stamp, "files": n, "assets": assets}).encode()
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
    digest = _sha256(tmp)
    name = f"{PREFIX}{stamp}-{digest[:8]}.tar.gz"
    os.replace(tmp, dest / name)
    rec = {"name": name, "sha256": digest, "bytes": (dest / name).stat().st_size, "created": stamp, "files": n}
    _atomic_write(dest / LATEST, json.dumps(rec, indent=2) + "\n")
    bundles = sorted(p for p in dest.glob(f"{PREFIX}*.tar.gz"))
    for old in bundles[:-max(1, keep)]:
        old.unlink(missing_ok=True)
    return {**rec, "path": str(dest / name)}


def _fetch(src: str, name: str, to: Optional[Path] = None) -> bytes | Path:
    """Read ``name`` from a directory or URL base. With ``to`` the file is copied / downloaded there and the path returned."""
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src.rstrip("/") + "/" + name, headers={"User-Agent": "cogent-snapshot"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if to is None:
                return resp.read()
            with to.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
            return to
    p = Path(src) / name
    if to is None:
        return p.read_bytes()
    shutil.copyfile(p, to)
    return to


def _safe_extract(archive: Path, target: Path) -> None:
    """Extract plain files and directories only; absolute paths, '..' and links are refused."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ValueError(f"unsafe path in snapshot: {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"snapshot member {m.name!r} is not a plain file or directory")
        try:
            tar.extractall(target, filter="data")
        except TypeError:                                   # Python < 3.12 has no extraction filters; the checks above apply
            tar.extractall(target)


def current(link: Path) -> Optional[dict[str, Any]]:
    """The snapshot ``link`` points to (``SNAPSHOT.json`` content plus ``name`` and ``path``), or None."""
    link = Path(link)
    try:
        target = link.resolve(strict=True)
        meta = json.loads((target / "SNAPSHOT.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {**meta, "name": target.name, "path": str(target)}


def pull(src: str, link: Path, keep: int = 3) -> dict[str, Any]:
    """Fetch the newest bundle from ``src`` and repoint ``link`` at it. ``{"updated": False}`` when the newest one is already current."""
    link = Path(link)
    latest = json.loads(_fetch(src, LATEST))
    root = link.parent / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    name = latest["name"]
    cur = current(link)
    if cur and cur["name"] == name.removesuffix(".tar.gz"):
        return {"updated": False, "name": name}
    incoming = root / f".incoming-{name}"
    try:
        _fetch(src, name, incoming)
        digest = _sha256(incoming)
        if digest != latest["sha256"]:
            raise ValueError(f"checksum mismatch for {name}: expected {latest['sha256'][:12]}…, got {digest[:12]}…")
        target = root / name.removesuffix(".tar.gz")
        if target.exists():
            shutil.rmtree(target)
        staging = Path(tempfile.mkdtemp(prefix=".extract-", dir=root))
        try:
            _safe_extract(incoming, staging)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    finally:
        incoming.unlink(missing_ok=True)
    tmp_link = link.with_name(f".{link.name}.new")
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    os.symlink(target, tmp_link)
    if link.exists() and not link.is_symlink():
        raise ValueError(f"{link} exists and is not a symlink; point --link at a path that only the snapshot tool manages")
    os.replace(tmp_link, link)                                  # atomic: readers see the old tree or the new one
    kept = sorted((p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX)), key=lambda p: p.name)
    for old in kept[:-max(1, keep)]:
        if old != target:
            shutil.rmtree(old, ignore_errors=True)
    return {"updated": True, "name": name, "path": str(target), "bytes": latest.get("bytes"), "previous": cur["name"] if cur else None}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="write a bundle of the agent home to a directory")
    p.add_argument("--home", type=Path, default=Path(os.environ.get("COGENT_HOME", "kg_agent_home")))
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--standards", type=Path, default=Path("config/standards.json"))
    p.add_argument("--reports", type=Path, default=Path("config/reports.json"))
    p.add_argument("--keep", type=int, default=5, help="bundles to keep in the destination")
    p.add_argument("--no-assets", action="store_true", help="leave out exported figures and table CSVs (smaller; get_figure / get_table_csv then fail)")
    for name in ("pull", "watch"):
        q = sub.add_parser(name, help="fetch the newest bundle and switch to it" if name == "pull" else "pull repeatedly")
        q.add_argument("--src", required=True, help="directory or http(s) base URL that holds latest.json")
        q.add_argument("--link", type=Path, required=True, help="symlink the MCP server uses as --home")
        q.add_argument("--keep", type=int, default=3, help="snapshots to keep on disk")
        if name == "watch":
            q.add_argument("--interval", type=float, default=300.0, help="seconds between checks")
    st = sub.add_parser("status", help="which snapshot a link points to")
    st.add_argument("--link", type=Path, required=True)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if a.cmd == "publish":
            print(json.dumps(publish(a.home, a.dest, a.standards, a.reports, a.keep, not a.no_assets), indent=2))
        elif a.cmd == "pull":
            print(json.dumps(pull(a.src, a.link, a.keep), indent=2))
        elif a.cmd == "watch":
            while True:
                try:
                    r = pull(a.src, a.link, a.keep)
                    if r["updated"]:
                        log.info("switched to %s", r["name"])
                except Exception as exc:                        # noqa: BLE001 - keep serving the current snapshot, try again later
                    log.warning("pull failed: %s", exc)
                time.sleep(a.interval)
        else:
            c = current(a.link)
            print(json.dumps(c, indent=2) if c else "no snapshot")
            return 0 if c else 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
