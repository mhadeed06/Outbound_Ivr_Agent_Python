import os
import time
import asyncio
import httpx
import logging
from typing import Dict, List
from dotenv import load_dotenv
load_dotenv()
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

CLAIMS_TAIL_CHARS  = 200     ### 250 for CIGNA    ## 150 for humana    ### 200 FOR BUYLER SCOTT


# Baylor Scott Claims IVR Controller Prompt

CONTROLLER_PROMPT_TEMPLATE = """
You are an IVR controller for Baylor Scott Claims flow.

**IMPORTANT NOTE**
"ONLY REPLY WITH THE SPECIFIED RESPONSES. IF THE TRANSCRIPT ONLY CARRIES CLAIM DETAILS/DATA, THEN JUST REPLY WITH CONTINUE"
"If you get multiple options in a transcript, give priority to progressing through claims or ending appropriately"

**Baylor Scott Claims Flow:**
- After claim details, you'll hear options like: "repeat that or press 1, NEXT CLAIM, previous claim, switch provider, main menu, check another date another member"
- If "NEXT CLAIM" option is available → respond with NEXT CLAIM
- If "NEXT CLAIM" option is NOT available (usually after last claim) → respond with STOP

*Analyze the transcript and return ONE of these responses:*

1- **NEXT CLAIM** - When you hear "NEXT CLAIM" in the options after claim details
2- **STOP** - When claim details are provided but "NEXT CLAIM" is NOT mentioned in the options (indicates last claim)
3- **CONTINUE** - For everything else (claim details, explanations, data reading)

**Examples:**
- "Here are the details... you can say repeat that, NEXT CLAIM, previous claim, main menu" → **NEXT CLAIM**
- "Here are the details... you can say repeat that, previous claim, switch provider, main menu" → **STOP** (no NEXT CLAIM option)
- "I found 2 claims, here is the first one and its details..." → **CONTINUE**

TRANSCRIPT: "{transcript_chunk}"

Reply with ONE RESPONSE: NEXT CLAIM, STOP, or CONTINUE
Answer:
""".strip()
# ==========================================================================================

# per-call state
_sessions: Dict[str, Dict] = {}       # call_id -> {"active": bool, "current": List[str], "claims": List[str]}
_locks: Dict[str, asyncio.Lock] = {}  # call_id -> asyncio.Lock

# injected callbacks from main (optional)
_hangup_cb = None     # async def (call_id: str) -> None
_tts_cb = None        # async def (text: str, call_id: str) -> None


def register_hangup(cb):
    """Main should call this once: claims_agent.register_hangup(hangup_call)"""
    global _hangup_cb
    _hangup_cb = cb

def register_tts(cb):
    """Main must call once: claims_agent.register_tts(speak_with_azure)"""
    global _tts_cb
    _tts_cb = cb


def is_active(call_id: str) -> bool:
    s = _sessions.get(call_id)
    return bool(s and s.get("active"))


async def start_session(call_id: str):
    _sessions[call_id] = {"active": True, "current": [], "claims": []}
    _locks[call_id] = _locks.get(call_id) or asyncio.Lock()


async def end_session(call_id: str, *, already_locked: bool = False):
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return

    async def _finish():
        _finalize_current(s)
        s["active"] = False
        if _hangup_cb:
            try:
                await _hangup_cb(call_id)
            except Exception:
                pass

    if already_locked:
        await _finish()
    else:
        async with _locks.setdefault(call_id, asyncio.Lock()):
            await _finish()


def get_claims(call_id: str) -> List[str]:
    s = _sessions.get(call_id) or {}
    return list(s.get("claims", []))
# If you want one string: "\n\n---\n\n".join(get_claims(call_id))


def _finalize_current(s: Dict):
    txt = " ".join(s["current"]).strip()
    if txt:
        s["claims"].append(txt)
    s["current"].clear()  # next claim starts clean


