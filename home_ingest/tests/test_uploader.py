from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ace_home_ingest.config import HomeIngestSettings
from ace_home_ingest.media import IngestError
from ace_home_ingest.uploader import SFTPUploader, remote_source_part_path


def test_remote_source_path_is_uuid_derived_and_contained() -> None:
    path = remote_source_part_path(
        "/srv/ace-service/data/incoming", "123e4567-e89b-12d3-a456-426614174000"
    )
    assert path == (
        "/srv/ace-service/data/incoming/123e4567-e89b-12d3-a456-426614174000/source.mp3.part"
    )


@pytest.mark.parametrize(
    "root,job_id",
    [
        ("incoming", "123e4567-e89b-12d3-a456-426614174000"),
        ("/srv/incoming/../outside", "123e4567-e89b-12d3-a456-426614174000"),
        ("/srv/incoming", "../../escape"),
    ],
)
def test_remote_source_path_rejects_traversal(root: str, job_id: str) -> None:
    with pytest.raises(IngestError, match="SFTP"):
        remote_source_part_path(root, job_id)


def test_remote_path_does_not_depend_on_local_filename(tmp_path: Path) -> None:
    local = tmp_path / "some-title.mp3"
    local.write_bytes(b"audio")
    assert "some-title" not in remote_source_part_path(
        "/srv/incoming", "123e4567-e89b-12d3-a456-426614174000"
    )


def test_sftp_upload_uses_only_deterministic_part_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"canonical")
    key_file = tmp_path / "key"
    key_file.write_text("private key")
    settings = HomeIngestSettings(
        data_root=tmp_path / "data",
        token="home-secret",
        sftp_host="hetzner.tailnet.ts.net",
        sftp_username="ace-incoming",
        sftp_private_key=key_file,
        sftp_remote_root="/srv/incoming",
    )

    class FakeRSAKey:
        @classmethod
        def from_private_key_file(cls, path: str) -> str:
            assert path == str(key_file)
            return "key"

    class FakeSFTP:
        def __init__(self) -> None:
            self.uploads: list[tuple[str, str]] = []
            self.directories: list[str] = []

        def stat(self, path: str) -> SimpleNamespace:
            if path.endswith("/123e4567-e89b-12d3-a456-426614174000"):
                raise OSError("missing directory")
            return SimpleNamespace(st_size=source.stat().st_size)

        def mkdir(self, path: str) -> None:
            self.directories.append(path)

        def put(self, local: str, remote: str) -> None:
            self.uploads.append((local, remote))

        def close(self) -> None:
            return None

    fake_sftp = FakeSFTP()

    class FakeClient:
        def load_system_host_keys(self) -> None:
            return None

        def set_missing_host_key_policy(self, policy: object) -> None:
            return None

        def connect(self, **kwargs: object) -> None:
            assert kwargs["hostname"] == "hetzner.tailnet.ts.net"
            assert kwargs["username"] == "ace-incoming"
            assert kwargs["look_for_keys"] is False
            assert kwargs["allow_agent"] is False

        def open_sftp(self) -> FakeSFTP:
            return fake_sftp

        def close(self) -> None:
            return None

    fake_paramiko = SimpleNamespace(
        RSAKey=FakeRSAKey,
        SSHClient=FakeClient,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    destination = SFTPUploader(settings).upload(source, "123e4567-e89b-12d3-a456-426614174000")
    assert destination == ("/srv/incoming/123e4567-e89b-12d3-a456-426614174000/source.mp3.part")
    assert fake_sftp.uploads == [(str(source), destination)]
