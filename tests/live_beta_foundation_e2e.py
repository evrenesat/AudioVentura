"""Explicit, bounded paid smoke test for the isolated beta foundation.

This module is deliberately outside pytest discovery. Run it only with the
protected beta credentials loaded and the explicit ``--allow-paid`` flag. It
uses one MP3 original submission, then verifies publication, playlists, range
playback, and the authenticated browser-facing surfaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@dataclass(slots=True)
class PaidBudget:
    maximum: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise RuntimeError("paid submission budget exhausted")
        self.used += 1


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required protected environment variable is missing: {name}")
    return value


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if match is None:
        raise RuntimeError("beta form did not contain a CSRF token")
    return match.group(1)


def _job_id(response: httpx.Response) -> str:
    path = urlparse(str(response.url)).path.rstrip("/")
    value = path.rsplit("/", 1)[-1]
    if _UUID_RE.fullmatch(value) is None:
        raise RuntimeError("beta submission did not resolve to a job detail page")
    return value


def _get(client: httpx.Client, path: str) -> httpx.Response:
    response = client.get(path)
    if response.status_code >= 400:
        raise RuntimeError(f"beta request failed with status {response.status_code}")
    return response


def _submit_original(client: httpx.Client, budget: PaidBudget, args: argparse.Namespace) -> str:
    form = _get(client, "/beta/create")
    budget.consume()
    response = client.post(
        "/beta/create",
        data={
            "csrf_token": _csrf(form.text),
            "description": args.description,
            "lyrics": "",
            "instrumental": "true",
            "prompt_mode": "original",
            "vocal_language": "en",
            "duration_mode": "auto",
            "variation_count": "1",
            "output_format": "mp3",
            "seed": str(args.seed),
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"beta original submission failed with status {response.status_code}")
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
            if not isinstance(outputs, list) or len(outputs) != 1:
                raise RuntimeError("completed beta smoke job did not expose one output")
            output = outputs[0]
            if not isinstance(output, dict) or not output.get("media_item_id"):
                raise RuntimeError("completed beta output was not published to the library")
            return body
        if state in {"failed", "cancelled"}:
            raise RuntimeError(f"beta smoke job reached terminal state {state}")
        time.sleep(5)
    raise RuntimeError("beta smoke job timed out")


def _verify_media_and_playlists(
    client: httpx.Client,
    status_body: dict[str, Any],
    *,
    description: str,
) -> dict[str, Any]:
    output = status_body["outputs"][0]
    media_url = output.get("media_url")
    download_url = output.get("download_url")
    media_item_id = output.get("media_item_id")
    if not all(isinstance(value, str) for value in (media_url, download_url, media_item_id)):
        raise RuntimeError("beta smoke output did not expose safe media URLs")

    ranged = client.get(media_url, headers={"Range": "bytes=0-0"})
    if ranged.status_code != 206 or len(ranged.content) != 1:
        raise RuntimeError("beta playback route did not honor a one-byte range")
    downloaded = client.get(download_url)
    if downloaded.status_code != 200 or not downloaded.content:
        raise RuntimeError("beta download route did not return the published MP3")

    library = _get(client, "/beta/library")
    if output["title"] not in library.text:
        raise RuntimeError("published beta output was absent from the library page")
    queue = _get(client, "/beta/player/queue/library").json()
    if not any(item.get("id") == media_item_id for item in queue.get("items", [])):
        raise RuntimeError("published beta output was absent from the library queue")

    project_url = status_body.get("project_url")
    if not isinstance(project_url, str):
        raise RuntimeError("completed beta job did not expose its project URL")
    project = _get(client, project_url)
    if description not in project.text:
        raise RuntimeError("beta project page did not expose the submitted project")

    playlists_page = _get(client, "/beta/playlists")
    csrf = _csrf(playlists_page.text)
    custom_title = f"Beta smoke {media_item_id[:8]}"
    created = client.post(
        "/beta/playlists",
        data={"csrf_token": csrf, "title": custom_title},
    )
    if created.status_code >= 400:
        raise RuntimeError(
            f"beta custom playlist creation failed with status {created.status_code}"
        )
    playlist_id = urlparse(str(created.url)).path.rstrip("/").rsplit("/", 1)[-1]
    if _UUID_RE.fullmatch(playlist_id) is None:
        raise RuntimeError("beta custom playlist redirect was malformed")
    detail = client.post(
        f"/beta/playlists/{playlist_id}/entries",
        data={"csrf_token": csrf, "media_item_id": media_item_id},
    )
    if detail.status_code >= 400:
        raise RuntimeError(f"beta playlist entry creation failed with status {detail.status_code}")
    playlist_queue = _get(client, f"/beta/player/queue/playlist/{playlist_id}").json()
    if [item.get("id") for item in playlist_queue.get("items", [])] != [media_item_id]:
        raise RuntimeError("beta custom playlist queue did not contain the published output")

    return {
        "job_id": status_body.get("id"),
        "project_url": project_url,
        "media_item_id": media_item_id,
        "byte_count": len(downloaded.content),
        "sha256": hashlib.sha256(downloaded.content).hexdigest(),
        "playlist_id": playlist_id,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_paid:
        raise RuntimeError("refusing paid beta smoke without --allow-paid")
    if args.max_paid_submissions != 1:
        raise RuntimeError("this beta smoke requires an exact one-submission paid budget")
    if args.timeout_seconds <= 0:
        raise RuntimeError("timeout must be positive")
    if args.resume_job_id is not None and _UUID_RE.fullmatch(args.resume_job_id) is None:
        raise RuntimeError("resume job ID is malformed")
    parsed_base = urlparse(args.base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise RuntimeError("beta base URL must be an absolute HTTP(S) URL")
    if parsed_base.username or parsed_base.password:
        raise RuntimeError("beta base URL must not contain credentials")

    budget = PaidBudget(1)
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        auth=(_required_env("ACE_SERVICE_USERNAME"), _required_env("ACE_SERVICE_PASSWORD")),
        follow_redirects=True,
        timeout=30,
    ) as client:
        _get(client, "/beta/")
        job_id = args.resume_job_id
        if job_id is None:
            job_id = _submit_original(client, budget, args)
        else:
            # Resuming polls the already-paid job; it does not spend a second
            # submission. The exact budget still remains one for this run.
            budget.used = 1
        status_body = _wait_for_completion(client, job_id, timeout_seconds=args.timeout_seconds)
        result = _verify_media_and_playlists(client, status_body, description=args.description)

    if budget.used != 1:
        raise RuntimeError("beta smoke did not account for exactly one paid submission")
    return {"status": "passed", "paid_submissions": 1, **result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://player.evren.io")
    parser.add_argument("--description", default="Beta foundation smoke composition")
    parser.add_argument("--seed", type=int, default=2_026_082_700)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-paid-submissions", type=int, default=1)
    parser.add_argument("--resume-job-id")
    parser.add_argument("--allow-paid", action="store_true")
    return parser


def main() -> int:
    try:
        result = run(_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
