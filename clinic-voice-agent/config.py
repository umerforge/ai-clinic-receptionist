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
    "N8N_WEBHOOK_URL",
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


def _timeout() -> float:
    return _float("WEBHOOK_TIMEOUT_SEC", 8.0, low=0.1, high=120.0)


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secret values are excluded from repr()."""

    livekit_url: str = field(repr=False)
    livekit_api_key: str = field(repr=False)
    livekit_api_secret: str = field(repr=False)
    groq_api_key: str = field(repr=False)
    n8n_webhook_url: str = field(repr=False)
    n8n_webhook_secret: str = field(repr=False)
    deepgram_api_key: str = field(repr=False)
    groq_model: str = "openai/gpt-oss-20b"
    groq_fallback_model: str = "qwen/qwen3.8-27b"
    tts_model: str = "aura-2-thalia-en"
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
        n8n_webhook_url=_required("N8N_WEBHOOK_URL").rstrip("/"),
        n8n_webhook_secret=_required("N8N_WEBHOOK_SECRET"),
        deepgram_api_key=_required("DEEPGRAM_API_KEY"),
        groq_model=_optional("GROQ_MODEL", "openai/gpt-oss-20b"),
        groq_fallback_model=_optional("GROQ_FALLBACK_MODEL", "qwen/qwen3.8-27b"),
        tts_model=_optional("TTS_MODEL", "aura-2-thalia-en"),
        test_phone=_optional("TEST_PHONE"),
        webhook_timeout_sec=_timeout(),
    )


settings = _load_settings()
