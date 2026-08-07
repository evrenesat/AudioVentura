from __future__ import annotations

import json
import wave
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runpod_worker import handler as handler_module
from runpod_worker.audio_output import AudioOutputError
from runpod_worker.runtime import WorkerRuntime
from runpod_worker.schemas import SourceInput
from runpod_worker.transfer_client import TransferError, UploadedOutput

JOB_ID = "11111111-1111-4111-8111-111111111111"
NONCE = "22222222-2222-4222-8222-222222222222"
SOURCE_BODY = b"prepared source"


class FakeParams:
    def __init__(self, **values: Any) -> None:
        self.values = values


class FakeConfig:
    def __init__(self, **values: Any) -> None:
        self.values = values


class FakeTransferClient:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.downloaded_path: Path | None = None
        self.uploaded_path: Path | None = None

    def download_source(self, _source: SourceInput, destination: Path) -> SimpleNamespace:
        self.downloaded_path = destination
        destination.write_bytes(SOURCE_BODY)
        return SimpleNamespace(path=destination, bytes=len(SOURCE_BODY), sha256="ignored")

    def upload_output(self, _upload: Any, source_path: Path) -> UploadedOutput:
        self.uploaded_path = source_path
        if self.fail_upload:
            raise TransferError("output upload failed")
        return UploadedOutput(bytes=source_path.stat().st_size, sha256="a" * 64, status_code=201)


def _payload(
    *, cover: bool = False, instrumental: bool = False, output_format: str = "mp3"
) -> dict[str, Any]:
    source = None
    if cover:
        from hashlib import sha256

        source = {
            "url": "https://transfer.example.test/transfer/v1/source/capability",
            "sha256": sha256(SOURCE_BODY).hexdigest(),
            "bytes": len(SOURCE_BODY),
            "format": "mp3",
        }
    return {
        "input": {
            "schema_version": 1,
            "job_id": JOB_ID,
            "submission_nonce": NONCE,
            "variation_index": 1,
            "task_type": "cover" if cover else "original",
            "generation": {
                "prompt": "warm analog synth",
                "lyrics": "" if instrumental else "[verse] hello",
                "instrumental": instrumental,
                "vocal_language": "en",
                "duration": 20,
                "output_format": output_format,
                "seed": 17,
                **({"cover_strength": 0.75} if cover else {}),
            },
            "source": source,
            "result_upload": {
                "url": "https://transfer.example.test/transfer/v1/output/capability",
                "max_bytes": 1024,
            },
        }
    }


def _runtime(
    transfer_client: FakeTransferClient,
    generated_paths: list[Path],
    calls: list[tuple[FakeParams, FakeConfig]],
    writer: Callable[[Path, str], None] | None = None,
) -> WorkerRuntime:
    def generate_music(
        _dit: object,
        _llm: object,
        params: FakeParams,
        config: FakeConfig,
        *,
        save_dir: str,
    ) -> SimpleNamespace:
        calls.append((params, config))
        output_format = config.values["audio_format"]
        output = Path(save_dir) / f"generated.{output_format}"
        if writer is None:
            if output_format == "wav":
                with wave.open(str(output), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(48_000)
                    wav_file.writeframes(b"\x00\x00" * 4_800)
            else:
                output.write_bytes(f"generated {output_format}".encode())
        else:
            writer(output, output_format)
        generated_paths.append(output)
        return SimpleNamespace(
            success=True,
            audios=[
                {
                    "path": str(output),
                    "sample_rate": 48_000,
                    "params": {"seed": 17},
                }
            ],
        )

    return WorkerRuntime(
        dit_handler=object(),
        llm_handler=object(),
        generation_params_type=FakeParams,
        generation_config_type=FakeConfig,
        generate_music=generate_music,
        gpu_name="test-gpu",
        gpu_vram_bytes=24 * 1024**3,
        transfer_client_factory=lambda: transfer_client,
    )


def test_original_mapping_forces_one_output_and_returns_small_metadata() -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))

    result = handler_module.handler(_payload(instrumental=True))
    params, config = calls[0]

    assert params.values["task_type"] == "text2music"
    assert params.values["lyrics"] == ""
    assert "Instrumental only; no vocals." in params.values["caption"]
    assert config.values["batch_size"] == 1
    assert config.values["audio_format"] == "wav"
    assert result["status"] == "uploaded"
    assert result["output"]["format"] == "mp3"
    assert result["output"]["mime_type"] == "audio/mpeg"
    assert "path" not in json.dumps(result)
    assert len(json.dumps(result)) < 4096
    assert not generated_paths[0].exists()


def test_cover_downloads_source_maps_local_path_and_cleans_everything() -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))

    result = handler_module.handler(_payload(cover=True))
    params, _config = calls[0]

    assert params.values["task_type"] == "cover"
    assert params.values["src_audio"] == str(transfer_client.downloaded_path)
    assert params.values["audio_cover_strength"] == 0.75
    assert transfer_client.downloaded_path is not None
    assert not transfer_client.downloaded_path.exists()
    assert transfer_client.uploaded_path is not None
    assert not transfer_client.uploaded_path.exists()
    assert result["output"]["bytes"] > 0


def test_failed_upload_propagates_and_generated_file_is_cleaned() -> None:
    transfer_client = FakeTransferClient(fail_upload=True)
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))

    with pytest.raises(TransferError, match="upload"):
        handler_module.handler(_payload())

    assert generated_paths
    assert not generated_paths[0].exists()
    assert transfer_client.uploaded_path is not None
    assert transfer_client.uploaded_path.suffix == ".mp3"
    assert not transfer_client.uploaded_path.exists()
    assert not transfer_client.uploaded_path.with_name(
        f"{transfer_client.uploaded_path.name}.part"
    ).exists()


@pytest.mark.parametrize(
    ("output_format", "internal_format"),
    [("mp3", "wav"), ("flac", "flac"), ("wav", "wav")],
)
def test_requested_format_selects_internal_format_and_upload_extension(
    output_format: str, internal_format: str
) -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))

    handler_module.handler(_payload(output_format=output_format))

    _params, config = calls[0]
    assert config.values["audio_format"] == internal_format
    assert transfer_client.uploaded_path is not None
    assert transfer_client.uploaded_path.suffix == f".{output_format}"


@pytest.mark.parametrize("case", ["malformed", "empty", "non_pcm", "wrong_rate", "wrong_width"])
def test_invalid_generated_wav_is_rejected_without_upload(case: str) -> None:
    def write_invalid_wav(path: Path, _format: str) -> None:
        if case == "malformed":
            path.write_bytes(b"not a wav")
            return
        if case == "empty":
            path.write_bytes(b"")
            return
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(1 if case == "wrong_width" else 2)
            wav_file.setframerate(44_100 if case == "wrong_rate" else 48_000)
            wav_file.writeframes(b"\x00" * 4_800)
        if case == "non_pcm":
            contents = bytearray(path.read_bytes())
            fmt_offset = contents.index(b"fmt ")
            contents[fmt_offset + 8 : fmt_offset + 10] = (3).to_bytes(2, "little")
            path.write_bytes(contents)

    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(
        _runtime(transfer_client, generated_paths, calls, writer=write_invalid_wav)
    )

    with pytest.raises(AudioOutputError, match="WAV"):
        handler_module.handler(_payload())

    assert transfer_client.uploaded_path is None
