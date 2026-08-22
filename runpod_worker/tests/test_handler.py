from __future__ import annotations

import json
import sys
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
TEST_IMAGE_DIGEST = "sha256:" + "a" * 64
TEST_MODEL_REPO = "evrenesat/audioventura-ace-step-v0.1.8"
TEST_MODEL_REVISION = "88b8c7fa089446b53382c1040037492463430bed"
TEST_MODEL_TAG = "av-v0.1.8-bundle-2"
TEST_MANIFEST_SHA256 = "39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc"


class FakeParams:
    def __init__(
        self,
        *,
        task_type: Any,
        caption: Any,
        lyrics: Any,
        instrumental: Any,
        vocal_language: Any,
        duration: Any,
        bpm: Any,
        keyscale: Any,
        timesignature: Any,
        seed: Any,
        inference_steps: Any,
        shift: Any,
        thinking: Any,
        use_cot_metas: Any,
        use_cot_caption: Any,
        use_cot_language: Any,
        src_audio: Any,
        audio_cover_strength: Any,
        cover_noise_strength: Any,
        lm_temperature: Any,
        lm_cfg_scale: Any,
        lm_top_k: Any,
        lm_top_p: Any,
        lm_negative_prompt: Any,
    ) -> None:
        self.values = {
            "task_type": task_type,
            "caption": caption,
            "lyrics": lyrics,
            "instrumental": instrumental,
            "vocal_language": vocal_language,
            "duration": duration,
            "bpm": bpm,
            "keyscale": keyscale,
            "timesignature": timesignature,
            "seed": seed,
            "inference_steps": inference_steps,
            "shift": shift,
            "thinking": thinking,
            "use_cot_metas": use_cot_metas,
            "use_cot_caption": use_cot_caption,
            "use_cot_language": use_cot_language,
            "src_audio": src_audio,
            "audio_cover_strength": audio_cover_strength,
            "cover_noise_strength": cover_noise_strength,
            "lm_temperature": lm_temperature,
            "lm_cfg_scale": lm_cfg_scale,
            "lm_top_k": lm_top_k,
            "lm_top_p": lm_top_p,
            "lm_negative_prompt": lm_negative_prompt,
        }


class FakeConfig:
    def __init__(
        self,
        *,
        batch_size: Any,
        allow_lm_batch: Any,
        use_random_seed: Any,
        seeds: Any,
        audio_format: Any,
        mp3_bitrate: Any,
        mp3_sample_rate: Any,
    ) -> None:
        self.values = {
            "batch_size": batch_size,
            "allow_lm_batch": allow_lm_batch,
            "use_random_seed": use_random_seed,
            "seeds": seeds,
            "audio_format": audio_format,
            "mp3_bitrate": mp3_bitrate,
            "mp3_sample_rate": mp3_sample_rate,
        }


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


def _v2_cover_payload() -> dict[str, Any]:
    resolved = {
        "profile_id": "fast-beta-v1",
        "task_type": "cover",
        "prompt_mode": "direct",
        "duration_mode": "source",
        "duration": 0.1,
        "caption": "warm analog synth",
        "lyrics": "",
        "seed": 17,
        "inference_steps": 8,
        "shift": 1.0,
        "lm_temperature": 0.85,
        "lm_cfg_scale": 2.0,
        "lm_top_k": 0,
        "lm_top_p": 0.9,
        "lm_negative_prompt": "NO USER INPUT",
        "thinking": False,
        "use_cot_metas": False,
        "use_cot_caption": False,
        "use_cot_language": False,
        "audio_cover_strength": 0.75,
        "cover_noise_strength": 0.20,
        "source_duration_seconds": 0.1,
        "target_duration_seconds": 0.1,
    }
    return {
        "input": {
            "schema_version": 2,
            "job_id": JOB_ID,
            "submission_nonce": NONCE,
            "variation_index": 1,
            "task_type": "cover",
            "profile_id": "fast-beta-v1",
            "resolved_parameters": resolved,
            "source_duration_seconds": 0.1,
            "resolved_target_duration_seconds": 0.1,
            "ace_duration_seconds": 0.1,
            "cover_staging": {"status": "confirmed"},
            "generation": {
                "prompt": "warm analog synth",
                "target_style": "warm analog synth",
                "remix_guidance": None,
                "lyrics": "",
                "instrumental": False,
                "vocal_language": "en",
                "prompt_mode": "direct",
                "duration_mode": "source",
                "duration_seconds": 0.1,
                "duration": 0.1,
                "bpm": None,
                "key_scale": None,
                "time_signature": None,
                "seed": 17,
                "output_format": "wav",
                "audio_cover_strength": 0.75,
                "cover_noise_strength": 0.20,
            },
            "source": {
                "url": "https://transfer.example.test/transfer/v1/source/capability",
                "sha256": "a" * 64,
                "bytes": len(SOURCE_BODY),
                "format": "mp3",
            },
            "result_upload": {
                "url": "https://transfer.example.test/transfer/v1/output/capability",
                "max_bytes": 1024,
            },
        }
    }


