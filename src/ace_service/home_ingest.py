"""Bounded controller client for the private home-ingest service."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

import httpx

from ace_service.config import ServiceSettings

_MAX_RESPONSE_BYTES = 64 * 1024
LOGGER = logging.getLogger(__name__)


class HomeIngestError(RuntimeError):
    """A stable, non-secret failure returned by or encountered at home."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PreparedCoverSource:
    """Metadata returned after home has uploaded the canonical source."""

    job_id: str
    video_id: str
    title: str
    canonical_url: str
    duration_seconds: float
    prepared_format: str
    prepared_bytes: int
    prepared_sha256: str

    @classmethod
    def from_mapping(cls, value: Any) -> PreparedCoverSource:
        if not isinstance(value, dict):
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned an invalid response"
            )
        try:
            job_id = str(UUID(str(value["job_id"])))
            video_id = value["video_id"]
            title = value["title"]
            canonical_url = value["canonical_url"]
            duration_seconds = value["duration_seconds"]
            prepared_format = value["prepared_format"]
            prepared_bytes = value["prepared_bytes"]
            prepared_sha256 = value["prepared_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned incomplete source metadata"
            ) from exc
        if (
            not isinstance(video_id, str)
            or not video_id
            or len(video_id) > 128
            or not isinstance(title, str)
            or not title
            or len(title) > 512
            or not isinstance(canonical_url, str)
            or not canonical_url
            or len(canonical_url) > 2048
            or isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or duration_seconds <= 0
            or isinstance(prepared_bytes, bool)
            or not isinstance(prepared_bytes, int)
            or prepared_bytes <= 0
            or prepared_format != "mp3"
            or not isinstance(prepared_sha256, str)
            or len(prepared_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in prepared_sha256)
        ):
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned invalid source metadata"
            )
        return cls(
            job_id=job_id,
            video_id=video_id,
            title=title,
            canonical_url=canonical_url,
            duration_seconds=float(duration_seconds),
            prepared_format=prepared_format,
            prepared_bytes=prepared_bytes,
            prepared_sha256=prepared_sha256.lower(),
        )


@dataclass(frozen=True, slots=True)
class PreparedSourceAsset:
    """Verified metadata returned by the v2 source operation."""

    source_asset_id: str
    title: str
    duration_seconds: float
    prepared_bytes: int
    prepared_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedClip:
    """Verified metadata returned by the v2 clip operation."""

    job_id: str
    duration_seconds: float
    prepared_bytes: int
    prepared_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedDerivative:
    """Verified metadata returned by the v2 playback-derivative operation."""

    derivative_task_id: str
    duration_seconds: float
    prepared_bytes: int
    prepared_sha256: str


def _bounded_hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise HomeIngestError("home_ingest_invalid_response", "home returned an invalid checksum")
    return value.lower()


def _positive_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise HomeIngestError("home_ingest_invalid_response", "home returned an invalid duration")
    result = float(value)
    if not math.isfinite(result):
        raise HomeIngestError("home_ingest_invalid_response", "home returned an invalid duration")
    return result


def _positive_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HomeIngestError("home_ingest_invalid_response", "home returned invalid byte metadata")
    return cast(int, value)


class HomeIngestService(Protocol):
    async def prepare(
        self,
        *,
        job_id: str,
        url: str,
        max_duration_seconds: int,
        max_source_bytes: int,
    ) -> PreparedCoverSource: ...


