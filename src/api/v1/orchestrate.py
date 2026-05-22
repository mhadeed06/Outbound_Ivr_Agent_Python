# routes/orchestrate.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.auth.jwt_auth import verify_token
from src.config.insurance_config import (
    config_manager,
    lookup_by_payer_name,
    set_active_insurance,
)
from src.services.clinical.client import ClinicalApiError, fetch_visit_data
from src.services.clinical.required_fields import missing_fields_for
from src.services.practice_ehr_auth.client import AuthApiError, fetch_api_key_for_customer
from src.utils.logging_config import set_call_id
from src.models.data_models import CallState
import src.services.telnyx.client as telnyx_client


logger = logging.getLogger(__name__)


def _respond(succeeded: bool, message: str, http_status: int = 200) -> JSONResponse:
    """Standard frontend response shape — mirrors the inbound agent:
      {"succeeded": bool, "message": str}

    On failure, `message` carries the upstream error verbatim so the frontend
    can show it to the user.
    """
    return JSONResponse(
        {"succeeded": succeeded, "message": message},
        status_code=http_status,
    )


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
    router = APIRouter()

    @router.post("/orchestrate_call_simple")
    async def orchestrate_call_simple(
        request: Request,
        user: dict = Depends(verify_token),
        wait_for_initiated_ms: int = 10000,
    ):
        """
        Receive visit_id + customer_id, look up the insurance from the
        Clinical API's plan name, start the Telnyx call, optionally wait
        briefly for 'call.initiated', then return status.
        """
        try:
            # ── parse body ───────────────────────────────────────────────────
            try:
                incoming = await request.json()
            except Exception:
                logger.exception("❌ Invalid JSON body")
                return _respond(False, "Invalid JSON body", http_status=400)

            visit_id    = incoming.get("visit_id")
            customer_id = incoming.get("customer_id")

            # Frontend sends only visit_id + customer_id. The insurance is
            # derived later from the plan name returned by the Clinical API.
            missing_body = [
                name for name, val in (
                    ("visit_id", visit_id),
                    ("customer_id", customer_id),
                )
                if not val
            ]
            if missing_body:
                return _respond(
                    False,
                    f"Missing or empty required field(s): {', '.join(missing_body)}",
                    http_status=400,
                )

            # Capture the raw Bearer token from the Authorization header —
            # reused for Auth / Clinical / PracticeEHR upload / Billing-Agent Log.
            auth_header = request.headers.get("authorization", "")
            auth_token = auth_header.split(" ", 1)[1] if auth_header.lower().startswith("bearer ") else ""

            # 1) Resolve the per-customer API key (needs customer_id + token).
            try:
                api_key = await fetch_api_key_for_customer(customer_id, auth_token)
            except AuthApiError as e:
                return _respond(False, str(e), http_status=400)

            # 2) Fetch visit data from Clinical API (needs visit_id + token + key).
            #    The response carries the plan name we use to pick the insurance.
            try:
                visit_data = await fetch_visit_data(visit_id, auth_token, api_key)
            except ClinicalApiError as e:
                return _respond(False, str(e), http_status=400)

            # 3) Derive the insurance from the Clinical API's planShortName.
            #    Reject if it maps to no supported insurer.
            plan_short_name = visit_data.get("plan_short_name")
            insurance = lookup_by_payer_name(plan_short_name)
            if insurance is None:
                return _respond(
                    False,
                    f"Could not determine insurance from plan: {plan_short_name or '(none)'}",
                    http_status=400,
                )

            # 4) Bind this insurance to the current async task's ContextVar.
            #    Must happen before CallState() (its __post_init__ reads config).
            set_active_insurance(insurance)

            # 5) Validate that this insurance has all the fields it needs.
            missing = missing_fields_for(insurance.name, visit_data)
            if missing:
                return _respond(
                    False,
                    f"Missing required clinical data for {insurance.name}: {', '.join(missing)}",
                    http_status=400,
                )

            TEL_TO = insurance.phone_number

            # ── Telnyx call payload ──────────────────────────────────────────
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
                "send_silence_when_idle": True,
            }

            # ── start the call with Telnyx ───────────────────────────────────
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

            # ── extract data ─────────────────────────────────────────────────
            data = body.get("data", {})
            call_control_id = data.get("call_control_id")
            call_session_id = data.get("call_session_id")
            is_alive        = data.get("is_alive")

            if not call_control_id:
                logger.error(f"❌ Missing call_control_id in response: {body!r}")
                return _respond(False, "Telnyx response missing call_control_id", http_status=500)

            # Tag every subsequent log line with the short call ID.
            set_call_id(call_control_id)

            # ── store call state ─────────────────────────────────────────────
            active_calls[call_control_id] = CallState(
                call_control_id=call_control_id,
                visit_id=visit_id,
                customer_id=customer_id,
                call_session_id=call_session_id,
                auth_token=auth_token,
                api_key=api_key,
                insurance_name=insurance.name,
                visit_data=visit_data,
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

            # Traceability log line: all the IDs billing/ops might ask about,
            # in one place so you can grep by visit_id to find call_control_id.
            logger.info(
                f"✅ Call queued | status={status} visit_id={visit_id} customer_id={customer_id} "
                f"insurance={insurance.name} plan={plan_short_name!r} "
                f"call_control_id={call_control_id} call_session_id={call_session_id} "
                f"is_alive={is_alive}"
            )

            # Map internal status → frontend-friendly {succeeded, message}.
            # "initiated" = IVR actually picked up → succeeded=true.
            # "queued"    = we timed out waiting → mirror inbound's pattern.
            if status == "initiated":
                return _respond(True, "Call answered")
            else:
                return _respond(False, "Call not answered (reason: timeout)")

        except Exception:
            logger.exception("❌ Unexpected error orchestrating call")
            return _respond(False, "Internal error starting call", http_status=500)

    return router
