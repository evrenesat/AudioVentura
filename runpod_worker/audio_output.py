"""Runpod-local generated-audio format handling."""

from __future__ import annotations

import os
import wave
from pathlib import Path

OUTPUT_FORMATS = frozenset({"flac", "mp3", "wav"})
MP3_SAMPLE_RATE = 48_000
MP3_BITRATE_KBPS = 192


class AudioOutputError(RuntimeError):
    """Raised when a generated audio file cannot be finalized safely."""


def internal_audio_format(requested_format: str) -> str:
    """Return the format ACE-Step should use for its own temporary output."""

    if requested_format not in OUTPUT_FORMATS:
        raise AudioOutputError(f"unsupported output format: {requested_format}")
    return "wav" if requested_format == "mp3" else requested_format


def finalize_generated_output(
    generated_path: Path,
    *,
    requested_format: str,
    temporary_root: Path,
) -> Path:
    """Convert an internal WAV when needed and return the requested output path."""

    if requested_format != "mp3":
        return generated_path
    destination = generated_path.with_suffix(".mp3")
    convert_wav_to_mp3(generated_path, destination, temporary_root=temporary_root)
    return destination


def convert_wav_to_mp3(source_path: Path, destination_path: Path, *, temporary_root: Path) -> Path:
    """Encode one validated PCM WAV to MP3 without starting another process."""

    root = temporary_root.resolve()
    if source_path.is_symlink():
        raise AudioOutputError("WAV input must not be a symlink")
    if destination_path.is_symlink():
        raise AudioOutputError("MP3 output must not be a symlink")
    source = source_path.resolve()
    destination = destination_path.resolve()
    part = destination.with_name(f"{destination.name}.part")
    _require_inside_root(root, source, "WAV input")
    _require_inside_root(root, destination, "MP3 output")
    _require_inside_root(root, part, "MP3 partial output")
    if source.suffix.lower() != ".wav":
        raise AudioOutputError("MP3 conversion requires a WAV input")
    if destination.suffix.lower() != ".mp3":
        raise AudioOutputError("MP3 conversion requires an MP3 output")
    if not source.is_file() or source.is_symlink():
        raise AudioOutputError("WAV input is not a regular file")
    if destination.exists() or destination.is_symlink():
        raise AudioOutputError("MP3 output already exists")

    _remove_artifact(part)
    try:
        channels, pcm = _read_pcm_wav(source)
        encoded = _encode_mp3(channels, pcm)
        if not encoded:
            raise AudioOutputError("LAME returned an empty MP3")
        with part.open("xb") as output_file:
            output_file.write(encoded)
            output_file.flush()
            os.fsync(output_file.fileno())
        if not part.is_file() or part.stat().st_size <= 0:
            raise AudioOutputError("MP3 partial output is empty")
        os.replace(part, destination)
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise AudioOutputError("MP3 output is empty")
        source.unlink()
    except AudioOutputError:
        _remove_artifact(part)
        _remove_artifact(destination)
        raise
    except Exception as exc:
        _remove_artifact(part)
        _remove_artifact(destination)
        raise AudioOutputError("WAV to MP3 conversion failed") from exc
    return destination


def _read_pcm_wav(source: Path) -> tuple[int, bytes]:
    try:
        with wave.open(str(source), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            if wav_file.getcomptype() != "NONE":
                raise AudioOutputError("WAV input must use uncompressed PCM")
            if channels not in (1, 2):
                raise AudioOutputError("WAV input must have one or two channels")
            if sample_rate != MP3_SAMPLE_RATE:
                raise AudioOutputError("WAV input must use a 48 kHz sample rate")
            if sample_width != 2:
                raise AudioOutputError("WAV input must use 16-bit samples")
            if frame_count <= 0:
                raise AudioOutputError("WAV input must contain audio frames")
            pcm = wav_file.readframes(frame_count)
    except AudioOutputError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioOutputError("WAV input is malformed") from exc
    expected_bytes = frame_count * channels * sample_width
    if len(pcm) != expected_bytes:
        raise AudioOutputError("WAV input has truncated audio frames")
    return channels, pcm


def _encode_mp3(channels: int, pcm: bytes) -> bytes:
    try:
        import lameenc  # type: ignore[import-not-found]

        encoder = lameenc.Encoder()
        encoder.set_channels(channels)
        encoder.set_in_sample_rate(MP3_SAMPLE_RATE)
        encoder.set_out_sample_rate(MP3_SAMPLE_RATE)
        encoder.set_bit_rate(MP3_BITRATE_KBPS)
        encoder.set_quality(2)
        return bytes(encoder.encode(pcm)) + bytes(encoder.flush())
    except AudioOutputError:
        raise
    except Exception as exc:
        raise AudioOutputError("LAME MP3 encoding failed") from exc


def _require_inside_root(root: Path, path: Path, label: str) -> None:
    if not path.is_relative_to(root):
        raise AudioOutputError(f"{label} escaped the worker temporary directory")


def _remove_artifact(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError:
        pass
