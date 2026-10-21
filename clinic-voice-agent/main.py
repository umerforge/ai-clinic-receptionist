"""LiveKit voice agent entry point for the clinic receptionist."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AgentStateChangedEvent,
    BuiltinAudioClip,
    CloseEvent,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.utils.audio import AudioByteStream
from livekit.agents.voice import io
from livekit.plugins import deepgram, groq, silero
from livekit.plugins.turn_detector.english import EnglishModel

import tools
from config import TIMEZONE, settings
from prompts import GREETING_INSTRUCTIONS, build_system_prompt

logger = logging.getLogger("clinic-agent")

class ClinicReceptionist(Agent):
    """Clinic agent that keeps the caller's phone outside the LLM arguments."""

    def __init__(self, *, caller_phone: str) -> None:
        now = datetime.now(ZoneInfo(TIMEZONE))
        super().__init__(
            instructions=build_system_prompt(now),
            tools=tools.CLINIC_TOOLS,
        )
        self.caller_phone = caller_phone
        self.call_outcomes: list[str] = []


def prewarm(proc: JobProcess) -> None:
    """Load the VAD once per worker process."""
    proc.userdata["vad"] = silero.VAD.load()


def _build_stt() -> deepgram.STT:
    return deepgram.STT(
        model="nova-3",
        language="en",
        api_key=settings.deepgram_api_key,
    )


def _build_tts() -> deepgram.TTS:
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


