# routes/orchestrate.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.auth.jwt_auth import verify_token
from src.config.insurance_config import config_manager
from src.models.data_models import CallState
import src.services.telnyx.client as telnyx_client


logger = logging.getLogger(__name__)

def make_orchestrate_router(
    active_calls: Dict[str, CallState],
    initiated_events: Dict[str, asyncio.Event],
    *,
    TELNYX_BASE_URL: str,
    HEADERS: dict,
    TEL_FROM: str,
    CALL_CONTROL_APP_ID: str,
    WEBHOOK_BASE_URL: str,
    STREAM_BASE_URL: str,
    auto_hangup_fn: Callable[[str, int], asyncio.Task] | Callable[[str, int], None],
) -> APIRouter:
    """
    Factory that builds a router and closes over your existing shared state and settings.
    This avoids importing from main.py and keeps internal logic identical.
    """
    router = APIRouter()

    @router.post("/orchestrate_call_simple")
    async def orchestrate_call_simple(request: Request, user: dict = Depends(verify_token), wait_for_initiated_ms: int = 10000):
        """
        Receive agent_id + app_id, start the Telnyx call (same flow as /start_call),
        optionally wait briefly for 'call.initiated', then return status.
        """
        try:
            # ── parse body ───────────────────────────────────────────────────
            try:
                incoming = await request.json()
            except Exception:
                logger.exception("❌ Invalid JSON body")
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            agent_id = incoming.get("agent_id")
            app_id   = incoming.get("app_id")

            if not agent_id or not app_id:
                return JSONResponse(
                    {"error": "agent_id and app_id are required"},
                    status_code=400
                )

            # NOTE: keep the same TEL_TO behavior as your main.py
            TEL_TO = config_manager.get_phone_number()

            # ── same Telnyx call payload as /start_call ─────────────────────
            call_payload = {
                "to": TEL_TO,
                "from": TEL_FROM,
                "connection_id": CALL_CONTROL_APP_ID,
                "webhook_url": f"{WEBHOOK_BASE_URL}/webhooks/calls",
                "webhook_url_method": "POST",
                "stream_url": f"{STREAM_BASE_URL}/stream",
                "stream_track": "both_tracks",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "send_silence_when_idle": True
            }

            # ── start the call with Telnyx (keeps response semantics) ───────
            response = await telnyx_client.create_call_raw(call_payload, TELNYX_BASE_URL, HEADERS)

            # parse Telnyx response safely (unchanged)
            try:
                body = response.json()
            except Exception:
                logger.exception("❌ Failed to parse JSON from Telnyx")
                return JSONResponse(
                    {"error": f"Invalid JSON from Telnyx: {response.text}"},
                    status_code=500
                )

            if not (200 <= response.status_code < 300):
                logger.error(f"❌ Telnyx error {response.status_code}: {body!r}")
                return JSONResponse(
                    {"error": f"Telnyx returned {response.status_code}: {body!r}"},
                    status_code=500
                )

            # ── extract data (unchanged) ─────────────────────────────────────
            data = body.get("data", {})
            call_control_id = data.get("call_control_id")
            call_session_id = data.get("call_session_id")
            is_alive        = data.get("is_alive")

            if not call_control_id:
                logger.error(f"❌ Missing call_control_id in response: {body!r}")
                return JSONResponse(
                    {"error": f"Missing call_control_id in Telnyx response: {body!r}"},
                    status_code=500
                )

            # ── store call state + your two IDs (unchanged) ─────────────────
            active_calls[call_control_id] = CallState(
                call_control_id=call_control_id,
                agent_id=agent_id,
                app_id=app_id
            )
            # keep your original behavior for auto hangup
            asyncio.create_task(auto_hangup_fn(call_control_id, delay_seconds=900))  # type: ignore[arg-type]

            # ── race-proof wait for 'call.initiated' (unchanged) ────────────
            status = "queued"
            if (wait_for_initiated_ms or 0) > 0:
                # create/reuse the event BEFORE checking status to avoid race
                ev = initiated_events.setdefault(call_control_id, asyncio.Event())

                # if webhook already flipped status, set event now
                cs = active_calls.get(call_control_id)
                if cs and getattr(cs, "status", None) == "initiated":
                    ev.set()

                try:
                    await asyncio.wait_for(
                        ev.wait(),
                        timeout=(wait_for_initiated_ms / 1000.0)
                    )
                    status = "initiated"
                except asyncio.TimeoutError:
                    status = "queued"  # fallback after timeout
                finally:
                    initiated_events.pop(call_control_id, None)

            logger.info(f"✅ Call queued: {call_control_id} (is_alive={is_alive}) status={status}")

            # ── response (unchanged) ────────────────────────────────────────
            return JSONResponse({
                "success": True,
                "status": status,
                "agent_id": agent_id,
                "app_id": app_id,
                "call_control_id": call_control_id,
                "call_session_id": call_session_id,
                "is_alive": is_alive
            })

        except Exception:
            logger.exception("❌ Unexpected error orchestrating call")
            return JSONResponse(
                {"error": "Internal error starting call; check server logs"},
                status_code=500
            )

    return router
