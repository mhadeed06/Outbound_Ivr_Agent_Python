# services/llm_service.py
from __future__ import annotations

import httpx
import logging
import re

logger = logging.getLogger(__name__)

# ---- core HTTP call (dependency-injected URL) ----
async def _call_llama_api(prompt: str, *, url: str) -> str:
    """
    Core LLM call. URL is injected from main via partial.
    Returns the 'response' field or '(no response)' on error.
    """
    payload = {
        "doctor_query": prompt,
        "role": "You are an outbound calling agent for insurance IVR handling claim status calls.",
        "max_new_tokens": 100
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            raw = resp.json()
            if resp.status_code == 200:
                answer = raw.get("response", "(no response)")
                logger.info(f"🦙 Parsed Llama response: {answer!r}")
                return answer
            else:
                logger.error(f"❌ Llama API error {resp.status_code}: {raw!r}")
                return "(no response)"
    except Exception as e:
        logger.error(f"❌ Llama API exception: {e}")
        return "(no response)"


# ---- text command parsing helpers (unchanged logic) ----
PAUSE_RE = re.compile(r"\s+")

def _normalize(s: str) -> str:
    return s.strip().strip("`").strip()

def _after(s: str, low: str, keyword: str) -> str | None:
    i = low.find(keyword)
    if i == -1:
        return None
    j = i + len(keyword)
    while j < len(s) and s[j] in " :=-\t":
        j += 1
    if j >= len(s):
        return ""
    if s[j] in ("'", '"'):
        q = s[j]
        k = s.find(q, j + 1)
        return s[j + 1:k].strip() if k != -1 else s[j + 1:].strip()
    return s[j:].strip()


# ---- response handler (dependencies injected) ----
async def _process_llama_response(
    response: str,
    call_control_id: str,
    *,
    speak_with_azure,                 # callable(text, call_id)
    send_dtmf,                        # callable(digits, call_id)
    ensure_call_cleanup,              # callable(call_id, reason=..., send_hangup=...)
    active_calls: dict,
) -> None:
    """
    Implements exactly the same behavior you had in main.py:
      - say/value/confirm -> TTS
      - dtmf -> send DTMF
      - end/endcall/hangup -> cleanup + hangup
      - 'fallback' and unknown -> ignore
    All dependencies are injected from main via partial.
    """
    if not response:
        logger.info("❗ Llama returned empty response")
        return

    s = _normalize(response)
    if not s:
        return

    # If it's just a quoted string, treat it as: say <text>
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = f"say {s[1:-1].strip()}"

    low = s.lower()

    # Ignore explicit "fallback" (do nothing)
    if low == "fallback" or low.startswith("fallback "):
        logger.info("Llama returned fallback; ignoring")
        return

    # 1) DTMF (explicit only)
    if low.startswith("dtmf"):
        tail = _after(s, low, "dtmf") or ""
        digits = "".join(ch for ch in tail if ch.isdigit() or ch in "*#")
        if digits:
            logger.info(f"→ Sending DTMF: {digits}")
            await send_dtmf(digits, call_control_id)
        else:
            logger.info("DTMF payload empty after sanitizing; ignoring")
        return

    # 2) SAY / VALUE / CONFIRM → speak
    for kw in ("say", "value", "confirm"):
        val = _after(s, low, kw)
        if val:
            logger.info(f"→ Speak ({kw}): {val!r}")
            await speak_with_azure(val, call_control_id)
            return

    # 3) End / hangup
    compact = low.replace(" ", "")
    if compact in ("endcall", "end", "hangup"):
        logger.info("→ Hanging up per instruction")
        cs = active_calls.get(call_control_id)
        if cs:
            cs.status = "hangup"
        await ensure_call_cleanup(call_control_id, reason="llm: end/hangup command", send_hangup=True)
        return

    # 4) Explicit no-response → ignore
    if "no response" in low or compact == "noresponse":
        logger.info("→ No response; continue listening")
        return

    # 5) Anything else → ignore (no TTS)
    logger.info(f"Ignoring unrecognized Llama reply: {s!r}")
