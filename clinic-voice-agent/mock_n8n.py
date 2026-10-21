"""Tiny local fake of the n8n webhooks, for testing before n8n exists."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

TIMEZONE = "Asia/Karachi"
PORT = 5678
SECRET = os.getenv("N8N_WEBHOOK_SECRET", "").strip() or "dev-secret"

CLINIC_INFO = {
    "timings": "Monday to Saturday, 9:00 AM to 6:00 PM.",
    "address": "12 Main Boulevard, Gulberg, Lahore.",
    "fee": "A general consultation is 2,000 rupees.",
    "emergency": "For an emergency, go to the nearest hospital or call 1122.",
    "closed_days": "The clinic is closed on Sunday.",
}


def _tomorrow_slots() -> list[str]:
    tomorrow = datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=1)
    return [
        f"{tomorrow.isoformat()}T09:00:00+05:00",
        f"{tomorrow.isoformat()}T10:00:00+05:00",
        f"{tomorrow.isoformat()}T11:00:00+05:00",
    ]


async def _payload(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@web.middleware
async def log_and_auth(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    print(f"[mock-n8n] {request.method} {request.path}")
    if request.headers.get("X-Webhook-Secret") != SECRET:
        print("[mock-n8n] rejected: wrong X-Webhook-Secret")
        return web.json_response(
            {"success": False, "reason": "unauthorized"}, status=401
        )
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:
        print(f"[mock-n8n] handler error: {exc.__class__.__name__}")
        return web.json_response(
            {"success": False, "reason": "system_error"}, status=500
        )


async def check_availability(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    return web.json_response({"success": True, "slots": _tomorrow_slots()})


async def book_appointment(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    try:
        starts_at = datetime.fromisoformat(str(body.get("starts_at", "")))
    except ValueError:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    if starts_at.hour == 10:
        return web.json_response({"success": False, "reason": "slot_taken"})
    return web.json_response(
        {
            "success": True,
            "appointment_id": "mock-appt-1001",
            "doctor": body.get("doctor_name", "Dr. Ahmed"),
            "starts_at": starts_at.isoformat(),
        }
    )


async def cancel_appointment(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    try:
        appointment_date = date.fromisoformat(str(body.get("date", "")))
    except ValueError:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    if appointment_date > today + timedelta(days=30):
        return web.json_response({"success": False, "reason": "not_found"})
    return web.json_response({"success": True})


async def clinic_info(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    key = str(body.get("key", "")).lower()
    value = CLINIC_INFO.get(key)
    if value is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    return web.json_response({"success": True, "value": value})


async def reschedule_appointment(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    try:
        new_starts_at = datetime.fromisoformat(str(body.get("new_starts_at", "")))
    except ValueError:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    if new_starts_at.hour == 10:
        return web.json_response({"success": False, "reason": "slot_taken"})
    return web.json_response(
        {"success": True, "starts_at": new_starts_at.isoformat()}
    )


async def request_callback(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    return web.json_response({"success": True})


async def log_call(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    return web.json_response({"success": True})


async def dispatch(request: web.Request) -> web.Response:
    body = await _payload(request)
    if body is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    action = str(body.get("action", "")).strip()
    handlers = {
        "check-availability": check_availability,
        "book-appointment": book_appointment,
        "cancel-appointment": cancel_appointment,
        "clinic-info": clinic_info,
        "reschedule-appointment": reschedule_appointment,
        "request-callback": request_callback,
        "log-call": log_call,
    }
    handler = handlers.get(action)
    if handler is None:
        return web.json_response(
            {"success": False, "reason": "invalid_input"}, status=400
        )
    return await handler(request)


def create_app() -> web.Application:
    app = web.Application(middlewares=[log_and_auth])
    app.router.add_post("/webhook", dispatch)
    app.router.add_post("/webhook/check-availability", check_availability)
    app.router.add_post("/webhook/book-appointment", book_appointment)
    app.router.add_post("/webhook/cancel-appointment", cancel_appointment)
    app.router.add_post("/webhook/clinic-info", clinic_info)
    app.router.add_post("/webhook/reschedule-appointment", reschedule_appointment)
    app.router.add_post("/webhook/request-callback", request_callback)
    app.router.add_post("/webhook/log-call", log_call)
    return app


if __name__ == "__main__":
    print(f"Mock n8n listening on http://127.0.0.1:{PORT}/webhook")
    web.run_app(create_app(), host="127.0.0.1", port=PORT, print=None)
