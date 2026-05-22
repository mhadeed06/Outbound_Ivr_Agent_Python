"""
POST to v1/Billing-Agent/Log — records the outcome of a completed call
(stored file paths + classified status) to the PracticeEHR billing DB.

Auth: Bearer token only (no API key).
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def post_billing_log(
    *,
    request_type: str,
    transcript_path: str,
    recording_path: str,
    request_status: str,
    claim_status: str,
    description: str,
    visit_seq_num,
    customer_id,
    entered_by: str,
    payer_name: str,
    auth_token: str,
) -> Optional[int]:
    """
    Returns the `data` field from the response on success, or None on failure.
    Never raises — all failures are logged and swallowed.
    """
    base_url = os.getenv("BILLING_AGENT_LOG_BASE_URL", "").rstrip("/")

    if not base_url or base_url == "REPLACE_ME":
        logger.error("BILLING_AGENT_LOG_BASE_URL is not configured — skipping Log call")
        return None
    if not auth_token:
        logger.error("No auth token available for Billing-Agent Log call")
        return None

    url = f"{base_url}/v1/Billing-Agent/Log"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "requestType": request_type,
        "transcriptPath": transcript_path,
        "recordingPath": recording_path,
        "claimStatus": claim_status,
        "requestStatus": request_status,
        "description": description,
        "visitSeqNum": _as_int(visit_seq_num),
        "customerId": _as_int(customer_id),
        "enteredBy": entered_by,
        "payerName": payer_name,
    }

    logger.info(
        f"📮 POST Billing-Agent/Log requestStatus={request_status!r} claimStatus={claim_status!r} "
        f"payerName={payer_name!r} visit={payload['visitSeqNum']} customer={payload['customerId']}"
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except Exception as e:
        logger.exception(f"❌ Billing-Agent/Log request exception: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"❌ Billing-Agent/Log failed ({resp.status_code}): {resp.text[:500]}")
        return None

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ Billing-Agent/Log returned non-JSON: {e}")
        return None

    if not body.get("succeeded"):
        logger.error(f"❌ Billing-Agent/Log returned succeeded=false: {body}")
        return None

    data = body.get("data")
    logger.info(f"✅ Billing-Agent/Log OK: data={data}")
    return data


def _as_int(value):
    """Convert to int if possible; otherwise return the original (API can validate)."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return value
