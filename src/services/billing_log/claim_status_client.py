"""
POST to IVR/ClaimStatus — the final per-call status update, sent AFTER
Billing-Agent/Log returns the row id (which becomes RefNo here).

POST {IVR_CLAIM_STATUS_BASE_URL}/IVR/ClaimStatus
Headers:
  x-customer-id : <customer_id from the frontend>
  Authorization : bearer <frontend JWT (the same token used to call us)>
Body:
  {
    "VisitSeqNum": <visit_id, number>,
    "RefNo":       "<Billing-Agent/Log data id>",
    "Summary":     "<claim summary>",
    "Status":      "<claim status>"
  }
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Map our internal claim status → the exact values the IVR/ClaimStatus endpoint
# accepts (case-sensitive). "no claim" maps to "NOT ON FILE".
_STATUS_MAP = {
    "paid": "PAID",
    "inprocess": "IN PROCESS",
    "denied": "DENIED",
    "unknown": "UNKNOWN",
    "no claim": "NOT ON FILE",
}


def _to_ivr_status(internal_status: str) -> str:
    """Translate our internal status to the endpoint's allowed enum.
    Falls back to UNKNOWN for anything unexpected."""
    return _STATUS_MAP.get((internal_status or "").strip().lower(), "UNKNOWN")


async def post_ivr_claim_status(
    *,
    visit_seq_num,
    ref_no,
    summary: str,
    status: str,
    customer_id,
    bearer_token: str,
) -> bool:
    """
    Returns True on a successful (HTTP 200 + Success=true) response, else False.
    Never raises — all failures are logged and swallowed.

    `bearer_token` is the frontend JWT (the same token the frontend used to
    call /orchestrate_call_simple).
    """
    base_url = os.getenv("IVR_CLAIM_STATUS_BASE_URL", "").rstrip("/")
    if not base_url or base_url == "REPLACE_ME":
        logger.error("IVR_CLAIM_STATUS_BASE_URL is not configured — skipping IVR/ClaimStatus")
        return False
    if not bearer_token:
        logger.error("No bearer token for IVR/ClaimStatus — skipping")
        return False
    if not customer_id:
        logger.error("No customer_id for IVR/ClaimStatus — skipping")
        return False

    url = f"{base_url}/IVR/ClaimStatus"
    headers = {
        "x-customer-id": str(customer_id),
        "Authorization": f"bearer {bearer_token}",
        "Content-Type": "application/json",
    }
    ivr_status = _to_ivr_status(status)  # map internal → endpoint enum
    payload = {
        "VisitSeqNum": _as_int(visit_seq_num),
        "RefNo": str(ref_no),
        "Summary": summary,
        "Status": ivr_status,
    }

    logger.info(
        f"📮 POST IVR/ClaimStatus VisitSeqNum={payload['VisitSeqNum']} "
        f"RefNo={payload['RefNo']!r} Status={ivr_status!r} (from {status!r})"
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except Exception as e:
        logger.exception(f"❌ IVR/ClaimStatus request exception: {e}")
        return False

    if resp.status_code != 200:
        logger.error(f"❌ IVR/ClaimStatus failed ({resp.status_code}): {resp.text[:500]}")
        return False

    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"❌ IVR/ClaimStatus returned non-JSON: {e}")
        return False

    # Their response uses capital-S "Success"
    if not body.get("Success"):
        logger.error(f"❌ IVR/ClaimStatus Success=false: {body}")
        return False

    logger.info(f"✅ IVR/ClaimStatus OK: {body.get('Message')!r}")
    return True


def _as_int(value):
    """Convert to int if possible; otherwise return the original."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return value
