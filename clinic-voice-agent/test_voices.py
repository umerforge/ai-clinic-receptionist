"""Generate Urdu and English voice samples with the configured Soniox voice."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.plugins import soniox

load_dotenv()

SAMPLES: list[tuple[str, str]] = [
    (
        "ur",
        "السلام علیکم، یہ کلینک ریسپشن ہے۔ میں آپ کی کیا مدد کر سکتی ہوں؟",
    ),
    (
        "ur",
        "ڈاکٹر احمد کی کل صبح دس بجے اپائنٹمنٹ دستیاب ہے۔",
    ),
    (
        "ur",
        "کیا آپ اپنا نام اور فون نمبر بتا سکتے ہیں؟",
    ),
    (
        "ur",
        "آپ کی اپائنٹمنٹ کینسل کر دی گئی ہے۔",
    ),
    (
        "ur",
        "براہ کرم ایک لمحہ انتظار کریں، میں شیڈول چیک کر رہی ہوں۔",
    ),
    (
        "en",
        "Good morning, this is the clinic reception. How can I help you?",
    ),
    (
        "en",
        "Dr. Sara has an opening tomorrow at ten thirty in the morning.",
    ),
    (
        "en",
        "Could you please confirm the patient's full name?",
    ),
    (
        "en",
        "Your appointment has been cancelled. Is there anything else I can help with?",
    ),
    (
        "en",
        "Let me check the schedule for you. One moment, please.",
    ),
    (
        "ur",
        "جی، Dr. Ahmed کی appointment کل صبح available ہے۔",
    ),
    (
        "ur",
        "آپ کا appointment confirm ہو گیا ہے، آپ کو SMS بھی آ جائے گا۔",
    ),
    (
        "ur",
        "کوئی بات نہیں، میں آپ کا callback request لگا دیتی ہوں۔",
    ),
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voice",
        default=os.getenv("SONIOX_VOICE", "Maya"),
        help="Soniox voice name (default: SONIOX_VOICE or Maya)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("SONIOX_TTS_MODEL", "tts-rt-v2"),
        help="Soniox TTS model (default: SONIOX_TTS_MODEL or tts-rt-v2)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.getenv("TTS_SPEED", "1.0")),
        help="Speaking rate from 0.7 to 1.3 (default: TTS_SPEED or 1.0)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("voice_tests"),
        help="Output directory (default: voice_tests)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the samples without calling Soniox",
    )
    return parser.parse_args()


async def _synthesize() -> None:
    args = _parse_args()
    if args.dry_run:
        print(f"Voice: {args.voice}  Model: {args.model}  Speed: {args.speed}")
        for index, (language, text) in enumerate(SAMPLES, start=1):
            print(f"{index:02d} [{language}] {text}")
        return

    api_key = os.getenv("SONIOX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "SONIOX_API_KEY is not set. Add it to .env, or run with --dry-run."
        )

    args.out.mkdir(parents=True, exist_ok=True)
    tts = soniox.TTS(
        model=args.model,
        language="ur",
        voice=args.voice,
        speed=args.speed,
        reduce_silence=True,
        api_key=api_key,
    )

    failures = 0
    try:
        for index, (language, text) in enumerate(SAMPLES, start=1):
            tts.update_options(language=language)
            started = time.perf_counter()
            stream = tts.synthesize(text)
            frames: list[rtc.AudioFrame] = []
            first_audio_ms: float | None = None
            try:
                async for event in stream:
                    if first_audio_ms is None:
                        first_audio_ms = (time.perf_counter() - started) * 1000
                    frames.append(event.frame)
            except Exception as exc:
                failures += 1
                print(f"{index:02d} [{language}] FAILED: {exc}")
                continue
            finally:
                await stream.aclose()

            if not frames:
                failures += 1
                print(f"{index:02d} [{language}] FAILED: no audio returned")
                continue

            audio = rtc.combine_audio_frames(frames)
            path = args.out / f"{index:02d}_{language}.wav"
            path.write_bytes(audio.to_wav_bytes())
            elapsed_ms = (time.perf_counter() - started) * 1000
            print(
                f"{index:02d} [{language}] {path}  "
                f"audio={audio.duration:.2f}s  first_audio={first_audio_ms:.0f}ms  "
                f"total={elapsed_ms:.0f}ms"
            )
    finally:
        await tts.aclose()

    if failures:
        raise SystemExit(f"{failures} sample(s) failed")
    print(f"\nDone: {len(SAMPLES)} samples written to {args.out}/")


if __name__ == "__main__":
    asyncio.run(_synthesize())