class AmbientAudioOutput(io.AudioOutput):
    """Publish agent speech and clinic ambience on one audio track."""

    def __init__(
        self,
        room: rtc.Room,
        *,
        ambience_path: Path,
        ambience_volume: float,
        thinking_paths: list[Path],
        thinking_volume: float,
        thinking_probability: float,
        sample_rate: int = 24000,
    ) -> None:
        super().__init__(
            label="clinic-ambient",
            capabilities=io.AudioOutputCapabilities(pause=True),
            sample_rate=sample_rate,
        )
        self._room = room
        self._ambience_path = ambience_path
        self._ambience_volume = ambience_volume
        self._thinking_paths = thinking_paths
        self._thinking_volume = thinking_volume
        self._thinking_probability = thinking_probability
        self._sample_rate = sample_rate
        self._frame_samples = sample_rate // 50
        self._source = rtc.AudioSource(sample_rate, 1, queue_size_ms=200)
        self._publication: rtc.LocalTrackPublication | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._ambient = np.empty(0, dtype=np.int16)
        self._ambient_position = 0
        self._thinking: list[np.ndarray] = []
        self._effect: np.ndarray | None = None
        self._effect_position = 0
        self._tts_bstream = AudioByteStream(
            sample_rate, 1, samples_per_channel=self._frame_samples
        )
        self._tts_queue: deque[rtc.AudioFrame] = deque()
        self._resampler: rtc.AudioResampler | None = None
        self._resampler_input_rate: int | None = None
        self._tts_played = 0.0
        self._segment_started = False
        self._captured_segment = False
        self._flush_pending = False
        self._paused = False
        self._closed = False

    @staticmethod
    def _decode_audio(path: Path, volume: float, sample_rate: int) -> np.ndarray:
        import av

        container = av.open(str(path))
        try:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=sample_rate
            )
            chunks: list[np.ndarray] = []
            for frame in container.decode(stream):
                for output in resampler.resample(frame):
                    chunks.append(output.to_ndarray().reshape(-1))
            for output in resampler.resample(None):
                chunks.append(output.to_ndarray().reshape(-1))
        finally:
            container.close()
        if not chunks:
            raise RuntimeError(f"audio file has no samples: {path}")
        audio = np.concatenate(chunks).astype(np.float32) * volume
        return np.clip(audio, -32768, 32767).astype(np.int16)

    async def start(self) -> None:
        self._ambient = self._decode_audio(
            self._ambience_path, self._ambience_volume, self._sample_rate
        )
        for path in self._thinking_paths:
            try:
                self._thinking.append(
                    self._decode_audio(path, self._thinking_volume, self._sample_rate)
                )
            except Exception:
                logger.warning("could not load thinking sound: %s", path, exc_info=True)
        track = rtc.LocalAudioTrack.create_audio_track("clinic_voice", self._source)
        self._publication = await self._room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        try:
            await asyncio.wait_for(self._publication.wait_for_subscription(), 5.0)
        except asyncio.TimeoutError:
            logger.info("no subscriber for the voice track yet; ambient will start after subscribe")
        self._pump_task = asyncio.create_task(self._pump())
        logger.info(
            "clinic ambience mixed into the voice track: %s (volume %.2f)",
            self._ambience_path,
            self._ambience_volume,
        )

    @staticmethod
    def _ensure_mono(frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if frame.num_channels == 1:
            return frame
        samples = np.frombuffer(frame.data, dtype=np.int16).reshape(
            -1, frame.num_channels
        )
        mono = samples.mean(axis=1).astype(np.int16)
        return rtc.AudioFrame(
            mono.tobytes(), frame.sample_rate, 1, mono.shape[0]
        )

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        self._captured_segment = True
        if self._closed:
            return
        mono = self._ensure_mono(frame)
        frames = [mono]
        if mono.sample_rate != self._sample_rate:
            if (
                self._resampler is None
                or self._resampler_input_rate != mono.sample_rate
            ):
                self._resampler = rtc.AudioResampler(
                    mono.sample_rate, self._sample_rate, num_channels=1
                )
                self._resampler_input_rate = mono.sample_rate
            frames = self._resampler.push(mono)
        for output in frames:
            for chunk in self._tts_bstream.push(output.data):
                self._tts_queue.append(chunk)

    def flush(self) -> None:
        super().flush()
        if self._resampler is not None:
            for output in self._resampler.flush():
                for chunk in self._tts_bstream.push(output.data):
                    self._tts_queue.append(chunk)
            self._resampler = None
            self._resampler_input_rate = None
        for chunk in self._tts_bstream.flush():
            self._tts_queue.append(chunk)
        if self._captured_segment:
            self._flush_pending = True
            self._maybe_finish_segment()

    def clear_buffer(self) -> None:
        super().clear_buffer()
        self._tts_queue.clear()
        self._tts_bstream.clear()
        self._resampler = None
        self._resampler_input_rate = None
        self._flush_pending = False
        if self._captured_segment:
            self._captured_segment = False
            self._segment_started = False
            played = self._tts_played
            self._tts_played = 0.0
            self.on_playback_finished(
                playback_position=played, interrupted=True
            )

    def pause(self) -> None:
        super().pause()
        self._paused = True

    def resume(self) -> None:
        super().resume()
        self._paused = False

    def play_thinking(self) -> None:
        if not self._thinking or random.random() > self._thinking_probability:
            return
        self._effect = random.choice(self._thinking)
        self._effect_position = 0

    def _maybe_finish_segment(self) -> None:
        if (
            not self._flush_pending
            or self._tts_queue
            or not self._segment_started
        ):
            return
        self._flush_pending = False
        self._segment_started = False
        self._captured_segment = False
        played = self._tts_played
        self._tts_played = 0.0
        self.on_playback_finished(playback_position=played, interrupted=False)

    def _next_ambient(self) -> np.ndarray:
        output = np.empty(self._frame_samples, dtype=np.int16)
        written = 0
        while written < self._frame_samples:
            take = min(
                self._frame_samples - written,
                len(self._ambient) - self._ambient_position,
            )
            start = self._ambient_position
            output[written : written + take] = self._ambient[start : start + take]
            written += take
            self._ambient_position = (self._ambient_position + take) % len(
                self._ambient
            )
        return output

    def _next_effect(self) -> np.ndarray | None:
        if self._effect is None:
            return None
        remaining = len(self._effect) - self._effect_position
        take = min(self._frame_samples, remaining)
        output = self._effect[
            self._effect_position : self._effect_position + take
        ]
        self._effect_position += take
        if self._effect_position >= len(self._effect):
            self._effect = None
            self._effect_position = 0
        return output

    def _mix(
        self, tts: rtc.AudioFrame | None, effect: np.ndarray | None
    ) -> rtc.AudioFrame:
        ambient = self._next_ambient().astype(np.float32)
        if tts is not None or effect is not None:
            ambient *= 0.5
        mixed = ambient.astype(np.int32)
        if tts is not None:
            voice = np.frombuffer(tts.data, dtype=np.int16)
            mixed[: len(voice)] += voice
        if effect is not None:
            mixed[: len(effect)] += effect
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        return rtc.AudioFrame(
            mixed.tobytes(), self._sample_rate, 1, self._frame_samples
        )

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while not self._closed:
            next_at += 1 / 50
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_at = loop.time()
            if self._closed:
                break
            tts = None if self._paused else (
                self._tts_queue.popleft() if self._tts_queue else None
            )
            effect = None if self._paused else self._next_effect()
            if tts is not None and not self._segment_started:
                self._segment_started = True
                self.on_playback_started(created_at=time.time())
            try:
                await self._source.capture_frame(self._mix(tts, effect))
            except Exception:
                logger.exception("ambient audio output stopped")
                break
            if tts is not None:
                self._tts_played += tts.duration
            self._maybe_finish_segment()

    async def aclose(self) -> None:
        self._closed = True
        task = self._pump_task
        self._pump_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        publication = self._publication
        self._publication = None
        if publication is not None:
            with suppress(Exception):
                await self._room.local_participant.unpublish_track(
                    publication.sid
                )
        with suppress(Exception):
            await self._source.aclose()


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

    logger.info("speech=deepgram stt=nova-3 tts=%s", settings.tts_model)

    agent = ClinicReceptionist(caller_phone=caller_phone)
    stt = _build_stt()
    tts = _build_tts()
    console_ambience = None
    ambient_output = None
    if settings.enable_background_audio and ctx.is_fake_job:
        console_ambience = ConsoleAmbience(
            _ambient_file(), settings.background_ambience_volume
        )

    session = AgentSession(
        stt=stt,
        llm=groq.LLM(model=settings.groq_model, api_key=settings.groq_api_key),
        tts=tts,
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            turn_detection=EnglishModel(),
            # Keep turns snappy without cutting off normal pauses.
            endpointing={"min_delay": 0.35, "max_delay": 2.0},
            # Speculative LLM call: lower perceived latency at the cost of some tokens.
            preemptive_generation={"enabled": True, "preemptive_tts": False},
        ),
    )

    if settings.enable_background_audio and not ctx.is_fake_job:
        ambient_output = AmbientAudioOutput(
            ctx.room,
            ambience_path=_ambient_file(),
            ambience_volume=settings.background_mix_volume,
            thinking_paths=[
                Path(BuiltinAudioClip.KEYBOARD_TYPING.path()),
                Path(BuiltinAudioClip.KEYBOARD_TYPING2.path()),
            ],
            thinking_volume=settings.background_thinking_volume,
            thinking_probability=settings.background_thinking_probability,
        )
        session.output.audio = ambient_output

    disconnected = asyncio.Event()

    @session.on("close")
    def _on_session_close(event: CloseEvent) -> None:
        logger.info("session closed: %s", event.reason)
        disconnected.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(remote: rtc.RemoteParticipant) -> None:
        if participant is not None and remote.identity == participant.identity:
            logger.info("caller disconnected")
            disconnected.set()

    @session.on("agent_state_changed")
    def _on_agent_state_changed(event: AgentStateChangedEvent) -> None:
        if ambient_output is not None and event.new_state == "thinking":
            ambient_output.play_thinking()

    try:
        await session.start(agent=agent, room=ctx.room)
        if console_ambience is not None:
            await console_ambience.start()
        elif ambient_output is not None:
            await ambient_output.start()
        await session.generate_reply(instructions=GREETING_INSTRUCTIONS)
        await disconnected.wait()
    except Exception:
        logger.exception("voice session failed")
    finally:
        if console_ambience is not None:
            try:
                await console_ambience.aclose()
            except Exception:
                logger.debug("console ambience was already closed", exc_info=True)
        if ambient_output is not None:
            try:
                await ambient_output.aclose()
            except Exception:
                logger.debug("ambient output was already closed", exc_info=True)
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
