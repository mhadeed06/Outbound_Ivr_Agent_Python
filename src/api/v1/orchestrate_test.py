# routes/orchestrate_test.py
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


# Fields the caller MUST send in the body. These become the CallState's
# visit_data — fed into the insurance's prompt template like it came from
# the Clinical API. Same keys as the prompt placeholders.
_REQUIRED_FIELDS = (
    "visit_id",
    "plan",           # e.g. "UHC", "CIGNA" — must be an entry in INSURANCE_CONFIGS
    "npi",
    "member_id",
    "dob",
    "member_name",
    "dos",
)

# Optional fields — passed through to visit_data if present. Not all
# insurances need all of these; the prompt template decides what to use.
# billed_amount is only needed by the follow-up (denial-inquiry) flow, not
# claim-status — but we accept it here in case future insurances need it.
_OPTIONAL_FIELDS = (
    "tax_id",
    "billed_amount",
    "provider_name",
    "provider_address",
)


def _respond(succeeded: bool, message: str, http_status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"succeeded": succeeded, "message": message},
        status_code=http_status,
    )


def make_orchestrate_test_router(
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
    """Build the dev/test claim-status router.

    Mirrors make_orchestrate_router but:
      - No JWT auth (testing endpoint only).
      - No Clinical / Auth API calls — caller provides all visit data inline.
      - No Billing-Agent/Log row created — post-call upload is also skipped
        (guarded via CallState.is_test → snapshot["is_test"]).
      - Uses the SAME IVR runtime path (handle_user_speech, prompts,
        claims_agent, cleanup) as production.

    Intended use: exercise new-insurance IVR flows before the Clinical API
    has visit data for that payer. Deprecate this endpoint once Clinical
    supports the payer and it's reachable via /v1/Billing-Agent/Call.
    """
    router = APIRouter()

    @router.post("/v1/Billing-Agent/Call/Test")
    async def create_test_call(
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

            plan = str(incoming["plan"]).strip().upper()
            insurance = INSURANCE_CONFIGS.get(plan)
            if insurance is None:
                return _respond(
                    False,
                    f"Unknown plan: {plan!r}",
                    http_status=400,
                )

            # Bind this insurance to the current async task's ContextVar so
            # prompts/manager.py can look up templates by carrier name.
            set_active_insurance(insurance)

            # Build the visit_data dict that would normally come from Clinical.
            # Required + optional fields, in the same shape the prompt expects.
            visit_data: dict = {}
            for f in _REQUIRED_FIELDS + _OPTIONAL_FIELDS:
                if f in incoming and incoming[f] is not None:
                    visit_data[f] = str(incoming[f])

            visit_id = visit_data["visit_id"]

            # ── Telnyx call payload ──────────────────────────────────────────
            call_payload = {
                "to": insurance.phone_number,
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
            # is_test=True → post_call_upload will skip all DB writes.
            # No Billing-Agent/Log row is created (ref_no stays None).
            active_calls[call_control_id] = CallState(
                call_control_id=call_control_id,
                visit_id=visit_id,
                customer_id=None,
                call_session_id=call_session_id,
                auth_token=None,
                api_key=None,
                insurance_name=insurance.name,
                visit_data=visit_data,
                ref_no=None,
                is_test=True,
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
                f"🧪 Test call queued | status={status} visit_id={visit_id} "
                f"plan={plan} call_control_id={call_control_id} "
                f"call_session_id={call_session_id}"
            )

            if status == "initiated":
                return _respond(True, "Call answered")
            return _respond(False, "Call not answered (reason: timeout)")

        except Exception:
            logger.exception("❌ Unexpected error orchestrating test call")
            return _respond(False, "Internal error starting call", http_status=500)

    return router
