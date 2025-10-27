# services/call_cleanup.py
import logging
from typing import Dict, Any, Callable
import asyncio

logger = logging.getLogger(__name__)

async def ensure_call_cleanup(
    call_control_id: str,
    *,
    reason: str,
    send_hangup: bool,
    active_calls: Dict[str, Any],
    claims_agent,
    stt_manager,
    hangup_call: Callable[[str], Any],
):
    """
    Unified, idempotent cleanup for a call.
    Safe to call from webhook, WebSocket finally, auto-hangup, or shutdown.
    - Ends claims session (if active)
    - Cleans Azure STT session
    - Optionally issues hangup to Telnyx
    - Removes call from active_calls
    """
    cs = active_calls.get(call_control_id)
    if not cs:
        return

    # lazily add guard fields to CallState
    if not hasattr(cs, "cleanup_lock"):
        cs.cleanup_lock = asyncio.Lock()
    if not hasattr(cs, "cleanup_done"):
        cs.cleanup_done = False

    async with cs.cleanup_lock:
        if cs.cleanup_done:
            return

        logger.info(f"🧹 Cleanup [{call_control_id}] due to: {reason}")

        # 1️⃣ stop claims flow
        try:
            await claims_agent.end_session(call_control_id)
        except Exception as e:
            logger.warning(f"[{call_control_id}] end_session error (ignored): {e}")

        # 2️⃣ stop/cleanup STT
        try:
            if getattr(cs, "azure_stt_session", None):
                stt_manager.remove_session(cs.websocket_id)
        except Exception as e:
            logger.warning(f"[{call_control_id}] STT cleanup error (ignored): {e}")

        # 3️⃣ send hangup if needed
        if send_hangup and getattr(cs, "status", "") not in ("hangup", "ended"):
            try:
                await hangup_call(call_control_id)
            except Exception as e:
                logger.warning(f"[{call_control_id}] hangup_call error (ignored): {e}")

        

        # 4️⃣ clear flags and forget this call
        cs.claim_mode = False
        cs.cleanup_done = True

        active_calls.pop(call_control_id, None)

        logger.info(f"✅ Cleanup complete [{call_control_id}]")
