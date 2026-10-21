"""LiveKit voice agent entry point for the clinic receptionist."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    CloseEvent,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    UserInputTranscribedEvent,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram, groq, silero, soniox
from livekit.plugins.turn_detector.multilingual import MultilingualModel

import tools
from config import TIMEZONE, settings
from prompts import (
    GREETING_INSTRUCTIONS,
    URDU_GREETING_INSTRUCTIONS,
    build_system_prompt,
)

logger = logging.getLogger("clinic-agent")

STT_TERMS = [
    "Dr. Ahmed",
    "Dr. Sara",
    "Dr. Imran",
    "appointment",
    "reschedule",
    "clinic",
    "token",
    "report",
]

_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def _detect_language(text: str, fallback: str = "ur") -> str:
    if _ARABIC_SCRIPT.search(text):
        return "ur"
    if any(char.isalpha() for char in text):
        return "en"
    return fallback


def _event_language(event: UserInputTranscribedEvent) -> str:
    if event.language:
        code = str(event.language).lower().replace("_", "-").split("-")[0]
        if code in settings.stt_language_hints:
            return code
    return _detect_language(event.transcript)


class ClinicReceptionist(Agent):
    """Clinic agent that keeps the caller's phone outside the LLM arguments."""

    def __init__(self, *, caller_phone: str) -> None:
        now = datetime.now(ZoneInfo(TIMEZONE))
        super().__init__(
            instructions=build_system_prompt(now, supports_urdu=settings.supports_urdu),
            tools=tools.CLINIC_TOOLS,
        )
        self.caller_phone = caller_phone
        self.call_outcomes: list[str] = []


def prewarm(proc: JobProcess) -> None:
    """Load the VAD once per worker process."""
    proc.userdata["vad"] = silero.VAD.load()


def _build_stt() -> deepgram.STT | soniox.STT:
    if settings.speech_provider == "soniox":
        return soniox.STT(
            api_key=settings.soniox_api_key,
            params=soniox.STTOptions(
                model=settings.soniox_stt_model,
                language_hints=list(settings.stt_language_hints),
                enable_language_identification=True,
                max_endpoint_delay_ms=settings.soniox_endpoint_delay_ms,
                endpoint_sensitivity=settings.soniox_endpoint_sensitivity,
                endpoint_latency_adjustment_level=settings.soniox_endpoint_latency_level,
                context=soniox.ContextObject(terms=STT_TERMS),
            ),
        )
    return deepgram.STT(
        model="nova-3",
        language="multi",
        api_key=settings.deepgram_api_key,
    )


def _build_tts() -> deepgram.TTS | soniox.TTS:
    if settings.speech_provider == "soniox":
        return soniox.TTS(
            model=settings.soniox_tts_model,
            language="ur",
            voice=settings.soniox_voice,
            speed=settings.tts_speed,
            reduce_silence=True,
            api_key=settings.soniox_api_key,
        )
    return deepgram.TTS(
        model=settings.tts_model,
        api_key=settings.deepgram_api_key,
    )


