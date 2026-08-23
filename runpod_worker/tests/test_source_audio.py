from __future__ import annotations

import wave
from pathlib import Path

import pytest
import soundfile as sf

from runpod_worker.source_audio import (
    SourceAudioPreparationError,
    prepare_cover_source_to_duration,
)


def _write_source(path: Path, *, frames: int = 4_800, sample_rate: int = 48_000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        samples = b"".join(value.to_bytes(2, "little", signed=True) for value in range(frames))
        output.writeframes(samples)


def test_short_source_is_repeated_to_exact_target_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "prepared.wav"
    _write_source(source)

    result = prepare_cover_source_to_duration(source, destination, target_duration_seconds=0.25)

    assert result == destination
    info = sf.info(destination)
    assert info.frames == 12_000
    assert info.samplerate == 48_000
    assert info.channels == 1
    assert oct(destination.stat().st_mode & 0o777) == "0o600"


def test_long_source_is_truncated_to_exact_target_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "prepared.wav"
    _write_source(source, frames=9_600)

    prepare_cover_source_to_duration(source, destination, target_duration_seconds=0.1)

    assert sf.info(destination).frames == 4_800


def test_invalid_source_removes_partial_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    destination = tmp_path / "prepared.wav"
    source.write_bytes(b"not audio")

    with pytest.raises(SourceAudioPreparationError, match="preparation failed"):
        prepare_cover_source_to_duration(source, destination, target_duration_seconds=10)

    assert not destination.exists()