def _v2_enhance_payload(*, lyrics: str = "[verse] preserve exactly") -> dict[str, Any]:
    resolved = {
        "profile_id": "fast-beta-v1",
        "task_type": "original",
        "prompt_mode": "enhance",
        "duration_mode": "auto",
        "duration": -1.0,
        "caption": "warm analog synth",
        "lyrics": lyrics,
        "seed": 17,
        "inference_steps": 8,
        "shift": 1.0,
        "lm_temperature": 0.85,
        "lm_cfg_scale": 2.0,
        "lm_top_k": 0,
        "lm_top_p": 0.9,
        "lm_negative_prompt": "NO USER INPUT",
        "thinking": False,
        "use_cot_metas": True,
        "use_cot_caption": True,
        "use_cot_language": True,
        "audio_cover_strength": 1.0,
        "cover_noise_strength": 0.0,
    }
    return {
        "input": {
            "schema_version": 2,
            "job_id": JOB_ID,
            "submission_nonce": NONCE,
            "variation_index": 1,
            "task_type": "original",
            "profile_id": "fast-beta-v1",
            "resolved_parameters": resolved,
            "generation": {
                "prompt": "warm analog synth",
                "lyrics": lyrics,
                "instrumental": False,
                "vocal_language": "en",
                "prompt_mode": "enhance",
                "duration_mode": "auto",
                "duration_seconds": None,
                "duration": -1.0,
                "bpm": None,
                "key_scale": None,
                "time_signature": None,
                "seed": 17,
                "output_format": "mp3",
                "audio_cover_strength": 1.0,
                "cover_noise_strength": 0.0,
            },
            "source": None,
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
    *,
    lm_metadata: dict[str, Any] | None = None,
    result_style: str = "attribute",
) -> WorkerRuntime:
    def generate_music(
        _dit: object,
        _llm: object,
        params: FakeParams,
        config: FakeConfig,
        *,
        save_dir: str,
    ) -> Any:
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
        audios = [
            {
                "path": str(output),
                "sample_rate": 48_000,
                "params": {"seed": 17},
                "duration_seconds": 0.1,
            }
        ]
        extra_outputs = {"lm_metadata": lm_metadata} if lm_metadata is not None else {}
        if result_style == "mapping":
            return {
                "success": True,
                "audios": audios,
                "extra_outputs": extra_outputs,
            }
        return SimpleNamespace(
            success=True,
            audios=audios,
            extra_outputs=extra_outputs,
        )

    return WorkerRuntime(
        dit_handler=object(),
        llm_handler=object(),
        generation_params_type=FakeParams,
        generation_config_type=FakeConfig,
        generate_music=generate_music,
        gpu_name="test-gpu",
        gpu_vram_bytes=24 * 1024**3,
        model_repo=TEST_MODEL_REPO,
        model_revision=TEST_MODEL_REVISION,
        model_tag=TEST_MODEL_TAG,
        model_manifest_sha256=TEST_MANIFEST_SHA256,
        worker_image_digest=TEST_IMAGE_DIGEST,
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
    assert len(json.dumps(result)) < 65_536
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


def test_v2_cover_probes_source_duration_and_preserves_independent_controls() -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))

    result = handler_module.handler(_v2_cover_payload())
    params, config = calls[0]

    assert params.values["audio_cover_strength"] == 0.75
    assert params.values["cover_noise_strength"] == 0.20
    assert params.values["duration"] == 0.1
    assert config.values["audio_format"] == "wav"
    assert result["schema_version"] == 2
    assert result["output"]["duration_seconds"] == pytest.approx(0.1)
    assert result["output"]["target_duration_seconds"] == pytest.approx(0.1)
    assert result["output"]["duration_within_tolerance"] is True


