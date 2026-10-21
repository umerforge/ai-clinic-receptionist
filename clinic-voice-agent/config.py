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
    "DEEPGRAM_API_KEY",
    "N8N_WEBHOOK_BASE_URL",
    "N8N_WEBHOOK_SECRET",
)


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


def _bool(name: str, default: bool) -> bool:
    raw = _optional(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _timeout() -> float:
    return _float("WEBHOOK_TIMEOUT_SEC", 8.0, low=0.1, high=120.0)


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secret values are excluded from repr()."""

    livekit_url: str = field(repr=False)
    livekit_api_key: str = field(repr=False)
    livekit_api_secret: str = field(repr=False)
    groq_api_key: str = field(repr=False)
    n8n_webhook_base_url: str = field(repr=False)
    n8n_webhook_secret: str = field(repr=False)
    deepgram_api_key: str = field(repr=False)
    groq_model: str = "openai/gpt-oss-20b"
    tts_model: str = "aura-2-thalia-en"
    enable_background_audio: bool = True
    background_ambience_file: str = "assets/hospital-ambience.mp3"
    background_ambience_volume: float = 0.3
    background_mix_volume: float = 0.12
    background_thinking_volume: float = 0.18
    background_thinking_probability: float = 0.55
    test_phone: str = field(default="", repr=False)
    webhook_timeout_sec: float = 8.0


def _load_settings() -> Settings:
    missing = [name for name in _REQUIRED_VARS if not os.getenv(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    return Settings(
        livekit_url=_required("LIVEKIT_URL"),
        livekit_api_key=_required("LIVEKIT_API_KEY"),
        livekit_api_secret=_required("LIVEKIT_API_SECRET"),
        groq_api_key=_required("GROQ_API_KEY"),
        n8n_webhook_base_url=_required("N8N_WEBHOOK_BASE_URL").rstrip("/"),
        n8n_webhook_secret=_required("N8N_WEBHOOK_SECRET"),
        deepgram_api_key=_required("DEEPGRAM_API_KEY"),
        groq_model=_optional("GROQ_MODEL", "openai/gpt-oss-20b"),
        tts_model=_optional("TTS_MODEL", "aura-2-thalia-en"),
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
