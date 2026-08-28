"""YouTube validation, bounded download, and canonical audio preparation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    """A verified local canonical MP3 ready for a signed transfer upload."""

    title: str
    duration_seconds: float
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
    info: Any, target: YouTubeTarget, max_duration_seconds: int | None
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
    if max_duration_seconds is not None and duration_seconds > max_duration_seconds:
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
    max_duration_seconds: int | None,
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
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError as exc:
        if process is not None:
            process.kill()
            await process.wait()
        LOGGER.warning(
            "stage=media error_code=%s exception_class=%s",
            error_code,
            type(exc).__name__,
            extra={"component": "home_ingest"},
        )
        raise IngestError(error_code, "the media command exceeded its time limit") from exc
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except OSError as exc:
        LOGGER.warning(
            "stage=media error_code=%s exception_class=%s",
            error_code,
            type(exc).__name__,
            extra={"component": "home_ingest"},
        )
        raise IngestError(error_code, "the media command could not be started") from exc
    if process.returncode != 0:
        LOGGER.warning(
            "stage=media error_code=%s exception_class=CalledProcessError",
            error_code,
            extra={"component": "home_ingest"},
        )
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
    path: Path,
    *,
    root: Path,
    max_duration_seconds: int | None,
    timeout_seconds: int,
    secure_input: bool = False,
) -> AudioProbe:
    """Use ffprobe to require finite duration and at least one audio stream."""

    safe_path = _safe_local_file(path, root=root)
    arguments_list = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
    ]
    if secure_input:
        arguments_list.extend(("-protocol_whitelist", "file,pipe,fd"))
    arguments_list.append(str(safe_path))
    arguments = tuple(arguments_list)
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
    if max_duration_seconds is not None and duration > max_duration_seconds:
        raise IngestError("youtube_duration_exceeded", "the source audio is longer than allowed")
    return AudioProbe(duration)


async def normalize_audio(
    input_path: Path,
    output_part_path: Path,
    *,
    root: Path,
    timeout_seconds: int,
    secure_input: bool = False,
) -> None:
    """Normalize one controlled input to the v1 canonical MP3 format."""

    safe_input = _safe_local_file(input_path, root=root)
    resolved_root = root.resolve()
    output = Path(output_part_path)
    if output.is_symlink() or not output.resolve().parent.is_relative_to(resolved_root):
        raise IngestError("ffmpeg_failed", "the canonical output escaped its temp directory")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    arguments_list = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if secure_input:
        arguments_list.extend(("-protocol_whitelist", "file,pipe,fd"))
    arguments_list.extend(("-y", "-i", str(safe_input)))
    if secure_input:
        arguments_list.extend(("-map", "0:a:0"))
    arguments_list.extend(
        (
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
        )
    )
    if secure_input:
        arguments_list.extend(("-map_metadata", "-1"))
    arguments_list.extend(("-f", "mp3", str(output)))
    arguments = tuple(arguments_list)
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
    max_duration_seconds: int | None,
    max_source_bytes: int,
    command_timeout_seconds: int,
    youtube_dl_factory: YTDLPFactory | None = None,
    secure_input: bool = False,
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
            secure_input=secure_input,
        )
        part_path = job_directory / "source.mp3.part"
        final_path = job_directory / "source.mp3"
        await normalize_audio(
            downloaded,
            part_path,
            root=job_directory,
            timeout_seconds=command_timeout_seconds,
            secure_input=secure_input,
        )
        os.replace(part_path, final_path)
        canonical_probe = await probe_audio(
            final_path,
            root=job_directory,
            max_duration_seconds=max_duration_seconds,
            timeout_seconds=command_timeout_seconds,
            secure_input=secure_input,
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


def _safe_work_path(path: Path, root: Path, *, label: str) -> Path:
    candidate = Path(path)
    resolved_root = root.resolve()
    if candidate.is_symlink() or not candidate.resolve().parent.is_relative_to(resolved_root):
        raise IngestError("prepared_source_invalid", f"the {label} path is unsafe")
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate.parent.chmod(0o700)
    return candidate


async def prepare_local_source(
    input_path: Path,
    work_directory: Path,
    *,
    title: str,
    max_canonical_bytes: int,
    command_timeout_seconds: int,
) -> PreparedMedia:
    """Probe any local audio-containing media and make the canonical MP3."""

    work_directory = _safe_work_path(work_directory, work_directory.parent, label="work directory")
    if input_path.is_symlink() or not input_path.is_file():
        raise IngestError("source_download_failed", "the source file is not a regular file")
    await probe_audio(
        input_path,
        root=work_directory,
        max_duration_seconds=None,
        timeout_seconds=command_timeout_seconds,
        secure_input=True,
    )
    part_path = _safe_work_path(
        work_directory / "source.mp3.part", work_directory, label="canonical"
    )
    final_path = _safe_work_path(work_directory / "source.mp3", work_directory, label="canonical")
    if final_path.exists() or final_path.is_symlink():
        final_path.unlink(missing_ok=True)
    await normalize_audio(
        input_path,
        part_path,
        root=work_directory,
        timeout_seconds=command_timeout_seconds,
        secure_input=True,
    )
    try:
        os.replace(part_path, final_path)
        with final_path.open("rb") as output:
            os.fsync(output.fileno())
    except OSError as exc:
        raise IngestError("ffmpeg_failed", "the canonical source could not be finalized") from exc
    measured = await probe_audio(
        final_path,
        root=work_directory,
        max_duration_seconds=None,
        timeout_seconds=command_timeout_seconds,
        secure_input=True,
    )
    byte_size, digest = sha256_file(final_path)
    if byte_size <= 0:
        raise IngestError("prepared_source_invalid", "the canonical source is empty")
    if byte_size > max_canonical_bytes:
        raise IngestError(
            "canonical_source_size_exceeded", "the canonical source exceeds the byte limit"
        )
    clean_title = _sanitize_title(title)
    return PreparedMedia(clean_title, measured.duration_seconds, final_path, byte_size, digest)


async def prepare_clip_local(
    input_path: Path,
    work_directory: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    max_bytes: int,
    command_timeout_seconds: int,
) -> PreparedMedia:
    """Create a precise canonical MP3 clip from a verified source MP3."""

    if (
        isinstance(start_seconds, bool)
        or isinstance(end_seconds, bool)
        or not math.isfinite(float(start_seconds))
        or not math.isfinite(float(end_seconds))
        or start_seconds < 0
        or end_seconds <= start_seconds
    ):
        raise IngestError("invalid_source_range", "the selected source range is invalid")
    safe_input = _safe_local_file(input_path, root=work_directory)
    output_part = _safe_work_path(work_directory / "clip.mp3.part", work_directory, label="clip")
    output_path = _safe_work_path(work_directory / "clip.mp3", work_directory, label="clip")
    if output_path.exists() or output_path.is_symlink():
        output_path.unlink(missing_ok=True)
    duration = float(end_seconds) - float(start_seconds)
    arguments = (
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe,fd",
        "-y",
        "-ss",
        f"{float(start_seconds):.6f}",
        "-i",
        str(safe_input),
        "-t",
        f"{duration:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-map_metadata",
        "-1",
        "-f",
        "mp3",
        str(output_part),
    )
    await _run_media_command(
        arguments, error_code="ffmpeg_failed", timeout_seconds=command_timeout_seconds
    )
    try:
        os.replace(output_part, output_path)
    except OSError as exc:
        raise IngestError("ffmpeg_failed", "the source clip could not be finalized") from exc
    measured = await probe_audio(
        output_path,
        root=work_directory,
        max_duration_seconds=None,
        timeout_seconds=command_timeout_seconds,
        secure_input=True,
    )
    if abs(measured.duration_seconds - duration) > 0.05:
        raise IngestError("clip_duration_mismatch", "the prepared clip duration is not precise")
    byte_size, digest = sha256_file(output_path)
    if byte_size <= 0 or byte_size > max_bytes:
        raise IngestError("source_size_exceeded", "the prepared clip exceeds the byte limit")
    return PreparedMedia("source clip", measured.duration_seconds, output_path, byte_size, digest)


def cleanup_job_directory(job_directory: Path, *, retain: bool) -> None:
    """Delete per-job artifacts unless explicit debug retention is enabled."""

    if retain:
        return
    shutil.rmtree(job_directory, ignore_errors=True)
