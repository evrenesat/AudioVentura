"""Explicit, bounded paid smoke test for the deployed private browser UI.

This module is intentionally outside pytest discovery. Run it only with the
protected production environment loaded and the explicit ``--allow-paid`` flag.
It submits exactly two one-variation cover jobs: one YouTube ingest followed by
one continuation that must reuse the completed local output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


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
        raise RuntimeError("UI form did not contain a CSRF token")
    return match.group(1)


def _job_id(response: httpx.Response) -> str:
    path = urlparse(str(response.url)).path.rstrip("/")
    job_id = path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
        raise RuntimeError(f"submission did not resolve to a job detail page: {path}")
    return job_id


def _get(client: httpx.Client, path: str) -> httpx.Response:
    response = client.get(path)
    response.raise_for_status()
    return response


def _submit_cover(
    client: httpx.Client,
    budget: PaidBudget,
    *,
    form_path: str,
    prompt: str,
    duration_seconds: float,
    youtube_url: str | None,
    seed: int,
) -> str:
    form = _get(client, form_path)
    data = {
        "csrf_token": _csrf(form.text),
        "target_style": prompt,
        "remix_guidance": "",
        "lyrics": "",
        "audio_cover_strength": "0.65",
        "cover_noise_strength": "0.0",
        "duration_mode": "custom",
        "duration_seconds": str(duration_seconds),
        "variation_count": "1",
        "seed": str(seed),
        "output_format": "mp3",
        "rights_confirmation": "true",
    }
    continuation_match = re.search(
        r'name="continue_from_job_id" value="([0-9a-f-]{36})"', form.text
    )
    if continuation_match is not None:
        data["continue_from_job_id"] = continuation_match.group(1)
        if 'name="youtube_url"' in form.text:
            raise RuntimeError("continuation form unexpectedly requested YouTube again")
        if "YouTube is not contacted again" not in form.text:
            raise RuntimeError("continuation form did not describe local output reuse")
    elif youtube_url is None:
        raise RuntimeError("initial cover submission requires a YouTube URL")
    else:
        data["youtube_url"] = youtube_url

    budget.consume()
    response = client.post("/beta/cover", data=data)
    response.raise_for_status()
    return _job_id(response)


def _wait_for_completion(
    client: httpx.Client, job_id: str, *, timeout_seconds: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        response = _get(client, f"/beta/jobs/{job_id}/status")
        body = response.json()
        last_status = str(body.get("status"))
        if last_status == "completed":
            outputs = body.get("outputs")
            if not isinstance(outputs, list) or len(outputs) != 1:
                raise RuntimeError("completed smoke job did not expose exactly one output")
            if body.get("target_duration_seconds") != 60.0:
                raise RuntimeError("completed smoke job did not preserve the 60-second target")
            return body
        if last_status == "failed":
            code = body.get("error_code", "unknown")
            message = body.get("error", "unknown")
            raise RuntimeError(f"paid smoke job failed: {job_id} {code}: {message}")
        time.sleep(5)
    raise RuntimeError(f"paid smoke job timed out: {job_id} last_status={last_status}")


def _assert_output_surfaces(client: httpx.Client, status: dict[str, Any]) -> None:
    output = status["outputs"][0]
    for field in ("media_url", "download_url"):
        response = client.get(output[field], headers={"Range": "bytes=0-0"})
        if response.status_code != 206 or len(response.content) != 1:
            raise RuntimeError(f"{field} did not honor a bounded byte-range request")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_paid:
        raise RuntimeError("refusing paid smoke without --allow-paid")
    if args.max_paid_submissions != 2:
        raise RuntimeError("this smoke requires an exact two-submission paid budget")
    budget = PaidBudget(args.max_paid_submissions)
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        auth=(_required_env("ACE_SERVICE_USERNAME"), _required_env("ACE_SERVICE_PASSWORD")),
        follow_redirects=True,
        timeout=30,
    ) as client:
        for path in ("/", "/beta/", "/beta/create", "/beta/cover", "/beta/projects", "/beta/jobs"):
            page = _get(client, path)
            if (
                path in {"/beta/create", "/beta/cover"}
                and 'name="duration_seconds"' not in page.text
            ):
                raise RuntimeError(f"duration control is missing from {path}")

        first_id = _submit_cover(
            client,
            budget,
            form_path="/beta/cover",
            prompt=args.prompt,
            duration_seconds=60.0,
            youtube_url=args.youtube_url,
            seed=args.seed,
        )
        first = _wait_for_completion(client, first_id, timeout_seconds=args.timeout_seconds)
        _assert_output_surfaces(client, first)
        continue_url = first.get("continue_url")
        if not isinstance(continue_url, str):
            raise RuntimeError("completed cover did not expose continuation")

        second_id = _submit_cover(
            client,
            budget,
            form_path=continue_url,
            prompt=args.prompt,
            duration_seconds=60.0,
            youtube_url=None,
            seed=args.seed + 1,
        )
        second = _wait_for_completion(client, second_id, timeout_seconds=args.timeout_seconds)
        _assert_output_surfaces(client, second)
        if second.get("project_url") != first.get("project_url"):
            raise RuntimeError("continued cover did not remain in the same project")
        _get(client, str(second["project_url"]))

    if budget.used != 2:
        raise RuntimeError("paid smoke did not consume its exact expected budget")
    return {
        "status": "passed",
        "paid_submissions": budget.used,
        "initial_job_id": first_id,
        "continuation_job_id": second_id,
        "project_url": second["project_url"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://player.evren.io")
    parser.add_argument("--youtube-url", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seed", type=int, default=2_026_082_200)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-paid-submissions", type=int, default=2)
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
