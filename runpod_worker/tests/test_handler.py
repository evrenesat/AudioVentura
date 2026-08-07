from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runpod_worker import handler as handler_module
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


def _payload(*, cover: bool = False, instrumental: bool = False) -> dict[str, Any]:
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
                "output_format": "mp3",
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
        output = Path(save_dir) / "generated.mp3"
        output.write_bytes(b"generated mp3")
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
    assert result["status"] == "uploaded"
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
