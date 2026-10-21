"""Generate English voice samples with the configured Deepgram Aura voice."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents.utils import http_context
from livekit.plugins import deepgram

load_dotenv()

SAMPLES: list[str] = [
    "Good morning, this is the clinic reception. How can I help you?",
    "Dr. Sara has an opening tomorrow at ten thirty in the morning.",
    "Could you please confirm the patient's full name?",
    "Your appointment has been cancelled. Is there anything else I can help with?",
    "Let me check the schedule for you. One moment, please.",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("TTS_MODEL", "aura-2-thalia-en"),
        help="Deepgram Aura voice model (default: TTS_MODEL or aura-2-thalia-en)",
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
        help="Print the samples without calling Deepgram",
    )
    return parser.parse_args()


async def _synthesize() -> None:
    args = _parse_args()
    if args.dry_run:
        print(f"Voice model: {args.model}")
        for index, text in enumerate(SAMPLES, start=1):
            print(f"{index:02d} [en] {text}")
        return

    api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "DEEPGRAM_API_KEY is not set. Add it to .env, or run with --dry-run."
        )

    args.out.mkdir(parents=True, exist_ok=True)
    tts = deepgram.TTS(model=args.model, api_key=api_key)

    failures = 0
    try:
        for index, text in enumerate(SAMPLES, start=1):
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
                print(f"{index:02d} [en] FAILED: {exc}")
                continue
            finally:
                await stream.aclose()

            if not frames:
                failures += 1
                print(f"{index:02d} [en] FAILED: no audio returned")
                continue

            audio = rtc.combine_audio_frames(frames)
            path = args.out / f"{index:02d}_en.wav"
            path.write_bytes(audio.to_wav_bytes())
            elapsed_ms = (time.perf_counter() - started) * 1000
            print(
                f"{index:02d} [en] {path}  "
                f"audio={audio.duration:.2f}s  first_audio={first_audio_ms:.0f}ms  "
                f"total={elapsed_ms:.0f}ms"
            )
    finally:
        await tts.aclose()

    if failures:
        raise SystemExit(f"{failures} sample(s) failed")
    print(f"\nDone: {len(SAMPLES)} samples written to {args.out}/")


async def _main() -> None:
    async with http_context.open():
        await _synthesize()


if __name__ == "__main__":
    asyncio.run(_main())
