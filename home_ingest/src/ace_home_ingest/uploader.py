"""Restricted SFTP upload for prepared home-ingest sources."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from .config import HomeIngestSettings
from .media import IngestError


class SFTPUploadError(IngestError):
    """A stable error raised when the prepared source cannot reach Hetzner."""

    def __init__(self, message: str = "the prepared source could not be uploaded") -> None:
        super().__init__("sftp_upload_failed", message)


def remote_source_part_path(remote_root: str, job_id: str | UUID) -> str:
    """Build the only permitted remote destination from a canonical UUID."""

    try:
        canonical_job_id = str(UUID(str(job_id)))
    except (AttributeError, ValueError) as exc:
        raise SFTPUploadError("the SFTP job identifier is invalid") from exc
    root = PurePosixPath(remote_root)
    if not root.is_absolute() or any(part == ".." for part in root.parts):
        raise SFTPUploadError("the configured SFTP destination is invalid")
    destination = root / canonical_job_id / "source.mp3.part"
    if destination.parent != root / canonical_job_id or not destination.is_relative_to(root):
        raise SFTPUploadError("the SFTP destination escaped the incoming directory")
    return str(destination)


class SFTPUploader:
    """Upload a canonical source using a dedicated Paramiko SFTP identity."""

    def __init__(self, settings: HomeIngestSettings) -> None:
        self.settings = settings

    def upload(self, local_path: Path, job_id: str | UUID) -> str:
        if local_path.name != "source.mp3" or local_path.is_symlink() or not local_path.is_file():
            raise SFTPUploadError("the upload source is not the canonical MP3")
        destination = remote_source_part_path(self.settings.sftp_remote_root, job_id)
        try:
            self.settings.validate_sftp_runtime()
            import paramiko

            key = _load_private_key(paramiko, self.settings.sftp_private_key)
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=self.settings.sftp_host,
                port=self.settings.sftp_port,
                username=self.settings.sftp_username,
                pkey=key,
                look_for_keys=False,
                allow_agent=False,
                timeout=self.settings.sftp_connect_timeout_seconds,
            )
            try:
                sftp = client.open_sftp()
                try:
                    remote_directory = str(PurePosixPath(destination).parent)
                    try:
                        sftp.stat(remote_directory)
                    except OSError:
                        sftp.mkdir(remote_directory)
                    sftp.put(str(local_path), destination)
                    remote_attributes = sftp.stat(destination)
                    if remote_attributes.st_size is None:
                        raise SFTPUploadError("the SFTP upload size was unavailable")
                    remote_size = int(remote_attributes.st_size)
                    local_size = local_path.stat().st_size
                    if remote_size != local_size:
                        raise SFTPUploadError(
                            "the SFTP upload size did not match the prepared source"
                        )
                finally:
                    sftp.close()
            finally:
                client.close()
        except SFTPUploadError:
            raise
        except Exception as exc:
            raise SFTPUploadError() from exc
        return destination


def _load_private_key(paramiko: Any, path: Path) -> Any:
    key_types = ("RSAKey", "Ed25519Key", "ECDSAKey", "DSSKey")
    last_error: Exception | None = None
    for key_type_name in key_types:
        key_type = getattr(paramiko, key_type_name, None)
        if key_type is None:
            continue
        try:
            return key_type.from_private_key_file(str(path))
        except Exception as exc:
            last_error = exc
    raise SFTPUploadError("the configured SFTP private key could not be loaded") from last_error