async def handle_final(call_id: str, utterance: str):
    """
    Main calls this for EVERY debounced Final while in claim mode.
    Append -> send tail (last 180 chars) of CURRENT claim to GPT-4o -> act on keyword.
    """
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return

    lock = _locks.setdefault(call_id, asyncio.Lock())
    async with lock:
        # 1) buffer the current claim only
        s["current"].append(utterance)

        # 2) build transcript and take a small tail for the controller
        full_transcript = " ".join(s["current"])
        chunk = full_transcript[-CLAIMS_TAIL_CHARS:].strip()   ### 250 characters for cigna 

        # 3) ask GPT for ONE WORD intent
        intent = await _ask_gpt_keyword(call_id, chunk)

        # 4) act (STOP > NEXT > DETAILS > CONFIRM/NO > CONTINUE)
        if intent == "STOP":
            await end_session(call_id, already_locked=True)
            return

        if intent == "NEXT":
            _finalize_current(s)  # store claim N, clear buffer
            if _tts_cb:
                try:
                    await _tts_cb("Next claim", call_id)
                except Exception:
                    pass
            return

        if intent == "DETAILS":
            if _tts_cb:
                try:
                    await _tts_cb("Details", call_id)
                except Exception:
                    pass
            return

        if intent == "CONFIRM":
            if _tts_cb:
                try:
                    await _tts_cb("Yes", call_id)
                except Exception:
                    pass
            return

        if intent == "NO":
            if _tts_cb:
                try:
                    await _tts_cb("No", call_id)
                except Exception:
                    pass
            return
        
        if intent == "FAX-ID":
            if _tts_cb:
                try:
                    await _tts_cb("2144465424", call_id) 
                except Exception:
                    pass
            return

        # CONTINUE (or unknown): keep buffering
        return


# ---------- GPT-4o controller (minimal logging) ----------

async def _ask_gpt_keyword(call_id: str, transcript_chunk: str) -> str:
    """
    Use GPT-4o ('4-o') to return ONE WORD:
    DETAILS, NEXT, STOP, CONFIRM, NO, or CONTINUE.
    """
    if not OPENAI_API_KEY:
        return "CONTINUE"

    system_prompt = CONTROLLER_PROMPT_TEMPLATE.format(transcript_chunk=transcript_chunk)

    # minimal logs: what we send + what we get
    logger.info(f"[{call_id}] → GPT tail: {transcript_chunk}")

    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-4o",  # per your request
            "messages": [
                {"role": "system", "content": system_prompt},
            ],
            "max_tokens": 4,
            "temperature": 0,
            "top_p": 1,
            "presence_penalty": 0,
            "frequency_penalty": 0,
        }

        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers=headers,
            )
        ms = (time.perf_counter() - t0) * 1000

        if resp.status_code != 200:
            logger.error(f"[{call_id}] ← GPT {resp.status_code}: {resp.text[:180]}")
            return "CONTINUE"

        data = resp.json()
        raw = (data.get("choices", [{}])[0]
                     .get("message", {})
                     .get("content", "")).strip()
        intent = _map_keyword(raw.upper())

        logger.info(f"[{call_id}] ← GPT: {raw!r} → {intent} ({ms:.0f}ms)")
        return intent

    except Exception as e:
        logger.error(f"[{call_id}] ← GPT EXC: {e}")
        return "CONTINUE"


def _map_keyword(upper: str) -> str:
    if "DETAIL" in upper:
        return "DETAILS"
    if "NEXT" in upper:
        return "NEXT"
    if "STOP" in upper or "END" in upper or "HANG" in upper or "MAIN MENU" in upper or "NO MORE CLAIM" in upper:
        return "STOP"
    if "CONFIRM" in upper or "YES" in upper or "HEAR CLAIM" in upper:
        return "CONFIRM"
    if "FAX" in upper or "FAX ID" in upper or "FAX-ID" in upper or "FAXID" in upper:
        return "FAX-ID"
    if upper == "NO" or " NO" in upper or upper.startswith("NO") or upper.endswith(" NO"):
        return "NO"
    return "CONTINUE"

    

