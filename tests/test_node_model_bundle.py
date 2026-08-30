from __future__ import annotations

import json

from ace_node.model_bundle import _ProgressReporter
from runpod_worker.runtime import MODEL_BUNDLE_FILE_COUNT, MODEL_BUNDLE_TOTAL_BYTES


class _Bar:
    unit = "B"
    total = 100
    n = 0


def test_model_progress_events_are_bounded_monotonic_and_redacted() -> None:
    lines: list[str] = []
    clock = [0.0]
    reporter = _ProgressReporter(lines.append, clock=lambda: clock[0])
    reporter.emit("starting", force=True)
    bar = _Bar()
    reporter.register(bar)
    bar.n = 25
    clock[0] = 0.3
    reporter.update(bar)
    bar.n = 100
    clock[0] = 0.6
    reporter.finish(bar)
    reporter.emit("complete", force=True)

    events = [json.loads(line) for line in lines]
    assert all(
        set(event)
        == {
            "stage",
            "downloaded_bytes",
            "total_bytes",
            "completed_files",
            "total_files",
            "safe_error_code",
        }
        for event in events
    )
    assert [event["downloaded_bytes"] for event in events] == sorted(
        event["downloaded_bytes"] for event in events
    )
    assert events[-1]["stage"] == "complete"
    assert events[-1]["total_bytes"] == MODEL_BUNDLE_TOTAL_BYTES
    assert events[-1]["total_files"] == MODEL_BUNDLE_FILE_COUNT
    assert all("token" not in line and "path" not in line for line in lines)
