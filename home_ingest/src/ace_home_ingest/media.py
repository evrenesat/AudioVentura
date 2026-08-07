"""YouTube validation, bounded download, and canonical audio preparation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
APPROVED_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}
)
PLAYLIST_QUERY_KEYS = frozenset({"list", "index", "start_radio", "playlist", "pp"})
ALLOWED_WATCH_QUERY_KEYS = frozenset({"v", "t", "start"})
DURATION_TOLERANCE_MAX_SECONDS = 5.0
CHUNK_SIZE = 1024 * 1024


class IngestError(RuntimeError):
    """A stable error that may be safely surfaced to the controller."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class YouTubeTarget:
    video_id: str
    canonical_url: str


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    video_id: str
    title: str
    canonical_url: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class AudioProbe:
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PreparedSource:
    metadata: VideoMetadata
    path: Path
    byte_size: int
    sha256: str


YTDLPFactory = Callable[[dict[str, Any]], Any]


def validate_youtube_url(value: str) -> YouTubeTarget:
    """Accept only one public YouTube video and reject redirector forms."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
    ):
        raise IngestError("youtube_url_rejected", "the URL is not an approved single-video URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname.lower() if parsed.hostname else None
        port = parsed.port
    except ValueError as exc:
        raise IngestError(
            "youtube_url_rejected", "the URL is not an approved single-video URL"
        ) from exc
    if (
        parsed.scheme != "https"
        or hostname not in APPROVED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise IngestError("youtube_url_rejected", "the URL is not an approved single-video URL")

    query = parse_qs(parsed.query, keep_blank_values=True)
    if PLAYLIST_QUERY_KEYS.intersection(query):
        raise IngestError("youtube_url_rejected", "the URL is not an approved single-video URL")

    video_id: str | None = None
    if hostname == "youtu.be":
        parts = parsed.path.split("/")
        if len(parts) != 2 or not parts[1] or set(query) - {"t", "start"}:
            raise IngestError("youtube_url_rejected", "the URL is not an approved single-video URL")
        video_id = parts[1]
    elif parsed.path == "/watch":
        if set(query) - ALLOWED_WATCH_QUERY_KEYS or len(query.get("v", [])) != 1:
            raise IngestError("youtube_url_rejected", "the URL is not an approved single-video URL")
        video_id = query["v"][0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] in {"shorts", "embed"} and not query:
            video_id = parts[1]

    if video_id is None or not VIDEO_ID_RE.fullmatch(video_id):
        raise IngestError("youtube_url_rejected", "the URL is not an approved single-video URL")
    return YouTubeTarget(video_id, f"https://www.youtube.com/watch?v={video_id}")


def _default_ytdlp_factory(options: dict[str, Any]) -> Any:
    from yt_dlp import YoutubeDL

    return YoutubeDL(options)


def _sanitize_title(value: Any) -> str:
    if not isinstance(value, str):
        raise IngestError("youtube_metadata_failed", "YouTube metadata did not contain a title")
    cleaned = "".join(
        " " if character in {"\r", "\n", "\t"} else character
        for character in value
        if unicodedata.category(character)[0] != "C" or character in {"\r", "\n", "\t", " "}
    )
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise IngestError("youtube_metadata_failed", "YouTube metadata did not contain a title")
    return cleaned[:300]


def _duration(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise IngestError("youtube_metadata_failed", "YouTube metadata did not contain duration")
    try:
        duration = float(value)
    except ValueError as exc:
        raise IngestError(
            "youtube_metadata_failed", "YouTube metadata contained invalid duration"
        ) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise IngestError("youtube_metadata_failed", "YouTube metadata contained invalid duration")
    return duration


def _metadata_from_info(
    info: Any, target: YouTubeTarget, max_duration_seconds: int
) -> VideoMetadata:
    if not isinstance(info, Mapping) or info.get("_type") in {"playlist", "multi_video"}:
        raise IngestError("youtube_metadata_failed", "YouTube did not return one video")
    video_id = info.get("id")
    if not isinstance(video_id, str) or video_id != target.video_id:
        raise IngestError("youtube_metadata_failed", "YouTube returned an unexpected video")
    webpage_url = info.get("webpage_url")
    if not isinstance(webpage_url, str):
        raise IngestError(
            "youtube_metadata_failed", "YouTube metadata did not contain a canonical URL"
        )
    canonical = validate_youtube_url(webpage_url)
    if canonical.video_id != target.video_id:
        raise IngestError("youtube_metadata_failed", "YouTube returned an unexpected canonical URL")
    duration_seconds = _duration(info.get("duration"))
    if duration_seconds > max_duration_seconds:
        raise IngestError("youtube_duration_exceeded", "the YouTube video is longer than allowed")
    return VideoMetadata(
        video_id=video_id,
        title=_sanitize_title(info.get("title")),
        canonical_url=canonical.canonical_url,
        duration_seconds=duration_seconds,
    )


def _is_blocked_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "sign in",
            "login",
            "log in",
            "confirm you’re not a bot",
            "confirm you are not a bot",
            "age-restricted",
            "age restricted",
            "bot",
        )
    )


def _download_progress_hook(max_source_bytes: int) -> Callable[[dict[str, Any]], None]:
    def hook(progress: dict[str, Any]) -> None:
        downloaded = progress.get("downloaded_bytes", 0)
        estimate = progress.get("total_bytes") or progress.get("total_bytes_estimate")
        values = [value for value in (downloaded, estimate) if isinstance(value, int)]
        if any(value > max_source_bytes for value in values):
            raise IngestError(
                "source_size_exceeded", "the downloaded source exceeds the byte limit"
            )

    return hook


def _safe_download_candidate(job_directory: Path) -> Path:
    candidates = sorted(
        path
        for path in job_directory.iterdir()
        if path.name.startswith("download.") and path.is_file() and not path.is_symlink()
    )
    if len(candidates) != 1:
        raise IngestError("youtube_download_failed", "YouTube did not produce one source file")
    candidate = candidates[0]
    if candidate.resolve().parent != job_directory.resolve():
        raise IngestError(
            "youtube_download_failed", "the downloaded file escaped its temp directory"
        )
    return candidate


def download_youtube(
    url: str,
    job_directory: Path,
    *,
    max_duration_seconds: int,
    max_source_bytes: int,
    youtube_dl_factory: YTDLPFactory | None = None,
) -> tuple[VideoMetadata, Path]:
    """Run yt-dlp metadata inspection and download into a controlled directory."""

    target = validate_youtube_url(url)
    if job_directory.is_symlink() or (job_directory.exists() and not job_directory.is_dir()):
        raise IngestError("youtube_download_failed", "the job temp directory is not safe")
    job_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_directory.chmod(0o700)
    if job_directory.resolve().parent != job_directory.parent.resolve():
        raise IngestError("youtube_download_failed", "the job temp directory escaped its root")
    factory = youtube_dl_factory or _default_ytdlp_factory
    metadata_options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with factory(metadata_options) as ydl:
            info = ydl.extract_info(target.canonical_url, download=False)
        metadata = _metadata_from_info(info, target, max_duration_seconds)
    except IngestError:
        raise
    except Exception as exc:
        code = (
            "youtube_blocked_or_login_required"
            if _is_blocked_error(exc)
            else "youtube_metadata_failed"
        )
        raise IngestError(code, "YouTube metadata could not be retrieved") from exc

    download_options: dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "outtmpl": str(job_directory / "download.%(ext)s"),
        "progress_hooks": [_download_progress_hook(max_source_bytes)],
    }
    try:
        with factory(download_options) as ydl:
            ydl.download([target.canonical_url])
    except IngestError:
        raise
    except Exception as exc:
        code = (
            "youtube_blocked_or_login_required"
            if _is_blocked_error(exc)
            else "youtube_download_failed"
        )
        raise IngestError(code, "YouTube audio could not be downloaded") from exc

    candidate = _safe_download_candidate(job_directory)
    try:
        byte_size = candidate.stat().st_size
    except OSError as exc:
        raise IngestError(
            "youtube_download_failed", "the downloaded source could not be inspected"
        ) from exc
    if byte_size <= 0:
        raise IngestError("youtube_download_failed", "YouTube produced an empty source file")
    if byte_size > max_source_bytes:
        raise IngestError("source_size_exceeded", "the downloaded source exceeds the byte limit")
    return metadata, candidate


async def _run_media_command(
    arguments: tuple[str, ...], *, error_code: str, timeout_seconds: int
) -> tuple[bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise IngestError(error_code, "the media command exceeded its time limit") from exc
    except OSError as exc:
        raise IngestError(error_code, "the media command could not be started") from exc
    if process.returncode != 0:
        raise IngestError(error_code, "the media command rejected the source")
    return stdout, stderr


def _safe_local_file(path: Path, *, root: Path) -> Path:
    candidate = Path(path)
    resolved_root = root.resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise IngestError("ffprobe_failed", "the source audio file is not a regular file")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise IngestError("ffprobe_failed", "the source audio file escaped its temp directory")
    return resolved


async def probe_audio(
    path: Path, *, root: Path, max_duration_seconds: int, timeout_seconds: int
) -> AudioProbe:
    """Use ffprobe to require finite duration and at least one audio stream."""

    safe_path = _safe_local_file(path, root=root)
    arguments = (
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(safe_path),
    )
    stdout, _ = await _run_media_command(
        arguments, error_code="ffprobe_failed", timeout_seconds=timeout_seconds
    )
    try:
        payload = json.loads(stdout)
        duration = _duration(payload.get("format", {}).get("duration"))
        streams = payload.get("streams", [])
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError, IngestError) as exc:
        raise IngestError("ffprobe_failed", "ffprobe returned invalid audio metadata") from exc
    if not isinstance(streams, list) or not any(
        isinstance(stream, Mapping) and stream.get("codec_type") == "audio" for stream in streams
    ):
        raise IngestError("ffprobe_failed", "the source does not contain an audio stream")
    if duration > max_duration_seconds:
        raise IngestError("youtube_duration_exceeded", "the source audio is longer than allowed")
    return AudioProbe(duration)


async def normalize_audio(
    input_path: Path,
    output_part_path: Path,
    *,
    root: Path,
    timeout_seconds: int,
) -> None:
    """Normalize one controlled input to the v1 canonical MP3 format."""

    safe_input = _safe_local_file(input_path, root=root)
    resolved_root = root.resolve()
    output = Path(output_part_path)
    if output.is_symlink() or not output.resolve().parent.is_relative_to(resolved_root):
        raise IngestError("ffmpeg_failed", "the canonical output escaped its temp directory")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    arguments = (
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(safe_input),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output),
    )
    await _run_media_command(arguments, error_code="ffmpeg_failed", timeout_seconds=timeout_seconds)
    if output.is_symlink() or not output.is_file():
        raise IngestError("ffmpeg_failed", "ffmpeg did not produce a canonical source")


def _duration_tolerance(expected_seconds: float) -> float:
    return min(DURATION_TOLERANCE_MAX_SECONDS, max(1.0, expected_seconds * 0.01))


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                byte_size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise IngestError(
            "prepared_source_invalid", "the prepared source could not be read"
        ) from exc
    return byte_size, digest.hexdigest()


async def prepare_source(
    url: str,
    job_directory: Path,
    *,
    max_duration_seconds: int,
    max_source_bytes: int,
    command_timeout_seconds: int,
    youtube_dl_factory: YTDLPFactory | None = None,
) -> PreparedSource:
    """Download, probe, normalize, verify, and checksum one cover source."""

    metadata, downloaded = await asyncio.to_thread(
        download_youtube,
        url,
        job_directory,
        max_duration_seconds=max_duration_seconds,
        max_source_bytes=max_source_bytes,
        youtube_dl_factory=youtube_dl_factory,
    )
    try:
        await probe_audio(
            downloaded,
            root=job_directory,
            max_duration_seconds=max_duration_seconds,
            timeout_seconds=command_timeout_seconds,
        )
        part_path = job_directory / "source.mp3.part"
        final_path = job_directory / "source.mp3"
        await normalize_audio(
            downloaded,
            part_path,
            root=job_directory,
            timeout_seconds=command_timeout_seconds,
        )
        os.replace(part_path, final_path)
        canonical_probe = await probe_audio(
            final_path,
            root=job_directory,
            max_duration_seconds=max_duration_seconds,
            timeout_seconds=command_timeout_seconds,
        )
        if abs(canonical_probe.duration_seconds - metadata.duration_seconds) > _duration_tolerance(
            metadata.duration_seconds
        ):
            raise IngestError(
                "prepared_source_invalid",
                "the prepared source duration does not match YouTube metadata",
            )
        byte_size, digest = sha256_file(final_path)
        if byte_size <= 0:
            raise IngestError("prepared_source_invalid", "the prepared source is empty")
        if byte_size > max_source_bytes:
            raise IngestError("source_size_exceeded", "the prepared source exceeds the byte limit")
        return PreparedSource(metadata, final_path, byte_size, digest)
    except IngestError:
        raise
    except (OSError, ValueError) as exc:
        raise IngestError(
            "prepared_source_invalid", "the prepared source could not be finalized"
        ) from exc


def cleanup_job_directory(job_directory: Path, *, retain: bool) -> None:
    """Delete per-job artifacts unless explicit debug retention is enabled."""

    if retain:
        return
    shutil.rmtree(job_directory, ignore_errors=True)
