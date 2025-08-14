# claims_agent.py
import asyncio
import httpx
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

LLAMA_URL = "http://20.172.5.137:9010/api/generate_response/"

# per-call state
_sessions: Dict[str, Dict] = {}      # call_id -> {"active":bool, "session":[], "current":[], "claims":[]}
_locks: Dict[str, asyncio.Lock] = {} # call_id -> lock

# injected callbacks from main (optional)
_hangup_cb = None   # async def (call_id: str) -> None
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

# start_session
async def start_session(call_id: str):
    _sessions[call_id] = {
        "active": True,
        "current": [],   # current claim-only chunks
        "claims": [],    # finalized claim strings
    }
    _locks[call_id] = _locks.get(call_id) or asyncio.Lock()
    logger.info(f"[{call_id}] 🟢 claims session START")


async def end_session(call_id: str):
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return
    async with _locks.setdefault(call_id, asyncio.Lock()):
        _finalize_current(s)
        s["active"] = False
        logger.info(f"[{call_id}] 🔴 claims session END — total claims: {len(s['claims'])}")
        if _hangup_cb is not None:
            try:
                await _hangup_cb(call_id)
            except Exception as e:
                logger.error(f"[{call_id}] hangup error: {e}")


def get_claims(call_id: str) -> list[str]:
    s = _sessions.get(call_id) or {}
    return list(s.get("claims", []))
# If you want one string: "\n\n---\n\n".join(get_claims(call_id))


def _finalize_current(s: Dict):
    txt = " ".join(s["current"]).strip()
    if txt:
        s["claims"].append(txt)
    s["current"].clear()  # <-- clears so the next claim starts clean


async def handle_final(call_id: str, utterance: str):
    """
    Main calls this for EVERY debounced Final while in claim mode.
    Append -> send ONLY current-claim transcript to Llama -> act on keyword.
    """
    s = _sessions.get(call_id)
    if not s or not s.get("active"):
        return

    lock = _locks.setdefault(call_id, asyncio.Lock())
    async with lock:
        # 1) Append to current-claim buffer ONLY
        s["current"].append(utterance)

        # 2) Build transcript from current claim (no history)
        transcript = "\n".join(s["current"])
        preview = "\n".join([ln for ln in transcript.splitlines() if ln.strip()][-4:])
        logger.info(f"[{call_id}] 📨 to Llama (current-claim last lines):\n{preview}")

        # 3) Ask Llama for a ONE-WORD intent
        intent = await _ask_llama_keyword(transcript)
        logger.info(f"[{call_id}] 📬 Llama intent: {intent!r}")

        # 4) Act (STOP > NEXT > CONFIRM > CONTINUE)
        if intent == "STOP":
            _finalize_current(s)
            await end_session(call_id)
            return

        if intent == "NEXT":
            _finalize_current(s)                # store claim N and clear buffer
            if _tts_cb:
                try:
                    await _tts_cb("Next claim", call_id)
                except Exception as e:
                    logger.error(f"[{call_id}] TTS error (Next claim): {e}")
            return

        if intent == "CONFIRM":
            if _tts_cb:
                try:
                    await _tts_cb("Yes", call_id)
                except Exception as e:
                    logger.error(f"[{call_id}] TTS error (Yes): {e}")
            return

        # CONTINUE (or unknown): do nothing, keep buffering this claim
        return





async def _ask_llama_keyword(full_transcript: str) -> str:
    """
    Return ONE WORD: NEXT, STOP, CONFIRM, or CONTINUE.
    Default to CONTINUE on errors.
    """
    prompt = f"""
You are a controller for an insurance IVR claim-reading segment.
You will receive the CURRENT CLAIM transcript (concatenated lines).
Reply with EXACTLY ONE WORD (no quotes, no punctuation):

- NEXT      → this claim just finished and the IVR is offering another claim
  (e.g., transcript includes "you can say repeat that or next claim")
  *** Importnt**** You cannot say next until there is an explicit work "next claim" in the transcript.

- STOP      → there are no more claims / going to main menu / hang up / transfer / office closed
    *****(e.g., transcript includes "would you like me to find a different claim?" or "hang up now" or "you want to search for something else?")***
    *****(e.g., transcript includes "if there's nothing else you can just hang up", or )***

- CONFIRM   → say "Yes" to prompt the IVR to read the claim info
  ***(e.g., transcript includes "do you want to hear the claim details?"   or "would you like to hear claim line details")***

- CONTINUE  → keep listening; the claim details are still being read
  ***(e.g., transcript includes "Data about the claim, like claim number and dates abou the claim? and no question in the end")***


TRANSCRIPT:
\"\"\"{full_transcript}\"\"\"
""".strip()

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                LLAMA_URL,
                json={
                    "doctor_query": prompt,
                    "role": "controller",
                    "max_new_tokens": 4
                },
            )
        raw = (resp.json().get("response") or "").strip()
        logger.info(f"🧾 Llama raw: {raw!r}")

        upper = raw.upper()
        if "NEXT" in upper:
            return "NEXT"
        if "STOP" in upper or "END" in upper or "HANG" in upper:
            return "STOP"
        if "CONFIRM" in upper or "YES" in upper or "HEAR CLAIM" in upper:
            return "CONFIRM"
        return "CONTINUE"
    except Exception as e:
        logger.error(f"Llama keyword error: {e}")
        return "CONTINUE"