def _ambient_source() -> str | BuiltinAudioClip:
    path = Path(settings.background_ambience_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.is_file():
        logger.warning(
            "background ambience file not found: %s; using built-in office ambience",
            path,
        )
        return BuiltinAudioClip.OFFICE_AMBIENCE
    return str(path)


def _ambient_file() -> Path:
    source = _ambient_source()
    if isinstance(source, BuiltinAudioClip):
        return Path(source.path())
    return Path(source)


class ConsoleAmbience:
    """Play the ambience through local speakers so console mode is testable."""

    def __init__(self, path: Path, volume: float) -> None:
        self._path = path
        self._volume = volume
        self._stream = None
        self._audio = None
        self._position = 0

    async def start(self) -> None:
        try:
            import av
            import numpy as np
            import sounddevice as sd

            container = av.open(str(self._path))
            stream = container.streams.audio[0]
            sample_rate = stream.rate
            frames = list(container.decode(audio=0))
            container.close()
            if not frames:
                raise RuntimeError("ambience file has no audio")
            audio = np.concatenate(
                [frame.to_ndarray().astype(np.float32) for frame in frames], axis=1
            ).T
            audio *= self._volume
            self._audio = audio

            def callback(outdata, frame_count, _time, _status) -> None:
                written = 0
                remaining = frame_count
                while remaining > 0:
                    available = len(self._audio) - self._position
                    take = min(available, remaining)
                    outdata[written : written + take] = self._audio[
                        self._position : self._position + take
                    ]
                    self._position = (self._position + take) % len(self._audio)
                    written += take
                    remaining -= take

            self._stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=audio.shape[1],
                dtype="float32",
                callback=callback,
            )
            self._stream.start()
            logger.info(
                "console ambience playing locally: %s (volume %.2f)",
                self._path,
                self._volume,
            )
        except Exception:
            logger.warning(
                "could not play console ambience through local speakers",
                exc_info=True,
            )
            await self.aclose()

    async def aclose(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.abort(ignore_errors=True)
            finally:
                stream.close(ignore_errors=True)


def _build_background_audio() -> BackgroundAudioPlayer | None:
    if not settings.enable_background_audio:
        return None
    return BackgroundAudioPlayer(
        ambient_sound=AudioConfig(
            _ambient_source(),
            volume=settings.background_ambience_volume,
            fade_in=1.5,
            fade_out=1.0,
        ),
        thinking_sound=[
            AudioConfig(
                BuiltinAudioClip.KEYBOARD_TYPING,
                volume=settings.background_thinking_volume,
                probability=settings.background_thinking_probability,
                fade_in=0.2,
                fade_out=0.2,
            ),
            AudioConfig(
                BuiltinAudioClip.KEYBOARD_TYPING2,
                volume=settings.background_thinking_volume,
                probability=settings.background_thinking_probability,
                fade_in=0.2,
                fade_out=0.2,
            ),
        ],
    )


def _build_transcript(session: AgentSession) -> str:
    lines: list[str] = []
    for message in session.history.messages():
        text = (message.text_content or "").strip()
        if not text:
            continue
        if message.role == "user":
            speaker = "Patient"
        elif message.role == "assistant":
            speaker = "Receptionist"
        else:
            continue
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _decide_outcome(outcomes: list[str]) -> str:
    if "booked" in outcomes:
        return "booked"
    if "cancelled" in outcomes:
        return "cancelled"
    if "handoff" in outcomes:
        return "handoff"
    if "failed" in outcomes:
        return "failed"
    return "info"


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    # Console mode uses a fake job with no remote caller. Real rooms wait for
    # the SIP or browser participant so we can read sip.phoneNumber.
    participant = None if ctx.is_fake_job else await ctx.wait_for_participant()
    caller_phone = tools.resolve_caller_phone(participant)
    started_at = time.monotonic()

    if settings.speech_provider_requested == "auto" and not settings.supports_urdu:
        logger.warning(
            "SONIOX_API_KEY is not set. Falling back to Deepgram English-only speech; "
            "Urdu and mixed-language replies are disabled. Set SONIOX_API_KEY to enable them."
        )

    logger.info(
        "speech provider=%s stt=%s tts=%s voice=%s",
        settings.speech_provider,
        settings.soniox_stt_model if settings.supports_urdu else "nova-3",
        settings.soniox_tts_model if settings.supports_urdu else settings.tts_model,
        settings.soniox_voice if settings.supports_urdu else "aura",
    )

    agent = ClinicReceptionist(caller_phone=caller_phone)
    stt = _build_stt()
    tts = _build_tts()
    soniox_tts = tts if isinstance(tts, soniox.TTS) else None
    console_ambience = None
    background_audio = None
    if settings.enable_background_audio:
        if ctx.is_fake_job:
            console_ambience = ConsoleAmbience(
                _ambient_file(), settings.background_ambience_volume
            )
        else:
            background_audio = _build_background_audio()

    session = AgentSession(
        stt=stt,
        llm=groq.LLM(model=settings.groq_model, api_key=settings.groq_api_key),
        tts=tts,
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            turn_detection=MultilingualModel(),
            # Keep turns snappy without cutting off normal pauses.
            endpointing={"min_delay": 0.35, "max_delay": 2.0},
            # Speculative LLM call: lower perceived latency at the cost of some tokens.
            preemptive_generation={"enabled": True, "preemptive_tts": False},
        ),
    )

    disconnected = asyncio.Event()
    tts_language = ["ur"]

    @session.on("close")
    def _on_session_close(event: CloseEvent) -> None:
        logger.info("session closed: %s", event.reason)
        disconnected.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(remote: rtc.RemoteParticipant) -> None:
        if participant is not None and remote.identity == participant.identity:
            logger.info("caller disconnected")
            disconnected.set()

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event: UserInputTranscribedEvent) -> None:
        if not event.is_final or soniox_tts is None:
            return
        language = _event_language(event)
        if language == tts_language[0]:
            return
        soniox_tts.update_options(language=language)
        tts_language[0] = language
        logger.info("tts language switched to %s", language)

    try:
        await session.start(agent=agent, room=ctx.room)
        if console_ambience is not None:
            await console_ambience.start()
        elif background_audio is not None:
            await background_audio.start(room=ctx.room, agent_session=session)
        greeting = (
            URDU_GREETING_INSTRUCTIONS
            if settings.supports_urdu
            else GREETING_INSTRUCTIONS
        )
        await session.generate_reply(instructions=greeting)
        await disconnected.wait()
    except Exception:
        logger.exception("voice session failed")
    finally:
        if console_ambience is not None:
            try:
                await console_ambience.aclose()
            except Exception:
                logger.debug("console ambience was already closed", exc_info=True)
        if background_audio is not None:
            try:
                await background_audio.aclose()
            except Exception:
                logger.debug("background audio was already closed", exc_info=True)
        try:
            await session.aclose()
        except Exception:
            logger.debug("session was already closed", exc_info=True)

        duration_sec = int(max(0.0, time.monotonic() - started_at))
        try:
            transcript = _build_transcript(session)
            outcome = _decide_outcome(agent.call_outcomes)
        except Exception:
            logger.exception("could not build the call report")
            transcript = ""
            outcome = "info"
        try:
            await tools.log_call(
                caller_phone=caller_phone,
                duration_sec=duration_sec,
                outcome=outcome,
                transcript=transcript,
            )
        except Exception:
            logger.exception("call logging failed")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
