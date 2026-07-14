# services/llm_service.py
from __future__ import annotations
import os
import httpx
import logging
import re
from openai import AsyncAzureOpenAI
logger = logging.getLogger(__name__)
from dotenv import load_dotenv

load_dotenv()

# Initialize client once (do NOT create per request)
azure_client = AsyncAzureOpenAI(
    api_key=os.getenv("PTU_API_KEY"),
    azure_endpoint=os.getenv("PTU_AZURE_ENDPOINT"),
    api_version=os.getenv("PTU_API_VERSION"),
)

async def _call_gpt_api(prompt: str, max_tokens: int = 50) -> str:
    """
    Azure GPT call replacing LLaMA.
    Returns plain text response.

    max_tokens defaults to 50 — enough for the short IVR-control replies
    (one-word intents, "say:"/"value:" commands). Callers that need a longer
    response (e.g. the claim classifier returning JSON + a description) should
    pass a larger value.
    """

    try:
        response = await azure_client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1"),  # Azure deployment name
            messages=[
                {
                    "role": "system",
                    "content": "You are an outbound calling agent for insurance IVR handling claim status calls."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            max_tokens=max_tokens,
        )


        answer = response.choices[0].message.content or "(no response)"
        logger.info(f"🤖 GPT response: {answer!r}")
        return answer.strip()

    except Exception as e:
        logger.error(f"❌ GPT API exception: {e}")
        raise

# ---- text command parsing helper----
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
        # NOTE: do NOT set cs.status = "hangup" here. ensure_call_cleanup only
        # sends the Telnyx hangup when status is not already "hangup"/"ended";
        # pre-setting it would make cleanup skip the hangup and the call would
        # linger on Telnyx (and the recording would never finalize). Let
        # cleanup send the hangup and let the webhook set the status.
        await ensure_call_cleanup(call_control_id, reason="llm: end/hangup command", send_hangup=True)
        return

    # 4) Explicit no-response → ignore
    if "no response" in low or compact == "noresponse":
        logger.info("→ No response; continue listening")
        return

    # 5) Anything else → ignore (no TTS)
    logger.info(f"Ignoring unrecognized Llama reply: {s!r}")
