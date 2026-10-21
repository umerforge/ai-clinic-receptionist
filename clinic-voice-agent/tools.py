"""LLM tools that call n8n webhooks.

The agent never talks to Supabase directly. Every tool validates its input,
then POSTs JSON to n8n with the shared secret header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date as date_type
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from livekit import rtc
from livekit.agents import RunContext, function_tool

from config import TIMEZONE, settings

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_CLINIC_INFO_KEYS = {"timings", "address", "fee", "emergency", "closed_days"}


def _system_error() -> dict[str, Any]:
    return {"success": False, "reason": "system_error"}


def _invalid_input(detail: str) -> dict[str, Any]:
    return {"success": False, "reason": "invalid_input", "detail": detail}


def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def normalize_phone(raw: str) -> str | None:
    """Return a Pakistani number in E.164 form, or None if it is invalid."""
    if not isinstance(raw, str):
        return None

    value = re.sub(r"[\s\-().]", "", raw.strip())
    if not value:
        return None

    if value.startswith("00"):
        value = "+" + value[2:]
    elif value.startswith("0"):
        value = "+92" + value[1:]
    elif value.startswith("92") and not value.startswith("+"):
        value = "+" + value

    if not _PHONE_RE.fullmatch(value):
        return None
    return value


def resolve_caller_phone(participant: rtc.RemoteParticipant | None) -> str:
    """Read the SIP caller id, then TEST_PHONE, then fall back to 'unknown'."""
    sip_phone = ""
    if participant is not None:
        sip_phone = participant.attributes.get("sip.phoneNumber", "").strip()
    raw = sip_phone or settings.test_phone
    if not raw:
        return "unknown"
    return normalize_phone(raw) or raw


def _caller_phone(context: RunContext) -> str:
    try:
        agent = context.session.current_agent
        raw = str(getattr(agent, "caller_phone", "") or "unknown")
    except Exception:
        logger.exception("could not read caller phone from the agent")
        return "unknown"
    if raw == "unknown":
        return raw
    return normalize_phone(raw) or raw


def _record_outcome(context: RunContext, outcome: str) -> None:
    try:
        agent = context.session.current_agent
        outcomes = getattr(agent, "call_outcomes", None)
        if isinstance(outcomes, list):
            outcomes.append(outcome)
    except Exception:
        logger.debug("could not record tool outcome", exc_info=True)


def _parse_date(value: str) -> date_type | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_iso_with_offset(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _valid_future_date(value: str) -> tuple[date_type | None, dict[str, Any] | None]:
    parsed = _parse_date(value)
    if parsed is None:
        return None, _invalid_input("date must be YYYY-MM-DD")
    if parsed < _now().date():
        return None, _invalid_input("date is in the past; ask for a future date")
    return parsed, None


def _valid_future_datetime(
    value: str, field: str = "starts_at"
) -> tuple[datetime | None, dict[str, Any] | None]:
    parsed = _parse_iso_with_offset(value)
    if parsed is None:
        return None, _invalid_input(
            f"{field} must be ISO 8601 with an offset, for example "
            "2026-10-26T10:00:00+05:00"
        )
    if parsed <= _now():
        return None, _invalid_input(f"{field} is in the past; ask for a future time")
    return parsed, None


async def post_n8n(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to the n8n webhook. This function never raises and never retries."""
    url = settings.n8n_webhook_url
    body = {**payload, "action": path}
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Secret": settings.n8n_webhook_secret,
    }
    timeout = aiohttp.ClientTimeout(total=settings.webhook_timeout_sec)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body, headers=headers) as response:
                status = response.status
                if status == 401:
                    logger.error(
                        "n8n webhook %s returned 401: N8N_WEBHOOK_SECRET is wrong", path
                    )
                    return _system_error()
                if status != 200:
                    logger.error("n8n webhook %s returned HTTP %s", path, status)
                    return _system_error()

                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                    logger.error("n8n webhook %s returned invalid JSON", path)
                    return _system_error()

                if not isinstance(data, dict):
                    logger.error("n8n webhook %s returned non-object JSON", path)
                    return _system_error()
                return data

    except asyncio.TimeoutError:
        logger.error(
            "n8n webhook %s timed out after %.1f seconds", path, settings.webhook_timeout_sec
        )
    except aiohttp.ClientError as exc:
        logger.error(
            "n8n webhook %s connection error: %s", path, exc.__class__.__name__
        )
    except Exception:
        logger.exception("n8n webhook %s failed unexpectedly", path)

    return _system_error()


@function_tool
async def check_availability(
    context: RunContext, doctor_name: str, date: str
) -> dict[str, Any]:
    """Check open appointment slots for a doctor on a date.

    Args:
        doctor_name: Full doctor name, for example Dr. Ahmed.
        date: Date in YYYY-MM-DD format.
    """
    doctor = (doctor_name or "").strip()
    if not doctor:
        return _invalid_input("doctor_name is required")

    parsed, error = _valid_future_date(date)
    if error:
        return error
    assert parsed is not None

    return await post_n8n(
        "check-availability", {"doctor_name": doctor, "date": parsed.isoformat()}
    )


