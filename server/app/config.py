"""Runtime configuration for the DGX Chat backend.

Everything is driven by environment variables (optionally seeded from a ``.env``
file next to the server package).  No third-party settings library is used so the
service has a predictable, tiny dependency surface.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "1.0.0"

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = SERVER_DIR / "data"

#: Client-facing model ids (``ChatModel`` in chat-demo) -> upstream model ids.
DEFAULT_MODEL_MAP: dict[str, str] = {
    "deepseek-flash": "deepseek-v4.1-flash-official",
    "deepseek-v4-pro": "deepseek-v4.1-flash-official",
}

#: Providers we know how to speak to.
#: Ways of disabling reasoning; ``reasoning_effort`` is the only switch verified
#: against BOTH the LAN gateway and the public gateway.
THINKING_DISABLE_MODES = (
    "reasoning_effort",
    "thinking",
    "both",
    "enable_thinking",
    "chat_template_kwargs",
    "none",
)
DEFAULT_THINKING_DISABLE_MODE = "reasoning_effort"

TTS_ASR_PROVIDERS = ("deepseek", "openai", "deepseek_tools", "disabled")
SEARCH_MODES = ("dgx", "official", "off")
SEARCH_PROVIDERS = ("deepseek", "brave", "tavily", "searxng", "google_cse", "none")
REGISTRATION_MODES = ("invite", "closed", "open")

#: Concrete Capacitor / loopback origins.  A wildcard is only used when the
#: operator explicitly sets ``CORS_ALLOW_ORIGINS=*``.
DEFAULT_CORS_ORIGINS: list[str] = [
    "https://localhost",
    "http://localhost",
    "capacitor://localhost",
    "ionic://localhost",
]

#: 127.0.0.1 on any port, plus private LAN ranges (the APK has no domain yet).
DEFAULT_CORS_ORIGIN_REGEX = (
    r"^(https?://(?:127\.0\.0\.1|0\.0\.0\.0|\[::1\]|localhost|"
    r"(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")(?::\d{1,5})?|capacitor://localhost|ionic://localhost|file://)$"
)


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader; existing environment variables win."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_json(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _env(name, default).strip().lower() or default
    return value if value in allowed else default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    items = [part.strip() for part in raw.replace(";", ",").split(",")]
    return [item for item in items if item]


# ---------------------------------------------------------------------------
# setting groups
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SpeechProviderConfig:
    """One upstream speech provider (used for both TTS and ASR)."""

    provider: str = "disabled"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    voice_map: dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0

    def merged(self, override: dict[str, Any] | None) -> "SpeechProviderConfig":
        """Return a copy with per-user overrides applied (validated)."""
        out = SpeechProviderConfig(
            provider=self.provider,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            voice_map=dict(self.voice_map),
            timeout=self.timeout,
        )
        if not isinstance(override, dict):
            return out
        provider = override.get("provider")
        if isinstance(provider, str) and provider.strip().lower() in TTS_ASR_PROVIDERS:
            out.provider = provider.strip().lower()
        for name in ("base_url", "api_key", "model"):
            value = override.get(name)
            if isinstance(value, str) and value.strip():
                setattr(out, name, value.strip())
        voice_map = override.get("voice_map")
        if isinstance(voice_map, dict):
            out.voice_map = {
                str(k): str(v) for k, v in voice_map.items() if isinstance(v, str)
            }
        voice = override.get("voice")
        if isinstance(voice, str) and voice.strip():
            out.voice_map = {**out.voice_map, "*": voice.strip()}
        timeout = override.get("timeout")
        if isinstance(timeout, (int, float)) and 1 <= float(timeout) <= 600:
            out.timeout = float(timeout)
        return out


@dataclass(slots=True)
class UpstreamEndpoint:
    """One OpenAI-compatible chat-completions endpoint and its parameter dialect.

    The LAN gateway accepts ``thinking:{"type":"disabled"}``; the public gateway
    rejects it with HTTP 400 but accepts ``reasoning_effort:"none"`` — which is
    the default dialect because it works on both (see README §3).
    """

    name: str = "primary"
    base_url: str = ""
    api_key: str = ""
    thinking_mode: str = "reasoning_effort"
    timeout: float = 300.0

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(slots=True)
class SpeechConfig:
    tts: SpeechProviderConfig = field(default_factory=SpeechProviderConfig)
    asr: SpeechProviderConfig = field(default_factory=SpeechProviderConfig)
    ffmpeg_bin: str | None = None
    output_sample_rate: int = 24000
    output_channels: int = 1
    max_tts_chars: int = 6000
    max_asr_bytes: int = 8 * 1024 * 1024
    transcode_timeout: float = 120.0


@dataclass(slots=True)
class SearchConfig:
    """联网搜索 / 链接阅读 configuration (SPEC §3.5)."""

    mode: str = "dgx"  # dgx | official | off
    provider: str = "deepseek"  # retrieval provider
    provider_base_url: str = ""
    provider_api_key: str = ""
    official_base_url: str = "https://api.deepseek.com/anthropic/v1"
    official_api_key: str = ""
    official_model: str = "deepseek-flash"
    timeout: float = 30.0
    fetch_timeout: float = 8.0
    fetch_max_chars: int = 8000
    max_results: int = 6
    max_queries: int = 3
    max_pages_per_turn: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 DGXChat/1.0"
    )


@dataclass(slots=True)
class Settings:
    version: str = VERSION
    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path = DEFAULT_DATA_DIR / "dgxchat.db"
    files_dir: Path = DEFAULT_DATA_DIR / "files"

    host: str = "0.0.0.0"
    port: int = 8439
    cors_origins: list[str] = field(default_factory=lambda: list(DEFAULT_CORS_ORIGINS))
    cors_origin_regex: str = DEFAULT_CORS_ORIGIN_REGEX
    #: built web client (optional); served at "/" with SPA deep-link fallback.
    web_dir: Path = SERVER_DIR / "web"

    # -- upstream (DGX gateway, OpenAI compatible) -------------------------
    upstream_base_url: str = "http://localhost:8041/v1"
    upstream_api_key: str = ""
    upstream_timeout: float = 300.0
    upstream_connect_timeout: float = 10.0
    #: ordered fallbacks tried when the primary is unreachable or rejects a request
    upstream_fallbacks: list[UpstreamEndpoint] = field(default_factory=list)
    model_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODEL_MAP))
    default_max_tokens: int = 8192
    temperature: float | None = None
    top_p: float | None = None
    #: how to force thinking off; measured against both gateways (see README)
    thinking_disable_mode: str = DEFAULT_THINKING_DISABLE_MODE
    log_upstream: bool = False
    ping_interval: float = 10.0

    # -- auth / limits -----------------------------------------------------
    registration_mode: str = "invite"  # invite | closed | open
    allow_open_registration: bool = False  # legacy alias for mode=open
    invite_code: str = ""  # legacy shared code; admin-issued codes are preferred
    password_min_length: int = 6
    docs_enabled: bool = False
    max_upload_bytes: int = 100 * 1024 * 1024
    max_json_body_bytes: int = 8 * 1024 * 1024
    max_image_bytes: int = 20 * 1024 * 1024
    max_document_bytes: int = 8 * 1024 * 1024
    max_document_chars: int = 120_000  # extracted text budget for PDF/DOCX/report files
    user_storage_mb: int = 0  # 0 = unlimited
    scrypt_n: int = 2**14
    token_ttl_days: int = 0  # 0 = never expires

    # -- abuse control (all env tunable) ----------------------------------
    login_max_failures: int = 10
    login_window_seconds: int = 300
    register_max_per_hour: int = 5
    message_rate_per_minute: int = 20
    daily_token_quota: int = 0  # 0 = unlimited
    daily_tts_chars: int = 20000  # 0 = unlimited
    trust_proxy: bool = False

    # -- features ----------------------------------------------------------
    search: SearchConfig = field(default_factory=SearchConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)

    title_max_chars: int = 30
    title_timeout: float = 25.0

    @property
    def upstream_chat_url(self) -> str:
        return f"{self.upstream_base_url.rstrip('/')}/chat/completions"

    def upstream_endpoints(self) -> list[UpstreamEndpoint]:
        """Primary endpoint first, then the configured fallbacks."""
        primary = UpstreamEndpoint(
            name="primary",
            base_url=self.upstream_base_url.rstrip("/"),
            api_key=self.upstream_api_key,
            thinking_mode=self.thinking_disable_mode,
            timeout=self.upstream_timeout,
        )
        endpoints = [primary]
        seen = {primary.base_url}
        for fallback in self.upstream_fallbacks:
            base = (fallback.base_url or "").rstrip("/")
            if not base or base in seen:
                continue
            seen.add(base)
            endpoints.append(
                UpstreamEndpoint(
                    name=fallback.name or f"fallback{len(endpoints)}",
                    base_url=base,
                    api_key=fallback.api_key,
                    thinking_mode=fallback.thinking_mode or self.thinking_disable_mode,
                    timeout=fallback.timeout or self.upstream_timeout,
                )
            )
        return endpoints

    def upstream_model(self, client_model: str) -> str:
        return self.model_map.get(client_model, client_model)

    def resolve_search_api_key(self, user_settings: dict[str, Any] | None) -> str:
        """Per-user search credentials win over env (except for the official key)."""
        if isinstance(user_settings, dict):
            search = user_settings.get("search")
            if isinstance(search, dict):
                key = search.get("api_key")
                if isinstance(key, str) and key.strip():
                    return key.strip()
        return self.search.provider_api_key or self.search.official_api_key


def _normalise_thinking_mode(value: Any, default: str = DEFAULT_THINKING_DISABLE_MODE) -> str:
    if isinstance(value, str) and value.strip().lower() in THINKING_DISABLE_MODES:
        return value.strip().lower()
    return default


def _parse_upstream_fallbacks(default_mode: str, default_timeout: float) -> list[UpstreamEndpoint]:
    """Fallbacks from ``DGX_FALLBACKS`` (JSON list) or singular env vars."""
    out: list[UpstreamEndpoint] = []
    raw = _env_json("DGX_FALLBACKS", None)
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            base = str(item.get("base_url") or item.get("url") or "").strip().rstrip("/")
            if not base:
                continue
            try:
                timeout = float(item.get("timeout") or default_timeout)
            except (TypeError, ValueError):
                timeout = default_timeout
            out.append(
                UpstreamEndpoint(
                    name=str(item.get("name") or f"fallback{index + 1}"),
                    base_url=base,
                    api_key=str(item.get("api_key") or "").strip(),
                    thinking_mode=_normalise_thinking_mode(
                        item.get("thinking_disable_mode"), default_mode
                    ),
                    timeout=timeout,
                )
            )
        return out

    for suffix in ("", "_2", "_3"):
        base = _env(f"DGX_BASE_URL_FALLBACK{suffix}", "").strip().rstrip("/")
        if not base:
            continue
        out.append(
            UpstreamEndpoint(
                name=f"fallback{len(out) + 1}",
                base_url=base,
                api_key=_env(f"DGX_API_KEY_FALLBACK{suffix}", "").strip(),
                thinking_mode=_normalise_thinking_mode(
                    _env(f"DGX_THINKING_DISABLE_MODE_FALLBACK{suffix}", ""), default_mode
                ),
                timeout=_env_float(f"DGX_TIMEOUT_FALLBACK{suffix}", default_timeout),
            )
        )
    return out


def resolve_cors_origins() -> list[str]:
    """Concrete origins by default; ``*`` only when the operator asks for it."""
    raw = os.environ.get("CORS_ALLOW_ORIGINS")
    if raw is None:
        raw = os.environ.get("DGXCHAT_CORS_ORIGINS")
    if raw is None or raw.strip() == "":
        return list(DEFAULT_CORS_ORIGINS)
    items = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if not items:
        return list(DEFAULT_CORS_ORIGINS)
    return items


def resolve_registration_mode() -> str:
    raw = os.environ.get("REGISTRATION_MODE")
    if raw is not None and raw.strip():
        mode = raw.strip().lower()
        return mode if mode in REGISTRATION_MODES else "invite"
    legacy = os.environ.get("DGXCHAT_ALLOW_OPEN_REGISTRATION", "")
    if legacy.strip().lower() in {"1", "true", "yes", "on"}:
        return "open"
    return "invite"


def load_settings(env_file: Path | None = None) -> Settings:
    """Build a :class:`Settings` from the environment (plus optional .env)."""
    _load_dotenv(env_file or (SERVER_DIR / ".env"));

    data_dir = Path(_env("DGXCHAT_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    db_path = Path(_env("DGXCHAT_DB_PATH", str(data_dir / "dgxchat.db"))).expanduser()
    files_dir = Path(_env("DGXCHAT_FILES_DIR", str(data_dir / "files"))).expanduser()

    model_map = dict(DEFAULT_MODEL_MAP)
    raw_map = _env_json("DGX_MODEL_MAP", None)
    if isinstance(raw_map, dict):
        for key, value in raw_map.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                model_map[key] = value.strip()
    flash = _env("DGX_MODEL_FLASH", "").strip()
    if flash:
        model_map["deepseek-flash"] = flash
    pro = _env("DGX_MODEL_PRO", "").strip()
    if pro:
        model_map["deepseek-v4-pro"] = pro

    upstream_base = _env("DGX_BASE_URL", "http://localhost:8041/v1").strip().rstrip("/")
    upstream_timeout = _env_float("DGX_TIMEOUT", 300.0)
    thinking_mode = _normalise_thinking_mode(_env("DGX_THINKING_DISABLE_MODE", ""))

    deepseek_api_key = _env("DEEPSEEK_API_KEY", "").strip()
    deepseek_base = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")

    # --- speech providers -------------------------------------------------
    def speech_provider(kind: str) -> SpeechProviderConfig:
        upper = kind.upper()
        default_provider = "deepseek" if deepseek_api_key else "disabled"
        provider = _env_choice(
            f"{upper}_PROVIDER", default_provider, TTS_ASR_PROVIDERS
        )
        if provider == "deepseek":
            base = _env(f"{upper}_BASE_URL", deepseek_base).strip().rstrip("/")
            key = _env(f"{upper}_API_KEY", deepseek_api_key).strip()
            model = _env(f"{upper}_MODEL", "").strip()
        elif provider == "openai":
            base = _env(
                f"{upper}_BASE_URL",
                "https://api.openai.com/v1",
            ).strip().rstrip("/")
            key = _env(f"{upper}_API_KEY", "").strip()
            model = _env(
                f"{upper}_MODEL",
                "whisper-1" if kind == "asr" else "gpt-4o-mini-tts",
            ).strip()
        elif provider == "deepseek_tools":
            base = _env(f"{upper}_BASE_URL", deepseek_base).strip().rstrip("/")
            key = _env(f"{upper}_API_KEY", deepseek_api_key).strip()
            model = _env(f"{upper}_MODEL", "").strip()
        else:
            base = _env(f"{upper}_BASE_URL", "").strip().rstrip("/")
            key = _env(f"{upper}_API_KEY", "").strip()
            model = _env(f"{upper}_MODEL", "").strip()

        raw_voice_map = _env_json(f"{upper}_VOICE_MAP", None)
        voice_map: dict[str, str] = {}
        if isinstance(raw_voice_map, dict):
            voice_map = {
                str(k): str(v) for k, v in raw_voice_map.items() if isinstance(v, str)
            }
        elif kind == "tts" and provider == "openai":
            # OpenAI voices; the DeepSeek official endpoint uses its own voice ids
            # (stella / tide) which must be forwarded untouched.
            voice_map = {"stella": "alloy", "tide": "echo"}
        return SpeechProviderConfig(
            provider=provider,
            base_url=base,
            api_key=key,
            model=model,
            voice_map=voice_map,
            timeout=_env_float(f"{upper}_TIMEOUT", 120.0),
        )

    ffmpeg = _env("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg") or None

    search_mode = _env_choice("SEARCH_MODE", "dgx", SEARCH_MODES)
    search_provider = _env_choice("SEARCH_PROVIDER", "deepseek", SEARCH_PROVIDERS)
    official_base = _env(
        "DEEPSEEK_ANTHROPIC_BASE_URL", f"{deepseek_base}/anthropic/v1"
    ).strip().rstrip("/")

    settings = Settings(
        data_dir=data_dir,
        db_path=db_path,
        files_dir=files_dir,
        host=_env("DGXCHAT_HOST", "0.0.0.0"),
        port=_env_int("DGXCHAT_PORT", 8439),
        cors_origins=resolve_cors_origins(),
        cors_origin_regex=_env("CORS_ALLOW_ORIGIN_REGEX", DEFAULT_CORS_ORIGIN_REGEX),
        web_dir=Path(_env("WEB_DIR", str(SERVER_DIR / "web"))).expanduser(),
        upstream_base_url=upstream_base,
        upstream_api_key=_env("DGX_API_KEY", "").strip(),
        upstream_timeout=upstream_timeout,
        upstream_connect_timeout=_env_float("DGX_CONNECT_TIMEOUT", 10.0),
        upstream_fallbacks=_parse_upstream_fallbacks(thinking_mode, upstream_timeout),
        model_map=model_map,
        default_max_tokens=_env_int("DGX_MAX_TOKENS", 8192),
        temperature=(
            None
            if _env("DGX_TEMPERATURE", "").strip() == ""
            else _env_float("DGX_TEMPERATURE", 0.7)
        ),
        top_p=(
            None
            if _env("DGX_TOP_P", "").strip() == ""
            else _env_float("DGX_TOP_P", 1.0)
        ),
        thinking_disable_mode=_normalise_thinking_mode(_env("DGX_THINKING_DISABLE_MODE", "")),
        log_upstream=_env_bool("DGX_LOG_UPSTREAM", False),
        ping_interval=_env_float("DGX_PING_INTERVAL", 10.0),
        allow_open_registration=resolve_registration_mode() == "open",
        invite_code=_env("DGXCHAT_INVITE_CODE", "").strip(),
        password_min_length=_env_int("DGXCHAT_PASSWORD_MIN_LENGTH", 6),
        docs_enabled=_env_bool("DGXCHAT_DOCS", False),
        max_upload_bytes=_env_int("DGXCHAT_MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        max_json_body_bytes=_env_int("DGXCHAT_MAX_JSON_BYTES", 8 * 1024 * 1024),
        max_image_bytes=_env_int("DGXCHAT_MAX_IMAGE_BYTES", 20 * 1024 * 1024),
        max_document_bytes=_env_int("DGXCHAT_MAX_DOCUMENT_BYTES", 8 * 1024 * 1024),
        max_document_chars=_env_int("DGXCHAT_MAX_DOCUMENT_CHARS", 120_000),
        user_storage_mb=_env_int("USER_STORAGE_MB", 0),
        scrypt_n=max(2**12, _env_int("DGXCHAT_SCRYPT_N", 2**14)),
        token_ttl_days=_env_int("TOKEN_TTL_DAYS", 0),
        registration_mode=resolve_registration_mode(),
        login_max_failures=_env_int("LOGIN_MAX_FAILURES", 10),
        login_window_seconds=_env_int("LOGIN_WINDOW_SECONDS", 300),
        register_max_per_hour=_env_int("REGISTER_MAX_PER_HOUR", 5),
        message_rate_per_minute=_env_int("MESSAGE_RATE_PER_MINUTE", 20),
        daily_token_quota=_env_int("DAILY_TOKEN_QUOTA", 0),
        daily_tts_chars=_env_int("DAILY_TTS_CHARS", 20000),
        trust_proxy=_env_bool("DGXCHAT_TRUST_PROXY", False),
        title_max_chars=_env_int("DGX_TITLE_MAX_CHARS", 30),
        title_timeout=_env_float("DGX_TITLE_TIMEOUT", 25.0),
        search=SearchConfig(
            mode=search_mode,
            provider=search_provider,
            provider_base_url=_env("SEARCH_BASE_URL", "").strip().rstrip("/"),
            provider_api_key=_env("SEARCH_API_KEY", "").strip(),
            official_base_url=official_base,
            official_api_key=deepseek_api_key,
            official_model=_env("SEARCH_OFFICIAL_MODEL", "deepseek-flash").strip(),
            timeout=_env_float("SEARCH_TIMEOUT", 30.0),
            fetch_timeout=_env_float("SEARCH_FETCH_TIMEOUT", 8.0),
            fetch_max_chars=_env_int("SEARCH_FETCH_MAX_CHARS", 8000),
            max_results=_env_int("SEARCH_MAX_RESULTS", 6),
            max_queries=_env_int("SEARCH_MAX_QUERIES", 3),
            max_pages_per_turn=_env_int("SEARCH_MAX_PAGES", 3),
            user_agent=_env(
                "SEARCH_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 DGXChat/1.0",
            ),
        ),
        speech=SpeechConfig(
            tts=speech_provider("tts"),
            asr=speech_provider("asr"),
            ffmpeg_bin=ffmpeg,
            output_sample_rate=_env_int("TTS_SAMPLE_RATE", 24000),
            output_channels=_env_int("TTS_CHANNELS", 1),
            max_tts_chars=_env_int("TTS_MAX_CHARS", 6000),
            max_asr_bytes=_env_int("ASR_MAX_BYTES", 8 * 1024 * 1024),
            transcode_timeout=_env_float("TTS_TRANSCODE_TIMEOUT", 120.0),
        ),
    )
    return settings


def ensure_directories(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
