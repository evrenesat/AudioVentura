"""Pin the vendored ailocals-v1 contract to its frozen upstream commit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "ailocals-v1"
FROZEN_COMMIT = "e4f1cc9177820f3c3c6a49240c035a54e01e9c45"


def _origin() -> dict:
    return json.loads((CONTRACT / "ORIGIN.json").read_text(encoding="utf-8"))


def test_consumer_pin_records_frozen_commit() -> None:
    consumer = _origin()["consumer"]
    assert consumer["frozen_contract_commit"] == FROZEN_COMMIT
    assert consumer["consumer"] == "audioventura"
    assert len(consumer["files"]) >= 120


def test_vendored_files_match_recorded_hashes() -> None:
    files = _origin()["consumer"]["files"]
    assert files, "ORIGIN.json consumer map must not be empty"
    for rel, expected in sorted(files.items()):
        actual = hashlib.sha256((CONTRACT / rel).read_bytes()).hexdigest()
        assert actual == expected, f"vendored contract drift at {rel}"


def test_vendored_manifest_is_self_consistent() -> None:
    manifest = json.loads((CONTRACT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    fixtures_dir = CONTRACT / "fixtures"
    for case in cases:
        for entry in case["files"]:
            data = (fixtures_dir / entry["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry["sha256"], case["id"]
            assert len(data) == entry["bytes"], case["id"]
