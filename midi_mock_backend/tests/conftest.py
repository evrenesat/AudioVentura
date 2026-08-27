from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from ace_midi_mock.config import MockSettings
from ace_midi_mock.corpus import CorpusManifest, CorpusMember


def midi_bytes(note: int = 60) -> bytes:
    # A tiny valid MIDI header and one-track note event. The renderer tests
    # replace FluidSynth, so the fixture stays small and repository-owned.
    track = (
        b"MTrk"
        + (12).to_bytes(4, "big")
        + bytes([0, 0x90, note, 0x60, 0x81, 0x40, 0x80, note, 0x40, 0, 0xFF, 0x2F, 0])
    )
    return (
        b"MThd"
        + (6).to_bytes(4, "big")
        + (0).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + (480).to_bytes(2, "big")
        + track
    )


@pytest.fixture
def fixture_manifest(tmp_path: Path) -> tuple[Path, CorpusManifest]:
    archive_path = tmp_path / "corpus.zip"
    entries = {
        "midis/B.mid": midi_bytes(61),
        "midis/A.mid": midi_bytes(60),
        "midis/C.MID": midi_bytes(62),
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("midis/", b"")
        for name, value in entries.items():
            archive.writestr(name, value)
    members = tuple(
        CorpusMember(index, name, hashlib.sha256(entries[name]).hexdigest(), len(entries[name]))
        for index, name in enumerate(sorted(entries, key=lambda item: item.encode("utf-8")))
    )
    document_digest = hashlib.sha256(archive_path.read_bytes() + b"fixture-manifest").hexdigest()
    return archive_path, CorpusManifest(
        archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        members=members,
        manifest_sha256=document_digest,
    )


@pytest.fixture
def mock_settings(tmp_path: Path, fixture_manifest: tuple[Path, CorpusManifest]) -> MockSettings:
    archive_path, _manifest = fixture_manifest
    return MockSettings(
        token="test-token",
        state_root=tmp_path / "state",
        corpus_archive=archive_path,
        manifest_path=tmp_path / "manifest.json",
        soundfont_path=tmp_path / "soundfont.sf2",
        fluidsynth_binary="/bin/false",
        max_output_bytes=1_000_000,
        render_timeout_seconds=10,
        upload_timeout_seconds=10,
    )
