# # claims_agent.py
# import asyncio
# import httpx
# import logging
# from typing import Dict, List, Optional

# logger = logging.getLogger(__name__)

# LLAMA_URL = "http://20.172.5.137:4010/api/generate_response/"

# # per-call state
# _sessions: Dict[str, Dict] = {}      # call_id -> {"active":bool, "session":[], "current":[], "claims":[]}
# _locks: Dict[str, asyncio.Lock] = {} # call_id -> lock

# # injected callbacks from main (optional)
# _hangup_cb = None   # async def (call_id: str) -> None
# _tts_cb = None        # async def (text: str, call_id: str) -> None


# def register_hangup(cb):
#     """Main should call this once: claims_agent.register_hangup(hangup_call)"""
#     global _hangup_cb
#     _hangup_cb = cb

# def register_tts(cb):
#     """Main must call once: claims_agent.register_tts(speak_with_azure)"""
#     global _tts_cb
#     _tts_cb = cb


# def is_active(call_id: str) -> bool:
#     s = _sessions.get(call_id)
#     return bool(s and s.get("active"))

# # start_session
# async def start_session(call_id: str):
#     _sessions[call_id] = {
#         "active": True,
#         "current": [],   # current claim-only chunks
#         "claims": [],    # finalized claim strings
#     }
#     _locks[call_id] = _locks.get(call_id) or asyncio.Lock()
#     logger.info(f"[{call_id}] 🟢 claims session START")


# async def end_session(call_id: str, *, already_locked: bool = False):
#     s = _sessions.get(call_id)
#     if not s or not s.get("active"):
#         return

#     if already_locked:
#         # we already hold _locks[call_id] in the caller
#         _finalize_current(s)                 # finalize ONCE here
#         s["active"] = False
#         logger.info(f"[{call_id}] 🔴 claims session END — total claims: {len(s['claims'])}")
#         if _hangup_cb:
#             try:
#                 await _hangup_cb(call_id)
#             except Exception as e:
#                 logger.error(f"[{call_id}] hangup error: {e}")
#         return

#     # normal path (we don't hold the lock yet)
#     async with _locks.setdefault(call_id, asyncio.Lock()):
#         _finalize_current(s)                 # finalize ONCE here
#         s["active"] = False
#         logger.info(f"[{call_id}] 🔴 claims session END — total claims: {len(s['claims'])}")
#         if _hangup_cb:
#             try:
#                 await _hangup_cb(call_id)
#             except Exception as e:
#                 logger.error(f"[{call_id}] hangup error: {e}")


# def get_claims(call_id: str) -> list[str]:
#     s = _sessions.get(call_id) or {}
#     return list(s.get("claims", []))
# # If you want one string: "\n\n---\n\n".join(get_claims(call_id))


# def _finalize_current(s: Dict):
#     txt = " ".join(s["current"]).strip()
#     if txt:
#         s["claims"].append(txt)
#     s["current"].clear()  # <-- clears so the next claim starts clean


# async def handle_final(call_id: str, utterance: str):
#     """
#     Main calls this for EVERY debounced Final while in claim mode.
#     Append -> send ONLY last 30 chars of current-claim transcript to Llama -> act on keyword.
#     """
#     s = _sessions.get(call_id)
#     if not s or not s.get("active"):
#         return

#     lock = _locks.setdefault(call_id, asyncio.Lock())
#     async with lock:
#         # 1) Append to current-claim buffer ONLY
#         s["current"].append(utterance)

#         # 2) Build full transcript from current claim (store everything)
#         full_transcript = " ".join(s["current"])
        
#         # 3) Send only last 30 characters to Llama for decision
#         transcript_for_llama = full_transcript[-180:].strip()
        
#         # 4) Enhanced logging
#         logger.info(f"[{call_id}] 📊 Full transcript length: {len(full_transcript)} chars")
#         logger.info(f"[{call_id}] 📨 Sending to Llama (last 180 chars): '{transcript_for_llama}'")

#         # 5) Ask Llama for a ONE-WORD intent
#         intent = await _ask_llama_keyword(transcript_for_llama)
#         logger.info(f"[{call_id}] 📬 Llama intent: {intent!r}")

#         # 6) Act (STOP > NEXT > DETAILS > CONTINUE)
#         if intent == "STOP":
#             await end_session(call_id, already_locked=True)
#             return

#         if intent == "NEXT":
#             _finalize_current(s)                # store claim N and clear buffer
#             if _tts_cb:
#                 try:
#                     await _tts_cb("Next claim", call_id)
#                 except Exception as e:
#                     logger.error(f"[{call_id}] TTS error (Next claim): {e}")
#             return

#         if intent == "DETAILS":
#             if _tts_cb:
#                 try:
#                     await _tts_cb("Details", call_id)
#                 except Exception as e:
#                     logger.error(f"[{call_id}] TTS error (Details): {e}")
#             return

#         if intent == "CONFIRM":
#             if _tts_cb:
#                 try:
#                     await _tts_cb("Yes", call_id)
#                 except Exception as e:
#                     logger.error(f"[{call_id}] TTS error (Yes): {e}")
#             return
        
#         if intent == "NO":
#             if _tts_cb:
#                 try:
#                     await _tts_cb("No", call_id)
#                 except Exception as e:
#                     logger.error(f"[{call_id}] TTS error (No): {e}")
#             return

