"""Environment-backed settings for one MIDI mock service instance."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CORPUS_SHA256 = "41549405bcaeed4783e366f61236db4203c9b5d846fd8e0fee59bcf2658a23b7"
CORPUS_MEMBER_COUNT = 10_855
CORPUS_MANIFEST_SHA256 = "916a7c9dbc1081efc27ff2fb59af1aeccef6052b1859e98585d9d9814f087c92"
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536
MAX_OUTPUT_BYTES = 268_435_456
PLACEHOLDERS = frozenset({"change-me", "changeme", "replace-me", "replace_me"})


def _env(env: dict[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _positive_int(env: dict[str, str], name: str, default: int, *, maximum: int) -> int:
    raw = _env(env, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return value


def _positive_float(env: dict[str, str], name: str, default: float, *, maximum: float) -> float:
    raw = _env(env, name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return value


@dataclass(frozen=True, slots=True)
class MockSettings:
    """Validated settings for one isolated mock instance."""

    host: str = "127.0.0.1"
    port: int = 8200
    token: str = "change-me"
    state_root: Path = Path("/var/lib/audioventura-midi-mock")
    corpus_archive: Path = Path(
        "/srv/audioventura-midi-corpus/"
        "41549405bcaeed4783e366f61236db4203c9b5d846fd8e0fee59bcf2658a23b7/midis_v1.2.zip"
    )
    manifest_path: Path | None = None
    soundfont_path: Path = Path("/usr/share/sounds/sf2/FluidR3_GM.sf2")
    fluidsynth_binary: str = "/usr/bin/fluidsynth"
    temp_root: Path | None = None
    renderer_id: str = "fluidsynth-2.3.4-lameenc-1.8.4"
    expected_manifest_sha256: str | None = CORPUS_MANIFEST_SHA256
    max_duration_seconds: int = 600
    max_output_bytes: int = MAX_OUTPUT_BYTES
    render_timeout_seconds: float = 900.0
    upload_timeout_seconds: float = 900.0
    sample_rate: int = 44_100
    bitrate_kbps: int = 192

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host or host in {"0.0.0.0", "::", "::0"}:
            raise ValueError("mock service must bind to a private non-wildcard host")
        if not 1 <= self.port <= 65_535:
            raise ValueError("mock service port is invalid")
        token = self.token.strip()
        if token.lower() in PLACEHOLDERS:
            raise ValueError("mock bearer token still contains a placeholder")
        if self.max_duration_seconds != 600:
            raise ValueError("the mock duration ceiling is fixed at 600 seconds")
        if self.max_output_bytes <= 0 or self.max_output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("mock output ceiling is invalid")
        for path_name in ("state_root", "corpus_archive", "soundfont_path"):
            path = getattr(self, path_name)
            if not path.is_absolute():
                raise ValueError(f"{path_name} must be absolute")
        if self.manifest_path is not None and not self.manifest_path.is_absolute():
            raise ValueError("manifest_path must be absolute")
        if self.temp_root is not None and not self.temp_root.is_absolute():
            raise ValueError("temp_root must be absolute")
        if self.sample_rate != 44_100 or self.bitrate_kbps != 192:
            raise ValueError("mock renderer format is fixed at 44.1 kHz and 192 kbps")
        if self.render_timeout_seconds <= 0 or self.render_timeout_seconds > 900:
            raise ValueError("render timeout must be between 0 and 900 seconds")
        if self.upload_timeout_seconds <= 0 or self.upload_timeout_seconds > 900:
            raise ValueError("upload timeout must be between 0 and 900 seconds")
        if self.expected_manifest_sha256 is not None:
            digest = self.expected_manifest_sha256.strip().lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("expected manifest SHA-256 is malformed")
            object.__setattr__(self, "expected_manifest_sha256", digest)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "state_root", self.state_root.resolve())
        object.__setattr__(self, "corpus_archive", self.corpus_archive.resolve())
        object.__setattr__(self, "soundfont_path", self.soundfont_path.resolve())
        if self.manifest_path is None:
            object.__setattr__(
                self, "manifest_path", self.corpus_archive.with_name("manifest.json")
            )
        else:
            object.__setattr__(self, "manifest_path", self.manifest_path.resolve())
        if self.temp_root is None:
            object.__setattr__(self, "temp_root", self.state_root / "temporary")
        else:
            object.__setattr__(self, "temp_root", self.temp_root.resolve())

    @property
    def database_path(self) -> Path:
        return self.state_root / "mock.sqlite3"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MockSettings:
        values = dict(os.environ if env is None else env)
        base_url = _env(
            values,
            "MOCK_BASE_URL",
            f"http://{values.get('MOCK_HOST', '127.0.0.1')}:{values.get('MOCK_PORT', '8200')}",
        )
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MOCK_BASE_URL must be an HTTP(S) URL with a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MOCK_BASE_URL must not contain credentials, query, or fragment")
        host = _env(values, "MOCK_HOST", parsed.hostname)
        port = _positive_int(values, "MOCK_PORT", parsed.port or 8200, maximum=65_535)
        manifest = values.get("MOCK_CORPUS_MANIFEST", "").strip() or None
        expected_manifest = (
            values.get("MOCK_CORPUS_MANIFEST_SHA256", "").strip() or CORPUS_MANIFEST_SHA256
        )
        return cls(
            host=host,
            port=port,
            token=_env(values, "MOCK_TOKEN", "change-me"),
            state_root=Path(_env(values, "MOCK_STATE_ROOT", "/var/lib/audioventura-midi-mock")),
            corpus_archive=Path(
                _env(
                    values,
                    "MOCK_CORPUS_ARCHIVE",
                    "/srv/audioventura-midi-corpus/"
                    "41549405bcaeed4783e366f61236db4203c9b5d846fd8e0fee59bcf2658a23b7/midis_v1.2.zip",
                )
            ),
            manifest_path=Path(manifest) if manifest else None,
            soundfont_path=Path(
                _env(values, "MOCK_SOUNDFONT", "/usr/share/sounds/sf2/FluidR3_GM.sf2")
            ),
            fluidsynth_binary=_env(values, "MOCK_FLUIDSYNTH", "/usr/bin/fluidsynth"),
            temp_root=Path(values["MOCK_TEMP_ROOT"]) if values.get("MOCK_TEMP_ROOT") else None,
            renderer_id=_env(values, "MOCK_RENDERER_ID", "fluidsynth-2.3.4-lameenc-1.8.4"),
            expected_manifest_sha256=expected_manifest,
            max_duration_seconds=_positive_int(
                values, "MOCK_MAX_DURATION_SECONDS", 600, maximum=600
            ),
            max_output_bytes=_positive_int(
                values, "MOCK_MAX_OUTPUT_BYTES", MAX_OUTPUT_BYTES, maximum=MAX_OUTPUT_BYTES
            ),
            render_timeout_seconds=_positive_float(
                values, "MOCK_RENDER_TIMEOUT_SECONDS", 900, maximum=900
            ),
            upload_timeout_seconds=_positive_float(
                values, "MOCK_UPLOAD_TIMEOUT_SECONDS", 900, maximum=900
            ),
        )

    def ensure_state_layout(self) -> None:
        for directory in (self.state_root, self.temp_root):
            assert directory is not None
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
