"""Load environment settings once and expose them as a typed object."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

TIMEZONE = "Asia/Karachi"

_REQUIRED_VARS = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GROQ_API_KEY",
    "N8N_WEBHOOK_BASE_URL",
    "N8N_WEBHOOK_SECRET",
)

_SPEECH_PROVIDERS = ("auto", "soniox", "deepgram")


class ConfigError(RuntimeError):
    """Raised when the environment is incomplete."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill in all required values."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _float(name: str, default: float, *, low: float, high: float) -> float:
    raw = _optional(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number between {low} and {high}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def _int(name: str, default: int, *, low: int, high: int) -> int:
    raw = _optional(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer between {low} and {high}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _optional(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _timeout() -> float:
    return _float("WEBHOOK_TIMEOUT_SEC", 8.0, low=0.1, high=120.0)


def _language_hints() -> tuple[str, ...]:
    raw = _optional("STT_LANGUAGE_HINTS", "ur,en")
    hints = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not hints:
        raise ConfigError("STT_LANGUAGE_HINTS must contain at least one language code")
    return hints


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secret values are excluded from repr()."""

    livekit_url: str = field(repr=False)
    livekit_api_key: str = field(repr=False)
    livekit_api_secret: str = field(repr=False)
    groq_api_key: str = field(repr=False)
    n8n_webhook_base_url: str = field(repr=False)
    n8n_webhook_secret: str = field(repr=False)
    deepgram_api_key: str = field(default="", repr=False)
    soniox_api_key: str = field(default="", repr=False)
    speech_provider: str = "deepgram"
    speech_provider_requested: str = "auto"
    groq_model: str = "openai/gpt-oss-20b"
    tts_model: str = "aura-2-thalia-en"
    soniox_stt_model: str = "stt-rt-v5"
    soniox_tts_model: str = "tts-rt-v2"
    soniox_voice: str = "Maya"
    stt_language_hints: tuple[str, ...] = ("ur", "en")
    soniox_endpoint_delay_ms: int = 800
    soniox_endpoint_sensitivity: float = 0.3
    soniox_endpoint_latency_level: int = 1
    tts_speed: float = 1.0
    enable_background_audio: bool = True
    background_ambience_file: str = "assets/hospital-ambience.mp3"
    background_ambience_volume: float = 0.3
    background_mix_volume: float = 0.12
    background_thinking_volume: float = 0.18
    background_thinking_probability: float = 0.55
    test_phone: str = field(default="", repr=False)
    webhook_timeout_sec: float = 8.0

    @property
    def supports_urdu(self) -> bool:
        return self.speech_provider == "soniox"


def _resolve_provider() -> tuple[str, str]:
    requested = _optional("SPEECH_PROVIDER", "auto").lower()
    if requested not in _SPEECH_PROVIDERS:
        raise ConfigError(
            "SPEECH_PROVIDER must be one of: " + ", ".join(_SPEECH_PROVIDERS)
        )

    soniox_key = _optional("SONIOX_API_KEY")
    deepgram_key = _optional("DEEPGRAM_API_KEY")

    if requested == "auto":
        provider = "soniox" if soniox_key else "deepgram"
    else:
        provider = requested

    if provider == "soniox" and not soniox_key:
        raise ConfigError(
            "SPEECH_PROVIDER=soniox requires SONIOX_API_KEY. "
            "Use SPEECH_PROVIDER=auto to fall back to Deepgram English-only speech."
        )
    if provider == "deepgram" and not deepgram_key:
        raise ConfigError(
            "Deepgram speech requires DEEPGRAM_API_KEY. "
            "Set it, or set SONIOX_API_KEY and SPEECH_PROVIDER=auto."
        )
    return requested, provider


def _load_settings() -> Settings:
    missing = [name for name in _REQUIRED_VARS if not os.getenv(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    requested_provider, provider = _resolve_provider()

    return Settings(
        livekit_url=_required("LIVEKIT_URL"),
        livekit_api_key=_required("LIVEKIT_API_KEY"),
        livekit_api_secret=_required("LIVEKIT_API_SECRET"),
        groq_api_key=_required("GROQ_API_KEY"),
        n8n_webhook_base_url=_required("N8N_WEBHOOK_BASE_URL").rstrip("/"),
        n8n_webhook_secret=_required("N8N_WEBHOOK_SECRET"),
        deepgram_api_key=_optional("DEEPGRAM_API_KEY"),
        soniox_api_key=_optional("SONIOX_API_KEY"),
        speech_provider=provider,
        speech_provider_requested=requested_provider,
        groq_model=_optional("GROQ_MODEL", "openai/gpt-oss-20b"),
        tts_model=_optional("TTS_MODEL", "aura-2-thalia-en"),
        soniox_stt_model=_optional("SONIOX_STT_MODEL", "stt-rt-v5"),
        soniox_tts_model=_optional("SONIOX_TTS_MODEL", "tts-rt-v2"),
        soniox_voice=_optional("SONIOX_VOICE", "Maya"),
        stt_language_hints=_language_hints(),
        soniox_endpoint_delay_ms=_int(
            "SONIOX_ENDPOINT_DELAY_MS", 800, low=500, high=3000
        ),
        soniox_endpoint_sensitivity=_float(
            "SONIOX_ENDPOINT_SENSITIVITY", 0.3, low=-1.0, high=1.0
        ),
        soniox_endpoint_latency_level=_int(
            "SONIOX_ENDPOINT_LATENCY_LEVEL", 1, low=0, high=3
        ),
        tts_speed=_float("TTS_SPEED", 1.0, low=0.7, high=1.3),
        enable_background_audio=_bool("ENABLE_BACKGROUND_AUDIO", True),
        background_ambience_file=_optional(
            "BACKGROUND_AMBIENCE_FILE", "assets/hospital-ambience.mp3"
        ),
        background_ambience_volume=_float(
            "BACKGROUND_AMBIENCE_VOLUME", 0.3, low=0.0, high=1.0
        ),
        background_mix_volume=_float(
            "BACKGROUND_MIX_VOLUME", 0.12, low=0.0, high=1.0
        ),
        background_thinking_volume=_float(
            "BACKGROUND_THINKING_VOLUME", 0.18, low=0.0, high=1.0
        ),
        background_thinking_probability=_float(
            "BACKGROUND_THINKING_PROBABILITY", 0.55, low=0.0, high=1.0
        ),
        test_phone=_optional("TEST_PHONE"),
        webhook_timeout_sec=_timeout(),
    )


settings = _load_settings()
