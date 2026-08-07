from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from runpod_worker.schemas import ResultUpload, SourceInput
from runpod_worker.transfer_client import TransferClient, TransferError


class FakeResponse:
    def __init__(
        self, body: bytes, *, content_length: int | None = None, status: int = 200
    ) -> None:
        self._body = body
        self._position = 0
        self.headers = {"Content-Length": str(content_length)} if content_length is not None else {}
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body)
        start = self._position
        self._position += size
        return self._body[start : start + size]


class FakeConnection:
    instances: list[FakeConnection] = []

    def __init__(self, _host: str, _port: int, *, timeout: float) -> None:
        self.headers: dict[str, str] = {}
        self.body = bytearray()
        self.status = 201
        self.request_target = ""
        self.timeout = timeout
        self.__class__.instances.append(self)

    def putrequest(self, _method: str, target: str) -> None:
        self.request_target = target

    def putheader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        return None

    def send(self, chunk: bytes) -> None:
        self.body.extend(chunk)

    def getresponse(self) -> FakeResponse:
        return FakeResponse(b"accepted", status=self.status)

    def close(self) -> None:
        return None


def _source(body: bytes, expected_bytes: int | None = None) -> SourceInput:
    return SourceInput(
        url="https://transfer.example.test/transfer/v1/source/capability",
        sha256=sha256(body).hexdigest(),
        bytes=len(body) if expected_bytes is None else expected_bytes,
        format="mp3",
    )


def test_source_download_streams_and_verifies(tmp_path: Path) -> None:
    body = b"source bytes"
    client = TransferClient(
        opener=lambda _request, timeout: FakeResponse(body, content_length=len(body))
    )
    destination = tmp_path / "source.mp3"

    downloaded = client.download_source(_source(body), destination)

    assert downloaded.path == destination
    assert downloaded.bytes == len(body)
    assert destination.read_bytes() == body
    assert not (tmp_path / "source.mp3.part").exists()


def test_source_sha_mismatch_removes_partial_file(tmp_path: Path) -> None:
    body = b"source bytes"
    bad_source = SourceInput(
        url="https://transfer.example.test/transfer/v1/source/capability",
        sha256="0" * 64,
        bytes=len(body),
        format="mp3",
    )
    client = TransferClient(opener=lambda _request, timeout: FakeResponse(body))
    destination = tmp_path / "source.mp3"

    with pytest.raises(TransferError, match="SHA-256"):
        client.download_source(bad_source, destination)

    assert not destination.exists()
    assert not (tmp_path / "source.mp3.part").exists()


def test_source_size_cap_is_enforced(tmp_path: Path) -> None:
    body = b"too large"
    client = TransferClient(opener=lambda _request, timeout: FakeResponse(body))

    with pytest.raises(TransferError, match="byte size"):
        client.download_source(_source(body, expected_bytes=4), tmp_path / "source.mp3")


def test_output_upload_is_streamed_with_checksum_headers(tmp_path: Path) -> None:
    FakeConnection.instances.clear()
    body = b"generated mp3"
    output = tmp_path / "generated.mp3"
    output.write_bytes(body)
    client = TransferClient(connection_factory=FakeConnection)
    capability = ResultUpload(
        url="https://transfer.example.test/transfer/v1/output/capability",
        max_bytes=1024,
    )

    uploaded = client.upload_output(capability, output)
    connection = FakeConnection.instances[0]

    assert uploaded.status_code == 201
    assert uploaded.bytes == len(body)
    assert uploaded.sha256 == sha256(body).hexdigest()
    assert bytes(connection.body) == body
    assert connection.headers["Content-Length"] == str(len(body))
    assert connection.headers["X-ACE-Output-SHA256"] == sha256(body).hexdigest()
    assert "capability" in connection.request_target


def test_failed_upload_is_propagated_and_oversized_file_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "generated.mp3"
    output.write_bytes(b"generated mp3")
    capability = ResultUpload(
        url="https://transfer.example.test/transfer/v1/output/capability",
        max_bytes=2,
    )
    client = TransferClient(connection_factory=FakeConnection)

    with pytest.raises(TransferError, match="byte limit"):
        client.upload_output(capability, output)


def test_http_upload_failure_is_propagated(tmp_path: Path) -> None:
    output = tmp_path / "generated.mp3"
    output.write_bytes(b"generated mp3")

    class RejectingConnection(FakeConnection):
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            super().__init__(host, port, timeout=timeout)
            self.status = 503

    client = TransferClient(connection_factory=RejectingConnection)
    capability = ResultUpload(
        url="https://transfer.example.test/transfer/v1/output/capability",
        max_bytes=1024,
    )

    with pytest.raises(TransferError, match="rejected"):
        client.upload_output(capability, output)
