"""LiveKit voice agent entry point for the clinic receptionist."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    CloseEvent,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.agents import llm as agents_llm
from livekit.agents.llm import FallbackAdapter
from livekit.agents.voice import ModelSettings
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

    async def llm_node(
        self,
        chat_ctx: agents_llm.ChatContext,
        tools: list[agents_llm.Tool],
        model_settings: ModelSettings,
    ):
        chat_ctx.truncate(max_items=24)
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            yield chunk


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


def _build_llm() -> FallbackAdapter:
    common = {
        "api_key": settings.groq_api_key,
        "max_completion_tokens": 200,
        "parallel_tool_calls": False,
        "max_retries": 0,
    }
    return FallbackAdapter(
        [
            groq.LLM(
                model=settings.groq_model,
                reasoning_effort="low",
                **common,
            ),
            groq.LLM(model=settings.groq_fallback_model, **common),
        ],
        attempt_timeout=4.0,
        max_retry_per_llm=0,
        retry_interval=0.2,
        sticky=True,
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

    logger.info("speech=deepgram stt=nova-3 tts=%s", settings.tts_model)

    agent = ClinicReceptionist(caller_phone=caller_phone)
    stt = _build_stt()
    tts = _build_tts()

    session = AgentSession(
        stt=stt,
        llm=_build_llm(),
        tts=tts,
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            turn_detection=EnglishModel(),
            # Keep turns snappy without cutting off normal pauses.
            endpointing={"min_delay": 0.35, "max_delay": 2.0},
            # Avoid duplicate LLM calls that burn through Groq's TPM limit.
            preemptive_generation={"enabled": False},
        ),
    )

    disconnected = asyncio.Event()
    shutdown_requested = False

    @session.on("close")
    def _on_session_close(event: CloseEvent) -> None:
        logger.info("session closed: %s", event.reason)
        disconnected.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(remote: rtc.RemoteParticipant) -> None:
        if participant is not None and remote.identity == participant.identity:
            logger.info("caller disconnected")
            disconnected.set()

    @ctx.room.on("disconnected")
    def _on_room_disconnected(*_args: object) -> None:
        logger.info("room disconnected")
        disconnected.set()

    async def _on_shutdown(reason: str) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        logger.info("shutdown requested: %s", reason)
        disconnected.set()

    ctx.add_shutdown_callback(_on_shutdown)

    try:
        await session.start(agent=agent, room=ctx.room)
        await session.generate_reply(instructions=GREETING_INSTRUCTIONS)
        await disconnected.wait()
    except Exception:
        logger.exception("voice session failed")
    finally:
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
        if shutdown_requested:
            logger.info("skipping call log because the worker is shutting down")
        else:
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
            shutdown_process_timeout=1.0,
        )
    )
