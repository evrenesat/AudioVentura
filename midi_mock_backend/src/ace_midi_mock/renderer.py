"""Bounded FluidSynth-to-LAME rendering without a persistent WAV file."""

from __future__ import annotations

import hashlib
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import lameenc

from .config import MockSettings
from .corpus import CorpusManifest, extract_member

PCM_CHUNK_BYTES = 64 * 1024


class RenderError(RuntimeError):
    """Safe, bounded rendering failure classification."""

    def __init__(self, code: str, message: str = "MIDI rendering failed") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RenderedOutput:
    path: Path
    byte_size: int
    sha256: str
    duration_seconds: float


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a renderer process group and never leak it past cleanup."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class MidiRenderer:
    """Render one manifest member with a fixed audio format and hard limits."""

    def __init__(self, settings: MockSettings, manifest: CorpusManifest) -> None:
        self.settings = settings
        self.manifest = manifest

    def _argv(self, midi_path: Path) -> list[str]:
        # FluidSynth's fast-render mode writes raw signed 16-bit PCM to stdout.
        # Keep this as an argv list: no shell interpolation is ever involved.
        return [
            self.settings.fluidsynth_binary,
            "--no-shell",
            "--quiet",
            "--sample-rate",
            str(self.settings.sample_rate),
            "--audio-file-type",
            "raw",
            "--audio-file-format",
            "s16",
            "--audio-channels",
            "1",
            "--fast-render",
            "/dev/stdout",
            str(self.settings.soundfont_path),
            str(midi_path),
        ]

    def render(
        self,
        member_index: int,
        job_directory: Path,
        *,
        cancelled: Event | None = None,
    ) -> RenderedOutput:
        member = self.manifest.member(member_index)
        job_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_directory.chmod(0o700)
        midi_path = extract_member(self.settings.corpus_archive, member, job_directory / "input")
        output_path = job_directory / "output.mp3"
        output_part = job_directory / "output.mp3.part"
        max_pcm_bytes = self.settings.max_duration_seconds * self.settings.sample_rate * 2 * 2
        process: subprocess.Popen[bytes] | None = None
        pcm_bytes = 0
        capped = False
        encoder = lameenc.Encoder()
        encoder.set_channels(2)
        encoder.set_in_sample_rate(self.settings.sample_rate)
        encoder.set_out_sample_rate(self.settings.sample_rate)
        encoder.set_bit_rate(self.settings.bitrate_kbps)
        encoder.set_quality(2)
        output_bytes = 0
        digest = hashlib.sha256()
        started = time.monotonic()
        try:
            with output_part.open("xb") as output:
                process = subprocess.Popen(
                    self._argv(midi_path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    shell=False,
                )
                assert process.stdout is not None
                stdout = process.stdout
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise RenderError("cancelled", "MIDI rendering was cancelled")
                    remaining = self.settings.render_timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise RenderError("render_timeout", "MIDI rendering exceeded its deadline")
                    ready, _, _ = select.select([stdout], [], [], min(1.0, remaining))
                    if not ready:
                        continue
                    # The descriptor was selected as readable.  Read from the
                    # descriptor directly so BufferedReader.read() cannot
                    # wait for its own larger fill buffer after the deadline
                    # check above.
                    block = os.read(stdout.fileno(), PCM_CHUNK_BYTES)
                    if not block:
                        break
                    remaining_pcm = max_pcm_bytes - pcm_bytes
                    if len(block) > remaining_pcm:
                        block = block[:remaining_pcm]
                        capped = True
                    frame_size = 4  # signed 16-bit stereo
                    block = block[: len(block) - (len(block) % frame_size)]
                    if block:
                        pcm_bytes += len(block)
                        encoded = encoder.encode(block)
                        if output_bytes + len(encoded) > self.settings.max_output_bytes:
                            raise RenderError(
                                "output_too_large", "rendered MP3 exceeds its byte ceiling"
                            )
                        output.write(encoded)
                        digest.update(encoded)
                        output_bytes += len(encoded)
                    if capped:
                        _terminate_process_group(process)
                        break
                if not capped:
                    return_code = process.wait(timeout=2)
                    if return_code != 0:
                        raise RenderError(
                            "renderer_failed", "FluidSynth did not complete successfully"
                        )
                tail = encoder.flush()
                if output_bytes + len(tail) > self.settings.max_output_bytes:
                    raise RenderError("output_too_large", "rendered MP3 exceeds its byte ceiling")
                output.write(tail)
                digest.update(tail)
                output_bytes += len(tail)
                output.flush()
                os.fsync(output.fileno())
            if pcm_bytes <= 0 or output_bytes <= 0:
                raise RenderError("empty_render", "renderer returned no audio")
            output_part.chmod(0o600)
            os.replace(output_part, output_path)
            output_path.chmod(0o600)
            return RenderedOutput(
                output_path,
                output_bytes,
                digest.hexdigest(),
                round(pcm_bytes / (self.settings.sample_rate * 2 * 2), 3),
            )
        except RenderError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RenderError("renderer_error") from exc
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)
            try:
                output_part.unlink()
            except OSError:
                pass