@pytest.mark.parametrize("result_style", ["mapping", "attribute"])
def test_v2_enhance_persists_pinned_lm_metadata_without_rewriting_lyrics(
    result_style: str,
) -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(
        _runtime(
            transfer_client,
            generated_paths,
            calls,
            lm_metadata={
                "caption": "LM-generated atmospheric synthwave",
                "lyrics": "planner rewrite must not replace supplied lyrics",
                "bpm": 118,
                "keyscale": "D minor",
                "audio_codes": "<|audio_code_1|>",
                "time_costs": {"total_time": 999},
            },
            result_style=result_style,
        )
    )

    result = handler_module.handler(_v2_enhance_payload())

    assert result["effective"]["caption"] == "LM-generated atmospheric synthwave"
    assert result["effective"]["lyrics"] == "[verse] preserve exactly"
    assert result["generated_metadata"] == {
        "caption": "LM-generated atmospheric synthwave",
        "lyrics": "planner rewrite must not replace supplied lyrics",
        "bpm": 118,
        "keyscale": "D minor",
    }
    assert result["worker"]["image_digest"] == TEST_IMAGE_DIGEST
    assert result["worker"]["model_bundle"] == {
        "repo": TEST_MODEL_REPO,
        "revision": TEST_MODEL_REVISION,
        "tag": TEST_MODEL_TAG,
        "manifest_sha256": TEST_MANIFEST_SHA256,
    }
    encoded = json.dumps(result)
    assert "audio_codes" not in encoded
    assert "time_costs" not in encoded
    assert "tensor" not in encoded


def test_cover_reports_exact_progress_boundaries_with_original_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    reports: list[tuple[object, dict[str, object]]] = []

    class FakeServerless:
        @staticmethod
        def progress_update(event: object, payload: dict[str, object]) -> None:
            reports.append((event, payload))

    monkeypatch.setitem(sys.modules, "runpod", SimpleNamespace(serverless=FakeServerless()))
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))
    event = _payload(cover=True)

    handler_module.handler(event)

    assert all(reported_event is event for reported_event, _ in reports)
    assert [payload for _, payload in reports] == [
        {"kind": "audioventura_progress_v1", "phase": "source_download", "sequence": 10},
        {"kind": "audioventura_progress_v1", "phase": "generation", "sequence": 20},
        {"kind": "audioventura_progress_v1", "phase": "finalizing", "sequence": 30},
        {"kind": "audioventura_progress_v1", "phase": "output_upload", "sequence": 40},
    ]


def test_progress_delivery_failure_does_not_fail_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []

    class FailingServerless:
        @staticmethod
        def progress_update(event: object, payload: dict[str, object]) -> None:
            del event, payload
            raise RuntimeError("provider progress unavailable")

    monkeypatch.setitem(sys.modules, "runpod", SimpleNamespace(serverless=FailingServerless()))
    handler_module.configure_runtime(_runtime(transfer_client, generated_paths, calls))

    result = handler_module.handler(_payload())

    assert result["status"] == "uploaded"


@pytest.mark.parametrize("result_style", ["mapping", "attribute"])
def test_v2_empty_lyrics_use_bounded_pinned_lm_lyrics(result_style: str) -> None:
    transfer_client = FakeTransferClient()
    generated_paths: list[Path] = []
    calls: list[tuple[FakeParams, FakeConfig]] = []
    handler_module.configure_runtime(
        _runtime(
            transfer_client,
            generated_paths,
            calls,
            lm_metadata={
                "caption": "LM-generated atmospheric synthwave",
                "lyrics": "[verse] planner lyrics",
            },
            result_style=result_style,
        )
    )

    result = handler_module.handler(_v2_enhance_payload(lyrics=""))

    assert result["input"]["lyrics"] == ""
    assert result["effective"]["lyrics"] == "[verse] planner lyrics"
    assert result["generated_metadata"]["lyrics"] == "[verse] planner lyrics"


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
