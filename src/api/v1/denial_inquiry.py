# routes/denial_inquiry.py
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config.insurance_config import (
    INSURANCE_CONFIGS,
    config_manager,
    set_active_insurance,
)
from src.models.data_models import CallState
from src.utils.logging_config import set_call_id
import src.services.telnyx.client as telnyx_client


logger = logging.getLogger(__name__)


# Fields the caller MUST send in the body. Used both for the knowledge sheet
# (fed into prompts) and for the response.
_REQUIRED_FIELDS = (
    "visit_id",
    "patient_name",
    "dob",
    "dos",
    "billed_amount",
    "member_id",
    "plan",
    "provider_npi",
    "provider_name",
)


def _respond(succeeded: bool, message: str, http_status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"succeeded": succeeded, "message": message},
        status_code=http_status,
    )


def make_denial_inquiry_router(
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
    """Build the denial-inquiry router.

    Mirrors make_orchestrate_router but:
      - No JWT auth (testing endpoint only).
      - No Clinical / Auth API calls — caller provides all visit data inline.
      - No Billing-Agent/Log row created — post-call upload is also skipped.
      - Routes to the carrier's denial_phone_number (not claim-status number).
    """
    router = APIRouter()

    @router.post("/v1/Billing-Agent/Denial-Inquiry")
    async def create_denial_inquiry_call(
        request: Request,
        wait_for_initiated_ms: int = 10000,
    ):
        try:
            # ── parse body ───────────────────────────────────────────────────
            try:
                incoming = await request.json()
            except Exception:
                logger.exception("❌ Invalid JSON body")
                return _respond(False, "Invalid JSON body", http_status=400)

            missing = [f for f in _REQUIRED_FIELDS if not incoming.get(f)]
            if missing:
                return _respond(
                    False,
                    f"Missing or empty required field(s): {', '.join(missing)}",
                    http_status=400,
                )

            # Caller's plan name (e.g. "HUMANA") drives which insurance + prompt
            # templates we use. Must match an entry in INSURANCE_CONFIGS.
            plan = str(incoming["plan"]).strip().upper()
            insurance = INSURANCE_CONFIGS.get(plan)
            if insurance is None:
                return _respond(
                    False,
                    f"Unknown plan: {plan!r}",
                    http_status=400,
                )
            if not insurance.supports_denial_inquiry:
                return _respond(
                    False,
                    f"{insurance.name} is not configured for denial-inquiry calls",
                    http_status=400,
                )
            if not insurance.denial_phone_number:
                return _respond(
                    False,
                    f"{insurance.name} has no denial_phone_number configured",
                    http_status=500,
                )

            # Bind this insurance to the current async task's ContextVar so
            # prompts/manager.py can look up templates by carrier name.
            set_active_insurance(insurance)

            # The body BECOMES the knowledge sheet — fed straight into prompts.
            denial_data = {f: str(incoming[f]) for f in _REQUIRED_FIELDS}

            # ── Telnyx call payload ──────────────────────────────────────────
            call_payload = {
                "to": insurance.denial_phone_number,
                "from": TEL_FROM,
                "connection_id": CALL_CONTROL_APP_ID,
                "webhook_url": f"{WEBHOOK_BASE_URL}/webhooks/calls",
                "webhook_url_method": "POST",
                "stream_url": f"{STREAM_BASE_URL}/stream",
                "stream_track": "both_tracks",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "send_silence_when_idle": True,
            }

            response = await telnyx_client.create_call_raw(call_payload, TELNYX_BASE_URL, HEADERS)
            try:
                body = response.json()
            except Exception:
                logger.exception("❌ Failed to parse JSON from Telnyx")
                return _respond(False, "Could not start call (invalid Telnyx response)", http_status=500)

            if not (200 <= response.status_code < 300):
                logger.error(f"❌ Telnyx error {response.status_code}: {body!r}")
                return _respond(
                    False,
                    f"Could not start call (Telnyx returned {response.status_code})",
                    http_status=500,
                )

            data = body.get("data", {})
            call_control_id = data.get("call_control_id")
            call_session_id = data.get("call_session_id")
            if not call_control_id:
                logger.error(f"❌ Missing call_control_id in response: {body!r}")
                return _respond(False, "Telnyx response missing call_control_id", http_status=500)

            set_call_id(call_control_id)

            # ── store call state ─────────────────────────────────────────────
            # NOTE: flow_type="denial_inquiry" gates _handle_denial_speech in
            # main.py. phase starts as "ivr" and flips to "representative"
            # the first time is_transfer_signal() matches the transcript.
            active_calls[call_control_id] = CallState(
                call_control_id=call_control_id,
                visit_id=denial_data["visit_id"],
                customer_id=None,           # not used in denial flow
                call_session_id=call_session_id,
                auth_token=None,            # not used in denial flow
                api_key=None,               # not used in denial flow
                insurance_name=insurance.name,
                visit_data=None,            # not used — denial_data is the source of truth
                ref_no=None,                # no Billing-Agent/Log row
                flow_type="denial_inquiry",
                phase="ivr",
                denial_data=denial_data,
            )

            auto_hangup_seconds = config_manager.get_auto_hangup_seconds()
            asyncio.create_task(auto_hangup_fn(call_control_id, delay_seconds=auto_hangup_seconds))  # type: ignore[arg-type]

            # ── race-proof wait for 'call.initiated' ────────────────────────
            status = "queued"
            if (wait_for_initiated_ms or 0) > 0:
                ev = initiated_events.setdefault(call_control_id, asyncio.Event())
                cs = active_calls.get(call_control_id)
                if cs and getattr(cs, "status", None) == "initiated":
                    ev.set()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=(wait_for_initiated_ms / 1000.0))
                    status = "initiated"
                except asyncio.TimeoutError:
                    status = "queued"
                finally:
                    initiated_events.pop(call_control_id, None)

            logger.info(
                f"✅ Denial-inquiry call queued | status={status} visit_id={denial_data['visit_id']} "
                f"plan={plan} call_control_id={call_control_id} call_session_id={call_session_id}"
            )

            if status == "initiated":
                return _respond(True, "Call answered")
            return _respond(False, "Call not answered (reason: timeout)")

        except Exception:
            logger.exception("❌ Unexpected error orchestrating denial-inquiry call")
            return _respond(False, "Internal error starting call", http_status=500)

    return router
