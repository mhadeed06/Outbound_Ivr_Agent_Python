import os
import time
import asyncio
import httpx
import logging
from typing import Dict, List
from dotenv import load_dotenv
from src.config.insurance_config import config_manager
from ..prompts.claims_prompts import get_claims_prompt
from src.config.insurance_config import config_manager
from src.services.llm_service import _call_gpt_api
load_dotenv()
logger = logging.getLogger(__name__)

# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# if not OPENAI_API_KEY:
#     raise RuntimeError(
#         "OPENAI_API_KEY is not set. "
#         "Expected it in OUTBOUND_AZURE_TELNYX/.env or the process environment."
#     )

#CLAIMS_TAIL_CHARS  = 200     ### 250 for CIGNA    ## 150 for humana    ### 200 FOR BUYLER SCOTT
def get_claims_tail_chars() -> int:
    """Get claims tail chars for current insurance"""
    return config_manager.get_claims_tail_chars()

def get_controller_prompt_template() -> str:
    """Get the controller prompt template for current insurance"""
    config = config_manager.get_config()
    return get_claims_prompt(config.claims_prompt_template)

# per-call state
_sessions: Dict[str, Dict] = {}       # call_id -> {"active": bool, "current": List[str], "claims": List[str]}
_locks: Dict[str, asyncio.Lock] = {}  # call_id -> asyncio.Lock

# injected callbacks from main (optional)
_hangup_cb = None     # async def (call_id: str) -> None
_tts_cb = None        # async def (text: str, call_id: str) -> None
_dtmf_cb = None        # async def (dtmf: str, call_id: str) -> None

def register_hangup(cb):
    """Main should call this once: claims_agent.register_hangup(hangup_call)"""
    global _hangup_cb
    _hangup_cb = cb

def register_tts(cb):
    """Main must call once: claims_agent.register_tts(speak_with_azure)"""
    global _tts_cb
    _tts_cb = cb

def register_dtmf(cb):
    """Main should call once: claims_agent.register_dtmf(send_dtmf)"""
    global _dtmf_cb
    _dtmf_cb = cb

def is_active(call_id: str) -> bool:
    s = _sessions.get(call_id)
    return bool(s and s.get("active"))

async def start_session(call_id: str):
    _sessions[call_id] = {"active": True, "current": [], "claims": [], "last_response": "", "full_transcript": []}
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
    Append -> send tail (last N chars) of CURRENT claim to GPT-4o -> act on keyword.
    """
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return

    lock = _locks.setdefault(call_id, asyncio.Lock())
    async with lock:
        # 1) buffer the current claim only
        s["current"].append(utterance)
        s["full_transcript"].append(utterance) # For overall transcript

                # 2) Decide what to send to GPT based on insurance
        insurance_name = config_manager.get_insurance_name()
        if  insurance_name.upper() == "OSCAR"  or insurance_name.upper() == "HEALTH_FIRST":
            # For Oscar: send ONLY the latest final utterance
            chunk = utterance.strip()
        else:
            # 2) build transcript and take a small tail for the controller for all other insurances
            # full_transcript = " ".join(s["current"])  
            full_text = " ".join(s["full_transcript"])  # ALL claims combined
            tail_chars = get_claims_tail_chars()
            chunk = full_text[-tail_chars:].strip()   # e.g., last 250 chars for Cigna

        last_response = s.get("last_response", "")


        # 3) ask GPT for ONE WORD intent
        intent = await _ask_gpt_keyword(call_id, chunk,last_response)
        s["last_response"] = intent

                # 4) Handle DTMF responses FIRST (NEW - add this block)
        if intent.startswith("DTMF:"):
            digit = intent.split(":")[1]
            if _dtmf_cb:
                try:
                    await _dtmf_cb(digit, call_id)
                    logger.info(f"[{call_id}] Sent DTMF: {digit}")
                except Exception as e:
                    logger.error(f"[{call_id}] Error sending DTMF: {e}")
            return


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

async def _ask_gpt_keyword(call_id: str, transcript_chunk: str, last_response: str) -> str:
    """
    Use GPT-4o ('4-o') to return ONE WORD:
    DETAILS, NEXT, STOP, CONFIRM, NO, or CONTINUE.
    # """
    # if not OPENAI_API_KEY:
    #     return "CONTINUE"
    
    prompt_template = get_controller_prompt_template()

    # Send last_response to ALL insurances
    # Only CIGNA's prompt template will actually use {last_response}
    # Other prompts will ignore it (no {last_response} placeholder)


    try:
        system_prompt = prompt_template.format_map({
            'transcript_chunk': transcript_chunk,
            'last_response': last_response or ""
        })
    except KeyError as e:
        # Fallback for prompts without {last_response}
        system_prompt = prompt_template.format(transcript_chunk=transcript_chunk)



    # print("\n========== SYSTEM PROMPT SENT TO GPT ==========\n")
    # print(system_prompt)
    # print("==============================================\n")
    print("Transcript chunk sent to GPT:", transcript_chunk)
    if last_response:
        print(f"Last response: {last_response}")


    # minimal logs: what we send + what we get
    #logger.info(f"[{call_id}] → GPT tail: {transcript_chunk}")

    try:
        t0 = time.perf_counter()
        raw = await _call_gpt_api(system_prompt)
        ms = (time.perf_counter() - t0) * 1000

        if not raw:
            return "CONTINUE"
        intent = _map_keyword(raw.upper())
        logger.info(f"[{call_id}] ← GPT: {raw!r} → {intent} ({ms:.0f}ms)")
        return intent

    except Exception as e:
        logger.exception(
            f"[{call_id}] GPT failure | transcript_len={len(transcript_chunk)} | last_response={last_response}"
        )
        return "CONTINUE"

def _map_keyword(upper: str) -> str:

    if "DTMF:" in upper:
        return upper  # Return as-is: "DTMF:1", "DTMF:2", etc.
        
    # Check for single digits (in case LLaMA returns just the number)
    if upper in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]:
        return f"DTMF:{upper}"

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
