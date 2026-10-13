"""Schema / consistency checks on the shipped configuration files.

These test *invariants*, never specific entries, so adding, removing or renumbering standards and reports does not break
them. Point ``COGENT_CONFIG_DIR`` at another directory to check a different pair of files; ``COGENT_STRICT_CONFIG=1``
turns dangling references into failures."""
import json
import os
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg_models import DocEntry, VersionTracking  # noqa: E402

STRICT = os.environ.get("COGENT_STRICT_CONFIG") == "1"
CONFIG = Path(os.environ.get("COGENT_CONFIG_DIR", ROOT / "config"))


def load(name, field):
    path = CONFIG / name
    if not path.exists():
        pytest.skip(f"{path} not found")
    return json.loads(path.read_text(encoding="utf-8"))[field]


@pytest.fixture(scope="module")
def standards():
    return load("standards.json", "standards")


@pytest.fixture(scope="module")
def reports():
    return load("reports.json", "reports")


def is_url(u):
    return isinstance(u, str) and u.startswith(("http://", "https://")) and " " not in u


def test_ids_are_unique_positive_ints(standards, reports):
    for items in (standards, reports):
        ids = [x["id"] for x in items]
        assert all(isinstance(i, int) and i > 0 for i in ids)
        assert len(ids) == len(set(ids)), "duplicate ids"


def test_every_entry_parses_and_has_a_name(standards, reports):
    for kind, items in (("standard", standards), ("report", reports)):
        for x in items:
            e = DocEntry.from_raw(kind, x)
            assert e.name.strip(), (kind, x["id"])


def test_urls_are_wellformed(standards, reports):
    for kind, items in (("standard", standards), ("report", reports)):
        for x in items:
            e = DocEntry.from_raw(kind, x)
            for l in e.links:
                assert is_url(l.url), (e.key, l.url)
            for u in e.tracking_urls:
                assert is_url(u), (e.key, u)


def test_reports_only_reference_existing_standards(standards, reports):
    known = {x["id"] for x in standards}
    for r in reports:
        ids = r.get("standards_to_comply_with_ids", [])
        assert len(ids) == len(set(ids)), f"report {r['id']}: duplicate standard ids"
        missing = set(ids) - known
        if missing:
            # the agent tolerates this (it adds a placeholder node), so it is a data-quality problem: warn by default,
            # fail with COGENT_STRICT_CONFIG=1 (e.g. in CI for the config repository)
            msg = f"report {r['id']} references unknown standards {sorted(missing)}"
            if STRICT:
                pytest.fail(msg)
            warnings.warn(msg, stacklevel=1)


def test_overlaps_are_valid_and_symmetric(standards):
    known = {x["id"] for x in standards}
    ov = {x["id"]: {o["id"] for o in x.get("overlaps_with", [])} for x in standards}
    for sid, others in ov.items():
        assert sid not in others, f"standard {sid} overlaps itself"
        assert others <= known, f"standard {sid}: unknown overlap targets {others - known}"
        for o in others & known:
            assert sid in ov[o], f"overlap {sid}->{o} is not declared back"


def test_report_text_and_ids_agree(reports):
    """If a report's free-text column cites '#N', N must be one of its ids (annotation drift)."""
    import re
    for r in reports:
        txt = r.get("standards_to_comply_with_text") or ""
        cited = {int(n) for n in re.findall(r"#(\d+)", txt)}
        assert cited <= set(r.get("standards_to_comply_with_ids", [])), f"report {r['id']}: text cites {cited}"


def test_existing_version_tracking_blocks_validate(standards, reports):
    for items in (standards, reports):
        for x in items:
            if "version_tracking" in x:
                VersionTracking.model_validate(x["version_tracking"])


def test_previous_versions_are_not_live_links(standards):
    for x in standards:
        old = {p.get("url") for p in x.get("previous_versions", []) if isinstance(p, dict)}
        assert not old & {l["url"] for l in x.get("links", [])}, f"standard {x['id']}: superseded URL still in links"
