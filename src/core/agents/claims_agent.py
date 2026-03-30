import os
import time
import asyncio
import httpx
import logging
from typing import Dict, List
from dotenv import load_dotenv
from src.config.insurance_config import config_manager
from ..prompts.claims_prompts import get_claims_prompt
from src.services.llm_service import _call_gpt_api
load_dotenv()
logger = logging.getLogger(__name__)


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
_active_calls = None

def register_active_calls(active_calls):
    global _active_calls
    _active_calls = active_calls

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

        # Full claim transcript
        full_claims_text = "\n\n--- CLAIM BREAK ---\n\n".join(s.get("claims", []))

        # Raw full transcript
        raw_full_transcript = " ".join(s.get("full_transcript", []))

        logger.info(f"\n========== CALL ENDED: {call_id} ==========")
        logger.info(f"\nFULL CLAIMS TRANSCRIPT:\n{full_claims_text if full_claims_text else '[No claims captured]'}")
        logger.info(f"\nRAW FULL TRANSCRIPT:\n{raw_full_transcript if raw_full_transcript else '[No transcript captured]'}")

        # Conversation history (if available from active calls)
        try:
            if _active_calls:
                call_state = _active_calls.get(call_id)
            else:
                call_state = None
        except Exception:
            call_state = None

        # Store finalized claims data and full transcript on call_state
        if call_state:
            call_state.finalized_claims = s.get("claims", [])
            call_state.full_claims_transcript = full_claims_text
            call_state.raw_full_transcript = raw_full_transcript

        conv_lines = []
        if call_state and getattr(call_state, "conversation_history", None):
            clean_steps = []
            for step in call_state.conversation_history:
                if not isinstance(step, dict):
                    continue

                t = (step.get("transcript") or "").strip()
                r = (step.get("gpt_result") or "").strip()

                # Skip malformed/old entries that don't match the new schema
                if not t and not r:
                    continue

                clean_steps.append({
                    "transcript": t,
                    "gpt_result": r,
                })
            for i, step in enumerate(clean_steps, start=1):
                conv_lines.append(
                    f"{i}. Transcript: {step['transcript']}\n   GPT: {step['gpt_result']}"
                )
        conv_text = "\n".join(conv_lines)
        logger.info(
            f"\nCONVERSATION HISTORY ({len(conv_lines)} steps):\n"
            f"{conv_text if conv_text else '[No conversation history]'}"
        )
        logger.info("===========================================")

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


def _append_conversation_step(call_id: str, transcript: str, gpt_result: str):
    if not _active_calls:
        return

    call_state = _active_calls.get(call_id)
    if not call_state:
        return

    transcript = (transcript or "").strip()
    gpt_result = (gpt_result or "").strip()

    # don't add empty rows
    if not transcript and not gpt_result:
        return

    call_state.conversation_history.append({
        "transcript": transcript,
        "gpt_result": gpt_result
    })
async def handle_final(call_id: str, utterance: str):
    """
    Main calls this for EVERY debounced Final while in claim mode.
    Append -> send tail (last N chars) of transcript to GPT -> act on keyword.
    """
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return

    utterance = (utterance or "").strip()
    if not utterance:
        return

    lock = _locks.setdefault(call_id, asyncio.Lock())
    async with lock:
        # Buffer raw claim transcript
        s["current"].append(utterance)
        s["full_transcript"].append(utterance)

        insurance_name = config_manager.get_insurance_name()

        # Full transcript for conversation history (what was actually said)
        full_text = " ".join(s["full_transcript"]).strip()

        # This is what GPT should see (trimmed for context window)
        if insurance_name.upper() in ("OSCAR", "HEALTH_FIRST"):
            chunk = utterance
        else:
            tail_chars = get_claims_tail_chars()
            chunk = full_text[-tail_chars:].strip()

        last_response = s.get("last_response", "")

        intent = await _ask_gpt_keyword(
            call_id,
            chunk,
            last_response,
            review_text=full_text,
        )
        s["last_response"] = intent

        if intent.startswith("DTMF:"):
            digit = intent.split(":", 1)[1]
            if _dtmf_cb:
                try:
                    await _dtmf_cb(digit, call_id)
                    logger.info(f"[{call_id}] Sent DTMF: {digit}")
                except Exception as e:
                    logger.error(f"[{call_id}] Error sending DTMF: {e}")
            return

        if intent == "STOP":
            await end_session(call_id, already_locked=True)
            return

        if intent == "NEXT":
            _finalize_current(s)
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

        return

# ---------- GPT-4o controller (minimal logging) ----------

async def _ask_gpt_keyword(
    call_id: str,
    transcript_chunk: str,
    last_response: str,
    review_text: str | None = None,
) -> str:
    """
    Use GPT to return one control intent.
    """
    prompt_template = get_controller_prompt_template()

    try:
        system_prompt = prompt_template.format_map({
            "transcript_chunk": transcript_chunk,
            "last_response": last_response or "",
        })
    except KeyError:
        system_prompt = prompt_template.format(transcript_chunk=transcript_chunk)

    logger.info(f"Transcript chunk sent to GPT: {transcript_chunk}")
    if last_response:
        logger.info(f"Last response: {last_response}")

    try:
        t0 = time.perf_counter()
        raw = await _call_gpt_api(system_prompt)
        ms = (time.perf_counter() - t0) * 1000

        if not raw:
            intent = "CONTINUE"
            _append_conversation_step(call_id, review_text or transcript_chunk, intent)
            return intent

        intent = _map_keyword(raw.upper())
        _append_conversation_step(call_id, review_text or transcript_chunk, intent)
        logger.info(f"[{call_id}] ← GPT: {raw!r} → {intent} ({ms:.0f}ms)")
        return intent

    except Exception as e:
        logger.error(f"[{call_id}] GPT controller error: {e}")
        intent = "CONTINUE"
        _append_conversation_step(call_id, review_text or transcript_chunk, intent)
        return intent
    

def _map_keyword(upper: str) -> str:

    if "DTMF:" in upper:
        return upper  # Return as-is: "DTMF:1", "DTMF:2", etc.
        
    # Check for single digits (in case LLaMA returns just the number)
    if upper in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]:
        return f"DTMF:{upper}"

    if "DETAIL" in upper:
        return "DETAILS"
    if  upper == "NEXT":
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
