from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from ace_midi_mock.corpus import CorpusError, build_manifest, extract_member


def test_manifest_sorts_utf8_and_extracts_one_member(fixture_manifest, tmp_path: Path) -> None:
    archive_path, _fixture = fixture_manifest
    manifest = build_manifest(
        archive_path,
        expected_archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        expected_member_count=3,
        strict_identity=False,
    )
    assert [member.name for member in manifest.members] == [
        "midis/A.mid",
        "midis/B.mid",
        "midis/C.MID",
    ]
    destination = tmp_path / "job"
    extracted = extract_member(archive_path, manifest.member(1), destination)
    assert extracted.read_bytes().startswith(b"MThd")
    assert extracted.stat().st_mode & 0o777 == 0o600
    assert list(destination.glob("*.part")) == []


@pytest.mark.parametrize(
    "name",
    ["/absolute.mid", "midis/../unsafe.mid", "midis\\unsafe.mid", "midis/unsafe.txt"],
)
def test_manifest_rejects_unsafe_member_names(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("midis/", b"")
        archive.writestr(name, b"MThdbad")
    with pytest.raises(CorpusError):
        build_manifest(
            archive_path,
            expected_archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            expected_member_count=1,
            strict_identity=False,
        )
