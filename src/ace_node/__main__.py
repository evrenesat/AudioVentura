"""Run the ACE Node HTTP service with a parent-owned lifecycle receipt."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import uvicorn

from .app import create_app
from .config import NodeSettings

MAX_RECEIPT_BYTES = 4_096
_RECEIPT_FIELDS = frozenset(
    {
        "parent_pid",
        "worker_pid",
        "process_start_identity",
        "executable_path",
        "application_revision",
    }
)


@dataclass(frozen=True, slots=True)
class WorkerProcessReceipt:
    """The minimum identity needed before a supervisor may stop a worker."""

    parent_pid: int
    worker_pid: int
    process_start_identity: str
    executable_path: str
    application_revision: str

    def as_mapping(self) -> dict[str, object]:
        return asdict(self)


def process_start_identity(pid: int) -> str | None:
    """Return a stable-enough process-start value for the current platform."""

    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return fields[21] if len(fields) > 21 else None
        except (OSError, ValueError):
            return None
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value[:256] if value else None


def process_executable_path(pid: int) -> str | None:
    """Resolve the executable path without trusting a process name."""

    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            return str(Path(f"/proc/{pid}/exe").resolve(strict=True))
        except OSError:
            return None
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "comm="],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    if not value or not value.startswith("/"):
        return None
    return str(Path(value).resolve())


def _validate_receipt_mapping(value: Any) -> WorkerProcessReceipt:
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        raise ValueError("worker receipt fields do not match")
    parent_pid = value.get("parent_pid")
    worker_pid = value.get("worker_pid")
    if (
        isinstance(parent_pid, bool)
        or not isinstance(parent_pid, int)
        or parent_pid <= 0
        or isinstance(worker_pid, bool)
        or not isinstance(worker_pid, int)
        or worker_pid <= 0
        or parent_pid == worker_pid
    ):
        raise ValueError("worker receipt process identity is invalid")
    start_identity = value.get("process_start_identity")
    executable_path = value.get("executable_path")
    application_revision = value.get("application_revision")
    if (
        not isinstance(start_identity, str)
        or not 1 <= len(start_identity) <= 256
        or not isinstance(executable_path, str)
        or not Path(executable_path).is_absolute()
        or not isinstance(application_revision, str)
        or len(application_revision) != 40
        or any(character not in "0123456789abcdef" for character in application_revision)
    ):
        raise ValueError("worker receipt values are invalid")
    return WorkerProcessReceipt(
        parent_pid=parent_pid,
        worker_pid=worker_pid,
        process_start_identity=start_identity,
        executable_path=str(Path(executable_path).resolve()),
        application_revision=application_revision,
    )


def read_worker_receipt(path: str | Path) -> WorkerProcessReceipt | None:
    receipt_path = Path(path)
    try:
        raw = receipt_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("worker receipt is unreadable") from exc
    if len(raw) > MAX_RECEIPT_BYTES:
        raise RuntimeError("worker receipt is too large")
    try:
        return _validate_receipt_mapping(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("worker receipt is malformed") from exc


def write_worker_receipt(path: str | Path, receipt: WorkerProcessReceipt) -> None:
    receipt_path = Path(path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(receipt.as_mapping(), sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_RECEIPT_BYTES:
        raise RuntimeError("worker receipt is too large")
    with tempfile.NamedTemporaryFile(
        dir=receipt_path.parent,
        prefix=f".{receipt_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        temporary_path.chmod(0o600)
        os.replace(temporary_path, receipt_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def receipt_matches_process(
    receipt: WorkerProcessReceipt,
    *,
    expected_executable_path: str | Path,
    expected_application_revision: str,
) -> bool:
    """Prove every persisted identity field before any signal is sent."""

    if receipt.application_revision != expected_application_revision:
        return False
    if receipt.executable_path != str(Path(expected_executable_path).resolve()):
        return False
    try:
        os.kill(receipt.worker_pid, 0)
    except OSError:
        return False
    return (
        process_executable_path(receipt.worker_pid) == receipt.executable_path
        and process_start_identity(receipt.worker_pid) == receipt.process_start_identity
    )


def recover_stale_process(
    path: str | Path,
    *,
    expected_executable_path: str | Path,
    expected_application_revision: str,
) -> bool:
    """Terminate only a process whose complete receipt and live identity match."""

    receipt = read_worker_receipt(path)
    if (
        receipt is None
        or receipt.worker_pid == os.getpid()
        or not receipt_matches_process(
            receipt,
            expected_executable_path=expected_executable_path,
            expected_application_revision=expected_application_revision,
        )
    ):
        return False
    try:
        os.kill(receipt.worker_pid, signal.SIGTERM)
    except OSError:
        return False
    Path(path).unlink(missing_ok=True)
    return True


class ParentWatchdog:
    """Stop the service if the owning Swift application disappears."""

    def __init__(self, *, parent_pid: int | None = None, interval: float = 1.0) -> None:
        self.parent_pid = parent_pid if parent_pid is not None else os.getppid()
        self.interval = max(0.1, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="ace-node-parent-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=min(2.0, self.interval + 1.0))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if os.getppid() != self.parent_pid:
                os.kill(os.getpid(), signal.SIGTERM)
                return


def _own_receipt(settings: NodeSettings) -> WorkerProcessReceipt | None:
    revision = settings.application_revision
    if not revision:
        return None
    start_identity = process_start_identity(os.getpid())
    if start_identity is None:
        return None
    return WorkerProcessReceipt(
        parent_pid=os.getppid(),
        worker_pid=os.getpid(),
        process_start_identity=start_identity,
        executable_path=str(Path(sys.executable).resolve()),
        application_revision=revision,
    )


def main() -> None:
    settings = NodeSettings()
    watchdog = ParentWatchdog()
    receipt = _own_receipt(settings)
    receipt_path = settings.data_root / "state" / "worker.json"
    if receipt is not None:
        try:
            recover_stale_process(
                receipt_path,
                expected_executable_path=sys.executable,
                expected_application_revision=settings.application_revision,
            )
        except RuntimeError as exc:
            raise SystemExit("worker receipt is malformed") from exc
    if receipt is not None:
        write_worker_receipt(receipt_path, receipt)
    watchdog.start()
    try:
        uvicorn.run(
            create_app(settings),
            host=settings.listen_host,
            port=settings.listen_port,
            access_log=False,
        )
    finally:
        watchdog.stop()
        if receipt is not None:
            current = read_worker_receipt(receipt_path)
            if current == receipt:
                receipt_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
