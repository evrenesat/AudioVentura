"""Canonical, safe access to the immutable MIDI ZIP corpus."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import CORPUS_MANIFEST_SHA256, CORPUS_MEMBER_COUNT, CORPUS_SHA256

FIRST_MEMBER = "midis/A., Jag, Je t'aime Juliette, OXC7Fd0ZN8o.mid"
LAST_MEMBER = "midis/Żołnowski, Maciej, Deszcz, S9nVJOmDCtI.mid"
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class CorpusError(ValueError):
    """Raised when the configured corpus is not the reviewed safe corpus."""


@dataclass(frozen=True, slots=True)
class CorpusMember:
    index: int
    name: str
    sha256: str
    byte_size: int

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class CorpusManifest:
    archive_sha256: str
    members: tuple[CorpusMember, ...]
    manifest_sha256: str

    @property
    def member_count(self) -> int:
        return len(self.members)

    def member(self, index: int) -> CorpusMember:
        if not 0 <= index < len(self.members):
            raise CorpusError("corpus index is outside the manifest")
        return self.members[index]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise CorpusError("corpus archive could not be read") from exc
    return digest.hexdigest()


def _safe_member_name(name: str) -> tuple[bool, str]:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise CorpusError("corpus contains an unsafe ZIP member name")
    if unicodedata.normalize("NFC", name) != name:
        raise CorpusError("corpus member names must already be NFC")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CorpusError("corpus contains an absolute or traversal ZIP member name")
    directory = name.endswith("/")
    if directory and not name.endswith("/"):
        raise CorpusError("invalid ZIP directory name")
    if not directory and not name.lower().endswith(".mid"):
        raise CorpusError("corpus contains an unexpected non-MIDI ZIP member")
    return directory, name


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _is_regular_or_directory(info: zipfile.ZipInfo, *, directory: bool) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    allowed = {0, 0o040000} if directory else {0, 0o100000}
    return mode in allowed


def _member_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, "r") as source:
            prefix = source.read(4)
            if prefix != b"MThd":
                raise CorpusError("corpus member is not a MIDI file")
            digest.update(prefix)
            size = 4
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                if size > MAX_MEMBER_BYTES:
                    raise CorpusError("corpus member exceeds the safety limit")
                digest.update(block)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CorpusError("corpus member could not be read") from exc
    return digest.hexdigest(), size


def _manifest_document(archive_sha256: str, members: tuple[CorpusMember, ...]) -> dict[str, Any]:
    return {
        "archive_sha256": archive_sha256,
        "member_count": len(members),
        "members": [
            {
                "index": member.index,
                "name": member.name,
                "sha256": member.sha256,
                "bytes": member.byte_size,
            }
            for member in members
        ],
    }


def _canonical_json(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _manifest_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(document)).hexdigest()


def build_manifest(
    archive_path: Path,
    *,
    expected_archive_sha256: str = CORPUS_SHA256,
    expected_member_count: int = CORPUS_MEMBER_COUNT,
    strict_identity: bool = True,
) -> CorpusManifest:
    """Validate every member and return the deterministic sorted manifest."""

    if archive_path.is_symlink() or not archive_path.is_file():
        raise CorpusError("corpus archive must be an existing regular file")
    archive_sha256 = sha256_file(archive_path)
    if archive_sha256 != expected_archive_sha256:
        raise CorpusError("corpus archive SHA-256 does not match the reviewed identity")
    seen: set[str] = set()
    candidates: list[tuple[str, zipfile.ZipInfo]] = []
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                directory, name = _safe_member_name(info.filename)
                if name in seen:
                    raise CorpusError("corpus contains duplicate ZIP member names")
                seen.add(name)
                if info.flag_bits & 0x1:
                    raise CorpusError("encrypted corpus members are not allowed")
                if _is_symlink(info):
                    raise CorpusError("symlink corpus members are not allowed")
                if directory:
                    if not info.is_dir() or not _is_regular_or_directory(info, directory=True):
                        raise CorpusError("ZIP directory marker is malformed")
                    continue
                if info.is_dir() or not _is_regular_or_directory(info, directory=False):
                    raise CorpusError("corpus contains a non-regular ZIP member")
                candidates.append((name, info))
            candidates.sort(key=lambda item: item[0].encode("utf-8"))
            members = tuple(
                CorpusMember(index, name, *_member_digest(archive, info))
                for index, (name, info) in enumerate(candidates)
            )
    except zipfile.BadZipFile as exc:
        raise CorpusError("corpus archive is not a valid ZIP") from exc
    if len(members) != expected_member_count:
        raise CorpusError("corpus member count does not match the reviewed identity")
    if strict_identity:
        directories = sorted(name for name in seen if name.endswith("/"))
        if directories != ["midis/"]:
            raise CorpusError("reviewed corpus must contain exactly the midis directory")
        if members[0].name != FIRST_MEMBER or members[-1].name != LAST_MEMBER:
            raise CorpusError("corpus sort order does not match the reviewed identity")
    document = _manifest_document(archive_sha256, members)
    return CorpusManifest(archive_sha256, members, _manifest_hash(document))


def write_manifest(manifest: CorpusManifest, path: Path) -> None:
    """Write one canonical manifest atomically with protected permissions."""

    document = _manifest_document(manifest.archive_sha256, manifest.members)
    if _manifest_hash(document) != manifest.manifest_sha256:
        raise CorpusError("manifest identity is internally inconsistent")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_name(f".{path.name}.part")
    try:
        with temporary.open("wb") as destination:
            # The file bytes are the canonical JSON bytes, so the recorded
            # manifest identity is also the ordinary file SHA-256.
            destination.write(_canonical_json(document))
            destination.flush()
            os.fsync(destination.fileno())
        temporary.chmod(0o640)
        os.replace(temporary, path)
        path.chmod(0o640)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise CorpusError("manifest could not be written") from exc


def load_manifest(path: Path, *, expected_sha256: str | None = None) -> CorpusManifest:
    """Load and validate a canonical manifest without exposing ZIP contents."""

    if path.is_symlink() or not path.is_file():
        raise CorpusError("manifest must be an existing regular file")
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise CorpusError("manifest is too large")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusError("manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise CorpusError("manifest root must be an object")
    archive_sha256 = document.get("archive_sha256")
    raw_members = document.get("members")
    if not isinstance(archive_sha256, str) or not isinstance(raw_members, list):
        raise CorpusError("manifest identity is incomplete")
    if document.get("member_count") != len(raw_members):
        raise CorpusError("manifest member count is inconsistent")
    members: list[CorpusMember] = []
    for expected_index, raw in enumerate(raw_members):
        if not isinstance(raw, dict):
            raise CorpusError("manifest member is malformed")
        index, name, digest, byte_size = (
            raw.get("index"),
            raw.get("name"),
            raw.get("sha256"),
            raw.get("bytes"),
        )
        if (
            index != expected_index
            or not isinstance(name, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size <= 4
        ):
            raise CorpusError("manifest member evidence is malformed")
        directory, _ = _safe_member_name(name)
        if directory:
            raise CorpusError("manifest contains a directory member")
        members.append(CorpusMember(index, name, digest, byte_size))
    frozen = tuple(members)
    if tuple(sorted(frozen, key=lambda member: member.name.encode("utf-8"))) != frozen:
        raise CorpusError("manifest member order is not canonical")
    if (
        not frozen
        or len(frozen) != CORPUS_MEMBER_COUNT
        or frozen[0].name != FIRST_MEMBER
        or frozen[-1].name != LAST_MEMBER
    ):
        raise CorpusError("manifest does not identify the reviewed corpus")
    computed = _manifest_hash(_manifest_document(archive_sha256, frozen))
    if expected_sha256 is not None and computed != expected_sha256.lower():
        raise CorpusError("manifest SHA-256 does not match the configured identity")
    return CorpusManifest(archive_sha256, frozen, computed)


def load_verified_corpus(
    archive_path: Path,
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = CORPUS_MANIFEST_SHA256,
) -> CorpusManifest:
    """Fail closed on archive identity and use a previously generated manifest."""

    if sha256_file(archive_path) != CORPUS_SHA256:
        raise CorpusError("corpus archive SHA-256 does not match the reviewed identity")
    manifest = load_manifest(manifest_path, expected_sha256=expected_manifest_sha256)
    if manifest.archive_sha256 != CORPUS_SHA256:
        raise CorpusError("manifest archive identity does not match the reviewed corpus")
    return manifest


def extract_member(archive_path: Path, member: CorpusMember, destination: Path) -> Path:
    """Extract exactly one validated MIDI member into a private job directory."""

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    basename = member.basename
    if not basename or basename in {".", ".."} or "/" in basename or "\\" in basename:
        raise CorpusError("corpus member basename is unsafe")
    target = destination / basename
    temporary = destination / f".{basename}.part"
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = [info for info in archive.infolist() if info.filename == member.name]
            if len(infos) != 1:
                raise CorpusError("corpus member is not uniquely addressable")
            info = infos[0]
            directory, _ = _safe_member_name(info.filename)
            if directory or info.flag_bits & 0x1 or _is_symlink(info):
                raise CorpusError("corpus member is not a safe regular MIDI")
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "r") as source, temporary.open("xb") as output:
                prefix = source.read(4)
                if prefix != b"MThd":
                    raise CorpusError("corpus member is not a MIDI file")
                output.write(prefix)
                digest.update(prefix)
                size = 4
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(block)
                    if size > MAX_MEMBER_BYTES:
                        raise CorpusError("corpus member exceeds the safety limit")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if size != member.byte_size or digest.hexdigest() != member.sha256:
                raise CorpusError("corpus member content does not match its manifest evidence")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
    except OSError as exc:
        raise CorpusError("corpus member could not be extracted") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return target