@function_tool
async def book_appointment(
    context: RunContext,
    patient_name: str,
    doctor_name: str,
    starts_at: str,
    reason: str,
) -> dict[str, Any]:
    """Book an appointment for the caller.

    Args:
        patient_name: Patient's full name.
        doctor_name: Full doctor name, for example Dr. Ahmed.
        starts_at: ISO 8601 start time with +05:00, for example 2026-10-26T10:00:00+05:00.
        reason: Short reason for the visit.
    """
    patient = (patient_name or "").strip()
    doctor = (doctor_name or "").strip()
    if not patient:
        return _invalid_input("patient_name is required")
    if not doctor:
        return _invalid_input("doctor_name is required")

    parsed, error = _valid_future_datetime(starts_at)
    if error:
        return error
    assert parsed is not None

    result = await post_n8n(
        "book-appointment",
        {
            "patient_name": patient,
            "phone": _caller_phone(context),
            "doctor_name": doctor,
            "starts_at": parsed.isoformat(),
            "reason": (reason or "").strip(),
        },
    )
    if result.get("success") is True:
        _record_outcome(context, "booked")
    elif result.get("reason") == "system_error":
        _record_outcome(context, "failed")
    return result


@function_tool
async def cancel_appointment(context: RunContext, date: str) -> dict[str, Any]:
    """Cancel the caller's appointment on a date.

    Args:
        date: Appointment date in YYYY-MM-DD format.
    """
    parsed, error = _valid_future_date(date)
    if error:
        return error
    assert parsed is not None

    result = await post_n8n(
        "cancel-appointment",
        {"phone": _caller_phone(context), "date": parsed.isoformat()},
    )
    if result.get("success") is True:
        _record_outcome(context, "cancelled")
    elif result.get("reason") == "system_error":
        _record_outcome(context, "failed")
    return result


@function_tool
async def get_clinic_info(context: RunContext, key: str) -> dict[str, Any]:
    """Get one piece of clinic information.

    Args:
        key: One of timings, address, fee, emergency, closed_days.
    """
    normalized = (key or "").strip().lower()
    if normalized not in _CLINIC_INFO_KEYS:
        return _invalid_input(
            "key must be one of timings, address, fee, emergency, closed_days"
        )
    return await post_n8n("clinic-info", {"key": normalized})


@function_tool
async def reschedule_appointment(
    context: RunContext, old_date: str, new_starts_at: str
) -> dict[str, Any]:
    """Move the caller's appointment to a new time.

    Args:
        old_date: Existing appointment date in YYYY-MM-DD format.
        new_starts_at: New ISO 8601 start time with +05:00, for example 2026-10-26T10:00:00+05:00.
    """
    old_parsed, error = _valid_future_date(old_date)
    if error:
        return error
    assert old_parsed is not None

    new_parsed, error = _valid_future_datetime(new_starts_at, field="new_starts_at")
    if error:
        return error
    assert new_parsed is not None

    result = await post_n8n(
        "reschedule-appointment",
        {
            "phone": _caller_phone(context),
            "old_date": old_parsed.isoformat(),
            "new_starts_at": new_parsed.isoformat(),
        },
    )
    if result.get("success") is True:
        _record_outcome(context, "booked")
    elif result.get("reason") == "system_error":
        _record_outcome(context, "failed")
    return result


@function_tool
async def request_callback(context: RunContext, reason: str) -> dict[str, Any]:
    """Ask clinic staff to call the caller back.

    Args:
        reason: Short reason why a callback is needed.
    """
    text = (reason or "").strip()
    if not text:
        return _invalid_input("reason is required")

    result = await post_n8n(
        "request-callback", {"phone": _caller_phone(context), "reason": text[:500]}
    )
    if result.get("success") is True:
        _record_outcome(context, "handoff")
    elif result.get("reason") == "system_error":
        _record_outcome(context, "failed")
    return result


async def log_call(
    caller_phone: str, duration_sec: int, outcome: str, transcript: str
) -> None:
    """Best-effort call log. Never raises and waits at most five seconds."""
    payload = {
        "caller_phone": caller_phone,
        "duration_sec": max(0, int(duration_sec)),
        "outcome": outcome,
        "transcript": transcript,
    }
    timeout = min(5.0, settings.webhook_timeout_sec)
    try:
        await asyncio.wait_for(post_n8n("log-call", payload), timeout=timeout)
    except Exception:
        logger.exception("call logging failed")


CLINIC_TOOLS = [
    check_availability,
    book_appointment,
    cancel_appointment,
    get_clinic_info,
    reschedule_appointment,
    request_callback,
]