#         # CONTINUE (or unknown): do nothing, keep buffering this claim
#         return


# async def _ask_llama_keyword(transcript_chunk: str) -> str:
#     """
#     Return ONE WORD: DETAILS, NEXT, STOP, or CONTINUE.
#     Default to CONTINUE on errors.
#     """
#     prompt = f"""
# You are an IVR controller. To respond to Claims data.

# ***Important:***

#  *** LOOK FOR THE KEYWORDS BELOW IN THE TRANSCRIPT ACCORDING TO THE PRIORITY, AND TAKE ACTION ACCORDINGLY***
#  *** NEVER RETURN "CONTINUE" IF THE TRANSCRIPT HAS "YOU CAN SAY" OR "PRESS" OPTIONS, ALWAYS RETURN THE FIRST 3 OPTIONS***
#  *** Before returning anything make sure the transcript has the exact keywords mentioned, ***

# CHECK IN ORDER:
# **PRIORITY 1. If transcript has keywords "details" or "more details" → DETAILS **
# **PRIORITY 2. If transcript has key words "next claim" OR "next item" OR "next" → NEXT**
# **PRIORITY 3. If no options/questions → CONTINUE**
# **PRIORITY 4. If the transcript have options and you cannot find keywords for any of the above, then just send → STOP**

# EXAMPLES:
# "hear ""****details****"" fax it next claim" → DETAILS
# "you can say repeat that, fax the full list next item or stop list" → NEXT  
# "find a different claim"  OR  "you can say repeat that fax the full list or stop list" → STOP
# "you can say repeat that fax the full list previous item or stop list" → STOP

# ***Important:***
#  *** NEVER RETURN "CONTINUE" IF THE TRANSCRIPT HAS "YOU CAN SAY" OR "PRESS" OPTIONS, THEN ALWAYS RETURN FROM THE FIRST 3 OPTIONS***
#  *** Before returning anything make sure the transcript has the exact keywords mentioned, ***

 

# Read this transcript carefully and check for the keywords in the order of priority above.

# TRANSCRIPT: "{transcript_chunk}"

# Reply with ONE WORD: DETAILS, NEXT, STOP, or CONTINUE
# Answer:""".strip()

#     try:
#         async with httpx.AsyncClient(timeout=8.0) as client:
#             resp = await client.post(
#                 LLAMA_URL,
#                 json={
#                     "doctor_query": prompt,
#                     "role": "controller",
#                     "max_new_tokens": 4
#                 },
#             )
#         raw = (resp.json().get("response") or "").strip()
#         logger.info(f"🧾 Llama raw response: {raw!r}")

#         upper = raw.upper()
#         if "DETAILS" in upper:
#             return "DETAILS"
#         if "NEXT" in upper:
#             return "NEXT"
#         if "STOP" in upper or "END" in upper or "HANG" in upper:
#             return "STOP"
#         if "CONFIRM" in upper or "YES" in upper:
#             return "CONFIRM"
#         if "NO" in upper:
#             return "NO"
#         return "CONTINUE"
#     except Exception as e:
#         logger.error(f"Llama keyword error: {e}")
#         return "CONTINUE"









# claims_agent.py — GPT-4o controller (drop-in)
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

# ==== YOUR PROMPT (unchanged). DO NOT EDIT THE TEXT, we just format {transcript_chunk} ====
CONTROLLER_PROMPT_TEMPLATE = """
You are an IVR controller. To respond to Claims data.

***Important:***

 *** LOOK FOR THE KEYWORDS BELOW IN THE TRANSCRIPT ACCORDING TO THE PRIORITY, AND TAKE ACTION ACCORDINGLY***
 *** NEVER RETURN "CONTINUE" IF THE TRANSCRIPT HAS "YOU CAN SAY" OR "PRESS" OPTIONS, ALWAYS RETURN THE FIRST 3 OPTIONS***
 *** Before returning anything make sure the transcript has the exact keywords mentioned, ***

CHECK IN ORDER:
**PRIORITY 1. If transcript has keywords "details" or "more details" → DETAILS **
**PRIORITY 2. If transcript has key words "next claim" OR "next item" OR "next" → NEXT**
**PRIORITY 3. If no options/questions → CONTINUE**
**PRIORITY 4. If the transcript have options and you cannot find keywords for any of the above, then just send → STOP**

EXAMPLES:
"hear ""****details****"" fax it next claim" → DETAILS
"you can say repeat that, fax the full list next item or stop list" → NEXT  
"find a different claim"  OR  "you can say repeat that fax the full list or stop list" → STOP
"you can say repeat that fax the full list previous item or stop list" → STOP

***Important:***
 *** NEVER RETURN "CONTINUE" IF THE TRANSCRIPT HAS "YOU CAN SAY" OR "PRESS" OPTIONS, THEN ALWAYS RETURN FROM THE FIRST 3 OPTIONS***
 *** Before returning anything make sure the transcript has the exact keywords mentioned, ***

 

Read this transcript carefully and check for the keywords in the order of priority above.

TRANSCRIPT: "{transcript_chunk}"

Reply with ONE WORD: DETAILS, NEXT, STOP, or CONTINUE
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
        chunk = full_transcript[-250:].strip()

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
    if upper == "NO" or " NO" in upper or upper.startswith("NO"):
        return "NO"
    return "CONTINUE"
