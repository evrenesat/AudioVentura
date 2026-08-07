from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from runpod_worker.audio_output import AudioOutputError, convert_wav_to_mp3


def _write_wav(
    path: Path,
    *,
    channels: int = 1,
    sample_rate: int = 48_000,
    sample_width: int = 2,
    frames: int = 4_800,
) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * frames * channels * sample_width)


def test_valid_pcm_wav_becomes_nonempty_real_mp3_and_removes_intermediate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated.wav"
    destination = tmp_path / "generated.mp3"
    _write_wav(source)

    result = convert_wav_to_mp3(source, destination, temporary_root=tmp_path)

    assert result == destination
    encoded = destination.read_bytes()
    assert encoded
    assert encoded[:2] == b"\xff\xfb"
    assert not source.exists()
    assert not (tmp_path / "generated.mp3.part").exists()


def test_mp3_conversion_does_not_use_subprocess_or_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "generated.wav"
    destination = tmp_path / "generated.mp3"
    _write_wav(source, channels=2)

    def fail_subprocess(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("subprocess must not be called")

    monkeypatch.setattr(subprocess, "run", fail_subprocess)
    monkeypatch.setenv("PATH", "")

    convert_wav_to_mp3(source, destination, temporary_root=tmp_path)

    assert destination.stat().st_size > 0


@pytest.mark.parametrize("case", ["malformed", "empty", "non_pcm", "wrong_rate", "wrong_width"])
def test_invalid_wav_is_rejected_without_output_or_part(case: str, tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    destination = tmp_path / "generated.mp3"
    if case == "malformed":
        source.write_bytes(b"not a RIFF file")
    elif case == "empty":
        source.write_bytes(b"")
    else:
        _write_wav(
            source,
            sample_rate=44_100 if case == "wrong_rate" else 48_000,
            sample_width=1 if case == "wrong_width" else 2,
        )
        if case == "non_pcm":
            contents = bytearray(source.read_bytes())
            fmt_offset = contents.index(b"fmt ")
            contents[fmt_offset + 8 : fmt_offset + 10] = (3).to_bytes(2, "little")
            source.write_bytes(contents)

    with pytest.raises(AudioOutputError):
        convert_wav_to_mp3(source, destination, temporary_root=tmp_path)

    assert not destination.exists()
    assert not (tmp_path / "generated.mp3.part").exists()


def test_three_channel_wav_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "generated.wav"
    _write_wav(source, channels=3)

    with pytest.raises(AudioOutputError, match="one or two"):
        convert_wav_to_mp3(source, tmp_path / "generated.mp3", temporary_root=tmp_path)


def test_out_of_root_wav_is_rejected_without_output(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-generated.wav"
    _write_wav(outside)
    destination = tmp_path / "generated.mp3"

    try:
        with pytest.raises(AudioOutputError, match="escaped"):
            convert_wav_to_mp3(outside, destination, temporary_root=tmp_path)
    finally:
        outside.unlink()

    assert not destination.exists()
    assert not (tmp_path / "generated.mp3.part").exists()


def test_encoder_failure_removes_partial_and_final_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "generated.wav"
    destination = tmp_path / "generated.mp3"
    _write_wav(source)

    class FailingEncoder:
        def set_channels(self, _channels: int) -> None:
            return None

        def set_in_sample_rate(self, _sample_rate: int) -> None:
            return None

        def set_out_sample_rate(self, _sample_rate: int) -> None:
            return None

        def set_bit_rate(self, _bitrate: int) -> None:
            return None

        def set_quality(self, _quality: int) -> None:
            return None

        def encode(self, _pcm: bytes) -> bytes:
            raise RuntimeError("codec failure")

        def flush(self) -> bytes:
            return b""

    monkeypatch.setitem(sys.modules, "lameenc", SimpleNamespace(Encoder=FailingEncoder))

    with pytest.raises(AudioOutputError, match="LAME"):
        convert_wav_to_mp3(source, destination, temporary_root=tmp_path)

    assert not destination.exists()
    assert not (tmp_path / "generated.mp3.part").exists()