class HomeIngestClient:
    """Use the private bearer-authenticated home endpoint over Tailscale."""

    def __init__(
        self,
        settings: ServiceSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        timeout = httpx.Timeout(
            connect=5,
            read=max(30, settings.runpod_job_timeout_seconds),
            write=30,
            pool=5,
        )
        self._client = http_client or httpx.AsyncClient(
            base_url=f"{settings.home_ingest_base_url.rstrip('/')}/",
            headers={
                "Authorization": f"Bearer {settings.home_ingest_token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        if http_client is not None:
            http_client.headers.update(
                {
                    "Authorization": f"Bearer {settings.home_ingest_token}",
                    "Accept": "application/json",
                }
            )

    async def __aenter__(self) -> HomeIngestClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> None:
        """Verify that the private home service is reachable and responsive."""

        started = time.monotonic()
        try:
            response = await self._client.get("healthz")
        except httpx.HTTPError as exc:
            LOGGER.warning(
                "stage=health error_code=home_ingest_unavailable exception_class=%s elapsed_ms=%d",
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
                extra={"component": "controller"},
            )
            raise HomeIngestError(
                "home_ingest_unavailable", "the home ingest service could not be reached"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            LOGGER.warning(
                "stage=health error_code=home_ingest_unavailable status=%d elapsed_ms=%d",
                response.status_code,
                int((time.monotonic() - started) * 1000),
                extra={"component": "controller"},
            )
            raise HomeIngestError("home_ingest_unavailable", "the home ingest service is not ready")
        LOGGER.info(
            "stage=health component=controller elapsed_ms=%d",
            int((time.monotonic() - started) * 1000),
            extra={"component": "controller"},
        )

    async def prepare(
        self,
        *,
        job_id: str,
        url: str,
        max_duration_seconds: int,
        max_source_bytes: int,
    ) -> PreparedCoverSource:
        started = time.monotonic()
        payload = {
            "job_id": job_id,
            "url": url,
            "max_duration_seconds": max_duration_seconds,
            "max_source_bytes": max_source_bytes,
        }
        try:
            response = await self._client.post("v1/prepare-youtube-cover", json=payload)
        except httpx.HTTPError as exc:
            LOGGER.warning(
                "job=%s stage=prepare error_code=home_ingest_unavailable exception_class=%s "
                "elapsed_ms=%d",
                job_id,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
                extra={"component": "controller"},
            )
            raise HomeIngestError(
                "home_ingest_unavailable", "the home ingest service could not be reached"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned an oversized response"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise _error_from_response(response)
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned non-JSON source metadata"
            ) from exc
        result = PreparedCoverSource.from_mapping(body)
        if result.job_id != str(job_id):
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned metadata for another job"
            )
        LOGGER.info(
            "job=%s stage=prepare component=controller video_id=%s bytes=%d elapsed_ms=%d",
            job_id,
            result.video_id,
            result.prepared_bytes,
            int((time.monotonic() - started) * 1000),
            extra={"component": "controller"},
        )
        return result

    async def _post_v2(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call one v2 operation without exposing capability URLs in logs."""

        try:
            response = await self._client.post(path, json=dict(payload))
        except httpx.HTTPError as exc:
            raise HomeIngestError(
                "home_ingest_unavailable", "the home ingest service could not be reached"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned an oversized response"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise _error_from_response(response)
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned non-JSON metadata"
            ) from exc
        if not isinstance(body, Mapping):
            raise HomeIngestError("home_ingest_invalid_response", "home returned invalid metadata")
        return body

    async def prepare_source_v2(
        self,
        *,
        source_asset_id: str,
        origin: str,
        display_title: str,
        canonical_upload_url: str,
        youtube_url: str | None = None,
        raw_download_url: str | None = None,
        raw_byte_size: int | None = None,
        raw_sha256: str | None = None,
        max_raw_bytes: int,
        max_canonical_bytes: int,
    ) -> PreparedSourceAsset:
        body = await self._post_v2(
            "v2/prepare-source",
            {
                "source_asset_id": source_asset_id,
                "origin": origin,
                "display_title": display_title,
                "youtube_url": youtube_url,
                "raw_download_url": raw_download_url,
                "raw_byte_size": raw_byte_size,
                "raw_sha256": raw_sha256,
                "canonical_upload_url": canonical_upload_url,
                "max_raw_bytes": max_raw_bytes,
                "max_canonical_bytes": max_canonical_bytes,
            },
        )
        try:
            returned_id = str(UUID(str(body["source_asset_id"])))
            title = body["title"]
            byte_size = _positive_bytes(body["prepared_bytes"])
            digest = _bounded_hash(body["prepared_sha256"])
            duration = _positive_float(body["duration_seconds"])
            prepared_format = body["prepared_format"]
        except (KeyError, TypeError, ValueError) as exc:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned incomplete source metadata"
            ) from exc
        if (
            returned_id != str(UUID(str(source_asset_id)))
            or not isinstance(title, str)
            or not title
            or len(title) > 300
            or prepared_format != "mp3"
        ):
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned invalid source metadata"
            )
        return PreparedSourceAsset(returned_id, title, duration, byte_size, digest)

    async def prepare_clip_v2(
        self,
        *,
        job_id: str,
        source_download_url: str,
        source_byte_size: int,
        source_sha256: str,
        clip_upload_url: str,
        start_seconds: float,
        end_seconds: float,
        max_source_bytes: int,
    ) -> PreparedClip:
        body = await self._post_v2(
            "v2/prepare-clip",
            {
                "job_id": job_id,
                "source_download_url": source_download_url,
                "source_byte_size": source_byte_size,
                "source_sha256": source_sha256,
                "clip_upload_url": clip_upload_url,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "max_source_bytes": max_source_bytes,
            },
        )
        try:
            returned_id = str(UUID(str(body["job_id"])))
            duration = _positive_float(body["duration_seconds"])
            byte_size = _positive_bytes(body["prepared_bytes"])
            digest = _bounded_hash(body["prepared_sha256"])
            prepared_format = body["prepared_format"]
        except (KeyError, TypeError, ValueError) as exc:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned incomplete clip metadata"
            ) from exc
        if returned_id != str(UUID(str(job_id))) or prepared_format != "mp3":
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned invalid clip metadata"
            )
        return PreparedClip(returned_id, duration, byte_size, digest)

    async def prepare_derivative_v2(
        self,
        *,
        derivative_task_id: str,
        source_download_url: str,
        source_byte_size: int,
        source_sha256: str,
        source_format: str,
        derivative_upload_url: str,
        max_source_bytes: int,
        max_canonical_bytes: int,
        expected_duration_seconds: float | None,
    ) -> PreparedDerivative:
        body = await self._post_v2(
            "v2/prepare-playback-derivative",
            {
                "derivative_task_id": derivative_task_id,
                "source_download_url": source_download_url,
                "source_byte_size": source_byte_size,
                "source_sha256": source_sha256,
                "source_format": source_format,
                "derivative_upload_url": derivative_upload_url,
                "max_source_bytes": max_source_bytes,
                "max_canonical_bytes": max_canonical_bytes,
                "expected_duration_seconds": expected_duration_seconds,
            },
        )
        try:
            returned_id = str(UUID(str(body["derivative_task_id"])))
            duration = _positive_float(body["duration_seconds"])
            byte_size = _positive_bytes(body["prepared_bytes"])
            digest = _bounded_hash(body["prepared_sha256"])
            prepared_format = body["prepared_format"]
        except (KeyError, TypeError, ValueError) as exc:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned incomplete derivative metadata"
            ) from exc
        if returned_id != str(UUID(str(derivative_task_id))) or prepared_format != "mp3":
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned invalid derivative metadata"
            )
        return PreparedDerivative(returned_id, duration, byte_size, digest)


def _error_from_response(response: httpx.Response) -> HomeIngestError:
    default_code = (
        "home_ingest_unavailable" if response.status_code >= 500 else "home_ingest_failed"
    )
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return HomeIngestError(default_code, "home ingest rejected the cover request")
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message")
        if isinstance(code, str) and code and isinstance(message, str) and message:
            return HomeIngestError(code, message)
    return HomeIngestError(default_code, "home ingest rejected the cover request")
