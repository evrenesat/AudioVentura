from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from ace_midi_mock.corpus import build_manifest
from ace_midi_mock.renderer import MidiRenderer


def test_renderer_streams_pcm_to_mp3_without_wav(
    fixture_manifest, mock_settings, tmp_path: Path
) -> None:
    archive_path, _fixture = fixture_manifest
    manifest = build_manifest(
        archive_path,
        expected_archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        expected_member_count=3,
        strict_identity=False,
    )
    fake_fluidsynth = tmp_path / "fluidsynth"
    fake_fluidsynth.write_text(
        "#!/bin/sh\ndd if=/dev/zero bs=4 count=4410 2>/dev/null\n",
        encoding="utf-8",
    )
    fake_fluidsynth.chmod(0o700)
    settings = replace(mock_settings, fluidsynth_binary=str(fake_fluidsynth))
    output = MidiRenderer(settings, manifest).render(0, tmp_path / "render")
    assert output.path.suffix == ".mp3"
    encoded = output.path.read_bytes()
    assert encoded.startswith(b"ID3") or encoded[0:2] == b"\xff\xfb"
    assert output.byte_size == output.path.stat().st_size > 0
    assert output.duration_seconds > 0
    assert not output.path.with_suffix(".wav").exists()
    assert output.sha256 == hashlib.sha256(output.path.read_bytes()).hexdigest()
