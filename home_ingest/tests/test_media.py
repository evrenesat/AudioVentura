from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ace_home_ingest.media import (
    IngestError,
    download_youtube,
    normalize_audio,
    prepare_source,
    probe_audio,
    validate_youtube_url,
)


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://music.youtube.com/watch?v=abc123&t=10",
        "https://m.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://www.youtube.com/shorts/abc123",
    ],
)
def test_youtube_url_allowlist(url: str) -> None:
    target = validate_youtube_url(url)
    assert target.video_id == "abc123"
    assert target.canonical_url == "https://www.youtube.com/watch?v=abc123"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=abc123",
        "https://www.youtube.com/watch?v=abc123&list=playlist",
        "https://www.youtube.com/playlist?list=playlist",
        "https://www.youtube.com/watch?v=abc123#fragment",
        "https://user:password@www.youtube.com/watch?v=abc123",
        "https://www.youtube.com:443/watch?v=abc123",
        "https://example.com/watch?v=abc123",
        "https://youtu.be/abc123/extra",
        "https://www.youtube.com/@channel",
    ],
)
def test_youtube_url_rejects_redirectors_and_playlists(url: str) -> None:
    with pytest.raises(IngestError, match="approved single-video") as error:
        validate_youtube_url(url)
    assert error.value.code == "youtube_url_rejected"


class FakeYoutubeDL:
    instances: list[FakeYoutubeDL] = []
    info = {
        "id": "abc123",
        "title": "  Example\nTitle\x00 ",
        "duration": 234.5,
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
    }

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        self.instances.append(self)

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        assert url == "https://www.youtube.com/watch?v=abc123"
        assert download is False
        return self.info

    def download(self, urls: list[str]) -> None:
        assert urls == ["https://www.youtube.com/watch?v=abc123"]
        output = Path(str(self.options["outtmpl"]).replace("%(ext)s", "webm"))
        output.write_bytes(b"downloaded")
        hooks = self.options["progress_hooks"]
        assert isinstance(hooks, list)
        hooks[0]({"downloaded_bytes": output.stat().st_size, "total_bytes": 7})


def test_download_uses_metadata_first_and_controlled_filename(tmp_path: Path) -> None:
    FakeYoutubeDL.instances = []
    metadata, path = download_youtube(
        "https://www.youtube.com/watch?v=abc123",
        tmp_path / "job",
        max_duration_seconds=600,
        max_source_bytes=100,
        youtube_dl_factory=FakeYoutubeDL,
    )

    assert metadata.title == "Example Title"
    assert path.name == "download.webm"
    assert FakeYoutubeDL.instances[0].options["skip_download"] is True
    assert FakeYoutubeDL.instances[1].options["format"] == "bestaudio/best"
    assert FakeYoutubeDL.instances[1].options["noplaylist"] is True
    assert "cookiefile" not in FakeYoutubeDL.instances[1].options


def test_download_progress_hook_enforces_byte_cap(tmp_path: Path) -> None:
    class TooLarge(FakeYoutubeDL):
        def download(self, urls: list[str]) -> None:
            hooks = self.options["progress_hooks"]
            assert isinstance(hooks, list)
            hooks[0]({"downloaded_bytes": 101})

    with pytest.raises(IngestError) as error:
        download_youtube(
            "https://www.youtube.com/watch?v=abc123",
            tmp_path / "job",
            max_duration_seconds=600,
            max_source_bytes=100,
            youtube_dl_factory=TooLarge,
        )
    assert error.value.code == "source_size_exceeded"


def test_download_rejects_duration_before_download(tmp_path: Path) -> None:
    class LongVideo(FakeYoutubeDL):
        info = {**FakeYoutubeDL.info, "duration": 601}

        def download(self, urls: list[str]) -> None:
            raise AssertionError("download must not start")

    with pytest.raises(IngestError) as error:
        download_youtube(
            "https://www.youtube.com/watch?v=abc123",
            tmp_path / "job",
            max_duration_seconds=600,
            max_source_bytes=100,
            youtube_dl_factory=LongVideo,
        )
    assert error.value.code == "youtube_duration_exceeded"


class FakeProcess:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        return None


def test_ffprobe_and_ffmpeg_use_expected_argument_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "download.webm"
    source.write_bytes(b"source")
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> FakeProcess:
        calls.append(args)
        if args[0] == "ffprobe":
            return FakeProcess(
                json.dumps(
                    {"format": {"duration": "12.0"}, "streams": [{"codec_type": "audio"}]}
                ).encode()
            )
        Path(args[-1]).write_bytes(b"canonical")
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    assert (
        run(
            probe_audio(source, root=tmp_path, max_duration_seconds=600, timeout_seconds=10)
        ).duration_seconds
        == 12
    )
    run(normalize_audio(source, tmp_path / "source.mp3.part", root=tmp_path, timeout_seconds=10))

    assert calls[0][:7] == (
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
    )
    assert calls[1] == (
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
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
        str(tmp_path / "source.mp3.part"),
    )


def test_ffprobe_failure_has_stable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")

    async def failing_exec(*args: str, **kwargs: object) -> FakeProcess:
        return FakeProcess(returncode=1)

    monkeypatch.setattr("asyncio.create_subprocess_exec", failing_exec)
    with pytest.raises(IngestError) as error:
        run(probe_audio(source, root=tmp_path, max_duration_seconds=600, timeout_seconds=10))
    assert error.value.code == "ffprobe_failed"


def test_prepare_source_verifies_canonical_duration_and_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> FakeProcess:
        calls.append(args)
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"canonical-mp3")
            return FakeProcess()
        return FakeProcess(
            json.dumps(
                {"format": {"duration": "234.7"}, "streams": [{"codec_type": "audio"}]}
            ).encode()
        )

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    prepared = run(
        prepare_source(
            "https://www.youtube.com/watch?v=abc123",
            tmp_path / "job",
            max_duration_seconds=600,
            max_source_bytes=100,
            command_timeout_seconds=10,
            youtube_dl_factory=FakeYoutubeDL,
        )
    )
    assert prepared.path.name == "source.mp3"
    assert prepared.byte_size == len(b"canonical-mp3")
    assert prepared.path.read_bytes() == b"canonical-mp3"
    assert len(calls) == 3
