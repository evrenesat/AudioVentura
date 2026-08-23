"""Provider-neutral source preparation for ACE-Step cover generation."""

from __future__ import annotations

import math
import os
from pathlib import Path

_BLOCK_FRAMES = 65_536
_MAX_TARGET_DURATION_SECONDS = 600.0


class SourceAudioPreparationError(RuntimeError):
    """Raised when a custom-duration cover source cannot be prepared safely."""


def prepare_cover_source_to_duration(
    source_path: Path,
    destination_path: Path,
    *,
    target_duration_seconds: float,
) -> Path:
    """Stream one decoded source into an exact-duration PCM WAV.

    ACE-Step v0.1.8 locks cover duration to the decoded source length. A
    shorter source is therefore repeated and a longer source is truncated.
    The temporary WAV remains inside the worker-owned job directory.
    """

    if (
        isinstance(target_duration_seconds, bool)
        or not isinstance(target_duration_seconds, (int, float))
        or not math.isfinite(float(target_duration_seconds))
        or float(target_duration_seconds) <= 0
        or float(target_duration_seconds) > _MAX_TARGET_DURATION_SECONDS
    ):
        raise SourceAudioPreparationError("cover target duration is invalid")
    if source_path.is_symlink() or not source_path.is_file():
        raise SourceAudioPreparationError("cover source is not a regular file")
    if destination_path.exists() or destination_path.is_symlink():
        raise SourceAudioPreparationError("prepared cover source already exists")
    if source_path.resolve().parent != destination_path.parent.resolve():
        raise SourceAudioPreparationError("prepared cover source escaped its directory")

    try:
        import soundfile as sf

        with sf.SoundFile(str(source_path), mode="r") as source:
            if source.frames <= 0 or source.samplerate <= 0 or source.channels not in {1, 2}:
                raise SourceAudioPreparationError("cover source has invalid audio properties")
            target_frames = round(float(target_duration_seconds) * source.samplerate)
            if target_frames <= 0:
                raise SourceAudioPreparationError("cover target duration has no audio frames")
            with sf.SoundFile(
                str(destination_path),
                mode="x",
                samplerate=source.samplerate,
                channels=source.channels,
                format="WAV",
                subtype="PCM_16",
            ) as destination:
                remaining = target_frames
                while remaining > 0:
                    source.seek(0)
                    copied_this_pass = 0
                    while remaining > 0:
                        block = source.read(
                            min(_BLOCK_FRAMES, remaining), dtype="float32", always_2d=True
                        )
                        if len(block) == 0:
                            break
                        destination.write(block)
                        copied = len(block)
                        copied_this_pass += copied
                        remaining -= copied
                    if copied_this_pass == 0:
                        raise SourceAudioPreparationError("cover source decoded to no audio")
        os.chmod(destination_path, 0o600)
        return destination_path
    except SourceAudioPreparationError:
        _remove_partial(destination_path)
        raise
    except Exception as exc:
        _remove_partial(destination_path)
        raise SourceAudioPreparationError("cover source preparation failed") from exc


def _remove_partial(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError:
        pass
