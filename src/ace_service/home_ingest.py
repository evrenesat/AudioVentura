"""Bounded controller client for the private home-ingest service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import httpx

from ace_service.config import ServiceSettings

_MAX_RESPONSE_BYTES = 64 * 1024


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

        try:
            response = await self._client.get("healthz")
        except httpx.HTTPError as exc:
            raise HomeIngestError(
                "home_ingest_unavailable", "the home ingest service could not be reached"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise HomeIngestError("home_ingest_unavailable", "the home ingest service is not ready")

    async def prepare(
        self,
        *,
        job_id: str,
        url: str,
        max_duration_seconds: int,
        max_source_bytes: int,
    ) -> PreparedCoverSource:
        payload = {
            "job_id": job_id,
            "url": url,
            "max_duration_seconds": max_duration_seconds,
            "max_source_bytes": max_source_bytes,
        }
        try:
            response = await self._client.post("v1/prepare-youtube-cover", json=payload)
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
                "home_ingest_invalid_response", "home returned non-JSON source metadata"
            ) from exc
        result = PreparedCoverSource.from_mapping(body)
        if result.job_id != str(job_id):
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned metadata for another job"
            )
        return result


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
