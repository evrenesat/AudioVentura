"""Explicit, non-paid smoke test for the isolated beta mock backend.

Run this only against an already deployed beta.  It requires the controller
Basic Auth values and the private mock health capability through the
environment, plus one caller-approved YouTube URL for the normal Cover path.
The failure output intentionally contains only an exception class; prompts,
source URLs, and credentials never appear in test output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx

MOCK_BACKEND = "mock/midi-sequential"
CORPUS_SHA256 = "41549405bcaeed4783e366f61236db4203c9b5d846fd8e0fee59bcf2658a23b7"
CORPUS_MEMBER_COUNT = 10_855
MAX_OUTPUT_BYTES = 268_435_456
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing protected environment value: {name}")
    return value


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if match is None:
        raise RuntimeError("beta form did not contain a CSRF token")
    return match.group(1)


def _job_id(response: httpx.Response) -> str:
    path = urlparse(str(response.url)).path.rstrip("/")
    job_id = path.rsplit("/", 1)[-1]
    if not UUID_RE.fullmatch(job_id):
        raise RuntimeError("beta submission did not resolve to a job detail page")
    return job_id


def _get(client: httpx.Client, path: str) -> httpx.Response:
    response = client.get(path)
    response.raise_for_status()
    return response


def _submit_original(client: httpx.Client, *, seed: int) -> str:
    form = _get(client, "/beta/create")
    response = client.post(
        "/beta/create",
        data={
            "csrf_token": _csrf(form.text),
            "backend": MOCK_BACKEND,
            "description": "beta sequential MIDI smoke",
            "lyrics": "[verse] smoke path",
            "instrumental": "false",
            "vocal_language": "en",
            "prompt_mode": "enhance",
            "duration_mode": "custom",
            "duration_seconds": "30",
            "bpm": "111",
            "key_scale": "D minor",
            "time_signature": "3",
            "seed": str(seed),
            "variation_count": "2",
            "output_format": "mp3",
        },
    )
    response.raise_for_status()
    return _job_id(response)


def _submit_cover(client: httpx.Client, *, youtube_url: str, seed: int) -> str:
    form = _get(client, "/beta/cover")
    response = client.post(
        "/beta/cover",
        data={
            "csrf_token": _csrf(form.text),
            "backend": MOCK_BACKEND,
            "youtube_url": youtube_url,
            "target_style": "beta sequential MIDI cover smoke",
            "remix_guidance": "keep the arrangement bright",
            "lyrics": "new smoke lyrics",
            "source_style": "the source style",
            "audio_cover_strength": "0.5",
            "cover_noise_strength": "0.2",
            "duration_mode": "custom",
            "duration_seconds": "30",
            "seed": str(seed),
            "variation_count": "1",
            "output_format": "mp3",
        },
    )
    response.raise_for_status()
    return _job_id(response)


def _wait_for_completion(
    client: httpx.Client, job_id: str, *, timeout_seconds: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = _get(client, f"/beta/jobs/{job_id}/status")
        body = response.json()
        state = body.get("status")
        if state == "completed":
            outputs = body.get("outputs")
            if not isinstance(outputs, list) or not outputs:
                raise RuntimeError("completed beta job did not expose outputs")
            return body
        if state in {"failed", "cancelled"}:
            raise RuntimeError("beta smoke job did not complete")
        time.sleep(2)
    raise RuntimeError("beta smoke job timed out")


def _attempts(body: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = body.get("attempts")
    if not isinstance(attempts, list) or not all(isinstance(item, dict) for item in attempts):
        raise RuntimeError("beta status did not expose bounded attempt evidence")
    return attempts


def _assert_mp3_outputs(client: httpx.Client, body: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = body.get("outputs")
    if not isinstance(outputs, list):
        raise RuntimeError("beta status did not expose output records")
    attempt_by_variation = {
        item.get("variation_index"): item
        for item in _attempts(body)
        if item.get("status") == "completed"
    }
    evidence: list[dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict) or output.get("mime_type") != "audio/mpeg":
            raise RuntimeError("beta output was not declared as MP3")
        byte_size = output.get("byte_size")
        media_url = output.get("media_url")
        download_url = output.get("download_url")
        if (
            not isinstance(byte_size, int)
            or not 0 < byte_size <= MAX_OUTPUT_BYTES
            or not isinstance(media_url, str)
            or not isinstance(download_url, str)
        ):
            raise RuntimeError("beta output evidence is incomplete")
        for url in (media_url, download_url):
            ranged = client.get(url, headers={"Range": "bytes=0-0"})
            if ranged.status_code != 206 or len(ranged.content) != 1:
                raise RuntimeError("beta MP3 endpoint did not honor a one-byte range")
        downloaded = client.get(download_url)
        downloaded.raise_for_status()
        if len(downloaded.content) != byte_size or len(downloaded.content) > MAX_OUTPUT_BYTES:
            raise RuntimeError("beta MP3 byte evidence did not match the download")
        if not (downloaded.content.startswith(b"ID3") or downloaded.content[:2] == b"\xff\xfb"):
            raise RuntimeError("beta output did not have an MPEG header")
        variation = output.get("variation_index")
        attempt = attempt_by_variation.get(variation)
        if not isinstance(attempt, dict):
            raise RuntimeError("beta output is missing completed attempt evidence")
        reported_bytes = attempt.get("output_bytes")
        reported_sha256 = attempt.get("output_sha256")
        duration = attempt.get("duration_seconds")
        digest = hashlib.sha256(downloaded.content).hexdigest()
        if reported_bytes != byte_size or reported_sha256 != digest:
            raise RuntimeError("beta output checksum evidence did not match the download")
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise RuntimeError("beta output duration evidence was invalid")
        evidence.append(
            {
                "variation_index": variation,
                "bytes": byte_size,
                "sha256": digest,
                "duration_seconds": duration,
                "corpus_index": attempt.get("corpus_index"),
            }
        )
    return evidence


def _mock_health(base_url: str, token: str) -> dict[str, Any]:
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
        timeout=10,
    ) as client:
        response = client.get("/healthz")
        response.raise_for_status()
        body = response.json()
    if body.get("status") != "ok":
        raise RuntimeError("beta mock health was not ready")
    corpus = body.get("corpus")
    cursor = body.get("cursor")
    if (
        not isinstance(corpus, dict)
        or corpus.get("archive_sha256") != CORPUS_SHA256
        or corpus.get("member_count") != CORPUS_MEMBER_COUNT
        or not isinstance(cursor, dict)
        or not isinstance(cursor.get("last_consumed_index"), int)
    ):
        raise RuntimeError("beta mock health did not expose the expected corpus identity")
    return body


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.youtube_url:
        raise RuntimeError("an approved YouTube URL is required")
    if args.timeout_seconds < 30 or args.timeout_seconds > 3600:
        raise RuntimeError("timeout is outside the bounded smoke-test range")
    username = _required_env("ACE_SERVICE_USERNAME")
    password = _required_env("ACE_SERVICE_PASSWORD")
    mock_token = _required_env("MOCK_TOKEN")
    before = _mock_health(args.mock_base_url, mock_token)
    cursor_before = before["cursor"]["last_consumed_index"]
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        auth=(username, password),
        follow_redirects=True,
        timeout=30,
    ) as client:
        for path in ("/beta/", "/beta/create", "/beta/cover", "/beta/jobs"):
            page = _get(client, path)
            if path in {"/beta/create", "/beta/cover"}:
                if (
                    "Mock · Sequential MIDI" not in page.text
                    or 'name="output_format"' not in page.text
                ):
                    raise RuntimeError("beta form did not expose the selected mock backend")
        original_id = _submit_original(client, seed=args.seed)
        original = _wait_for_completion(client, original_id, timeout_seconds=args.timeout_seconds)
        original_again = _get(client, f"/beta/jobs/{original_id}/status").json()
        original_evidence = _assert_mp3_outputs(client, original)
        if original_again.get("status") != "completed":
            raise RuntimeError("repeated beta status poll changed the terminal state")
        if len(original_evidence) != 2 or original_evidence[0]["corpus_index"] is None:
            raise RuntimeError("two-variation beta original evidence was incomplete")
        if original_evidence[0]["sha256"] == original_evidence[1]["sha256"]:
            raise RuntimeError("two sequential MIDI variations produced identical MP3 bytes")
        cover_id = _submit_cover(client, youtube_url=args.youtube_url, seed=args.seed + 1)
        cover = _wait_for_completion(client, cover_id, timeout_seconds=args.timeout_seconds)
        cover_evidence = _assert_mp3_outputs(client, cover)
        if len(cover_evidence) != 1 or cover_evidence[0]["corpus_index"] is None:
            raise RuntimeError("beta cover evidence was incomplete")
    after = _mock_health(args.mock_base_url, mock_token)
    cursor_after = after["cursor"]["last_consumed_index"]
    expected_indices = [(cursor_before + offset) % CORPUS_MEMBER_COUNT for offset in range(3)]
    actual_indices = [item["corpus_index"] for item in (*original_evidence, *cover_evidence)]
    if actual_indices != expected_indices or cursor_after != expected_indices[-1]:
        raise RuntimeError("beta cursor evidence did not match three sequential claims")
    return {
        "status": "passed",
        "product_revision": args.expected_revision,
        "original_job_id": original_id,
        "cover_job_id": cover_id,
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "outputs": [*original_evidence, *cover_evidence],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://player.evren.io")
    parser.add_argument("--mock-base-url", default=os.environ.get("MOCK_BASE_URL", ""))
    parser.add_argument("--youtube-url", required=True)
    parser.add_argument("--seed", type=int, default=2_026_082_200)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--expected-revision")
    return parser


def main() -> int:
    try:
        args = _parser().parse_args()
        if not args.mock_base_url:
            raise RuntimeError("--mock-base-url or MOCK_BASE_URL is required")
        result = run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
