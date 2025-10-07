import asyncio
import json
import base64
import uuid
import httpx
import time
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
import logging
from azure_stt_service import stt_manager, convert_mulaw_to_pcm, AzureRealtimeSttService
from pydantic import BaseModel
import re
#from prompt import PROMPT_TEMPLATE
import claims_agent
from insurance_config import config_manager
from prompt import get_main_prompt_template
from Data_models import CallState, SimpleCallRequest
from services import telnyx_client
from routes.orchestrate import make_orchestrate_router
from routes.webhooks import make_webhooks_router
from routes.stream import make_stream_router

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_BASE_URL = "https://api.telnyx.com/v2"
#TEL_TO = os.getenv("TEL_TO")  # Number to call
TEL_FROM = os.getenv("TEL_FROM")  # Your Telnyx number
CALL_CONTROL_APP_ID = os.getenv("CALL_CONTROL_APP_ID")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")  # Your server URL
STREAM_BASE_URL = WEBHOOK_BASE_URL.replace("https://", "wss://")
AZURE_SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")

TEL_TO = config_manager.get_phone_number()  

DEBOUNCE_SECONDS = config_manager.get_debounce_seconds()
CLAIM_DEBOUNCE_SECONDS = config_manager.get_claim_debounce_seconds()

#DEBOUNCE_SECONDS = 0.1  # baseline for cigna
#CLAIM_DEBOUNCE_SECONDS = 1.2  # when in claim mode for cigna 


HEADERS = {
    "Authorization": f"Bearer {TELNYX_API_KEY}",
    "Content-Type": "application/json"
}

app = FastAPI()



logging.basicConfig(
    level=logging.INFO,  # Use logging.DEBUG for even more detail
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

logger = logging.getLogger(__name__)


initiated_events: Dict[str, asyncio.Event] = {}


# Global state management
active_calls: Dict[str, CallState] = {}


# --- Unified, idempotent cleanup for a call ---
async def ensure_call_cleanup(call_control_id: str, *, reason: str, send_hangup: bool):
    """
    Safe to call from: webhook, WebSocket finally, auto-hangup, shutdown, or any early-exit.
    - Ends claims session (if active)
    - Cleans Azure STT session
    - Optionally issues a hangup to Telnyx (when WE need to end it)
    - Clears flags and removes from active_calls
    Idempotent: calling this multiple times is safe.
    """
    cs = active_calls.get(call_control_id)
    if not cs:
        return

    # Lazily add guard fields to CallState to avoid changing your dataclass
    if not hasattr(cs, "cleanup_lock"):
        import asyncio as _asyncio
        cs.cleanup_lock = _asyncio.Lock()
    if not hasattr(cs, "cleanup_done"):
        cs.cleanup_done = False

    async with cs.cleanup_lock:
        if cs.cleanup_done:
            return

        logger.info(f"🧹 Cleanup [{call_control_id}] due to: {reason}")

        # 1) Stop claims flow (safe if already ended)
        try:
            await claims_agent.end_session(call_control_id)
        except Exception as e:
            logger.warning(f"[{call_control_id}] end_session error (ignored): {e}")

        # 2) Stop/cleanup STT session
        try:
            if getattr(cs, "azure_stt_session", None):
                stt_manager.remove_session(cs.websocket_id)
        except Exception as e:
            logger.warning(f"[{call_control_id}] STT cleanup error (ignored): {e}")

        # 3) If WE should send hangup (e.g., WS died first / auto timeout), do it
        #    If webhook already confirmed hangup, skip issuing it again.
        if send_hangup and getattr(cs, "status", "") not in ("hangup", "ended"):
            try:
                await hangup_call(call_control_id)
            except Exception as e:
                logger.warning(f"[{call_control_id}] hangup_call error (ignored): {e}")

        # 4) Clear flags and forget this call
        cs.claim_mode = False
        cs.cleanup_done = True
        active_calls.pop(call_control_id, None)

        logger.info(f"✅ Cleanup complete [{call_control_id}]")




CLAIM_NOT_FOUND_TRIGGERS = [
    "i couldn't find any claims",
    "i did not find any claims",
    "I didn't find any claims on that date",
    "I did not find any claims on that date",
    "no claims found",
    "there are no claims on that date",
    "no matching claims",
    "i’m not seeing any claims for that",
]
def is_claim_not_found(text: str) -> bool:
    return any(phrase in text.lower() for phrase in CLAIM_NOT_FOUND_TRIGGERS)
#### Claims helper functions 
# --- Claim-capture triggers (keep tight & cheap) ---
CLAIM_START_TRIGGERS = [
    "i found your claim",
    "i found a claim",
    "i found two claims",
    "i found 2 claims",
    "here's the first one",
    "here is the first one",
    "the first one was for service",
    "the first claim",
]

def is_claim_start(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in CLAIM_START_TRIGGERS)



# # ——— STREAMING ENDPOINT ————————————————————————————————————————

# @app.websocket("/stream")
# async def media_stream_endpoint(websocket: WebSocket):
#     logger.info("🔗 New WebSocket connection")
#     await websocket.accept()

#     call_control_id = None
#     call_state      = None
#     websocket_id    = str(uuid.uuid4())

#     # ───────── Debounce state (per-connection) ─────────
#     from types import SimpleNamespace
#     state = SimpleNamespace(
#         pending_finals=[],                                  # accumulate final STT chunks here
#         debounce_task=None,                                 # the timer task we cancel/restart
#         debounce_time=DEBOUNCE_SECONDS,                  # how long to wait for "silence" before processing
#     )

#     async def _process_after_quiet():
#         """
#         Runs after a short quiet gap. If not cancelled by new audio,
#         it joins pending final chunks and treats them as one utterance.
#         """
#         try:
#             await asyncio.sleep(state.debounce_time)        # wait for "silence" gap
#         except asyncio.CancelledError:
#             return                                          # new speech arrived → timer reset

#         if not state.pending_finals:
#             return

#         text = " ".join(state.pending_finals).strip()
#         state.pending_finals.clear()
#         if not text:
#             return

#         # optional latency logging
#         if call_state and hasattr(call_state, "last_media_ts"):
#             ms = (time.perf_counter() - call_state.last_media_ts) * 1000
#             logger.info(f" Debounced STT latency: {ms:.0f} ms")

#         # persist and dispatch
#         if call_state:
#             call_state.conversation_history.append({"role": "user", "content": text})
#         await handle_user_speech(text, call_control_id)

#     def _reschedule_debounce():
#         """Cancel current timer (if any) and start a fresh one."""
#         if state.debounce_task and not state.debounce_task.done():
#             state.debounce_task.cancel()
#         state.debounce_task = asyncio.create_task(_process_after_quiet())

#     async def _flush_pending_now():
#         """
#         Force-process whatever we have (used on 'stop' or disconnect) so we
#         don’t lose the caller’s last utterance.
#         """
#         if state.debounce_task and not state.debounce_task.done():
#             state.debounce_task.cancel()

#         if state.pending_finals:
#             text = " ".join(state.pending_finals).strip()
#             state.pending_finals.clear()
#             if text:
#                 if call_state and hasattr(call_state, "last_media_ts"):
#                     ms = (time.perf_counter() - call_state.last_media_ts) * 1000
#                     logger.info(f" Debounced STT latency (flush): {ms:.0f} ms")
#                 if call_state:
#                     call_state.conversation_history.append({"role": "user", "content": text})
#                 await handle_user_speech(text, call_control_id)

#     # ───────── Azure STT callbacks ─────────
#     async def on_partial(text: str):
#         # NEW: sync dynamic debounce (per call)
#         if call_state:
#             desired = getattr(call_state, "debounce_seconds", DEBOUNCE_SECONDS)
#             if state.debounce_time != desired:
#                 state.debounce_time = desired
#             if getattr(call_state, "need_debounce_reset", False):
#                 _reschedule_debounce()
#                 call_state.need_debounce_reset = False

#         # partials are unstable; we use them only to reset the quiet timer
#         _reschedule_debounce()


#     async def on_final(text: str):
#         text = text.strip()
#         if not text:
#             return

#         # NEW: sync dynamic debounce (per call)
#         if call_state:
#             desired = getattr(call_state, "debounce_seconds", DEBOUNCE_SECONDS)
#             if state.debounce_time != desired:
#                 state.debounce_time = desired
#             if getattr(call_state, "need_debounce_reset", False):
#                 _reschedule_debounce()
#                 call_state.need_debounce_reset = False

#         # optional: measure time since last inbound audio
#         if call_state and hasattr(call_state, "last_media_ts"):
#             ms = (time.perf_counter() - call_state.last_media_ts) * 1000
#             logger.info(f" STT final piece latency: {ms:.0f} ms")

#         state.pending_finals.append(text)  # accumulate stable text
#         _reschedule_debounce()             # restart quiet timer

#     async def on_error(err: str):
#         logger.error(f"[{websocket_id}] STT error: {err}")

#     # ───────── WebSocket receive loop ─────────
#     try:
#         while True:
#             frame = await websocket.receive_text()
#             msg   = json.loads(frame)
#             ev    = msg.get("event")

#             if ev == "start":
#                 call_control_id = msg["start"]["call_control_id"]
#                 logger.info(f" Call started: {call_control_id}")

#                 call_state = active_calls.get(call_control_id)
#                 if not call_state:
#                     logger.warning(f" Unknown call ID")
#                     continue

#                 # allow TTS to send outbound audio on the same socket
#                 call_state.websocket = websocket

#                 # create and wire the Azure STT session
#                 session = stt_manager.create_session(websocket_id)
#                 call_state.azure_stt_session = session
#                 session.initialize(
#                     on_partial_result=on_partial,
#                     on_final_result=on_final,
#                     on_error=on_error,
#                 )
#                 session.start_continuous_recognition()
#                 session.start_async_event_handler(asyncio.get_running_loop())

#             elif ev == "media":
#                 media = msg["media"]
#                 if media.get("track") == "inbound" and call_state:
#                     pcm = convert_mulaw_to_pcm(base64.b64decode(media["payload"]))
#                     call_state.last_media_ts = time.perf_counter()
#                     if getattr(call_state, "is_tts_active", False):
#                         pass
#                     else:
#                         call_state.azure_stt_session.feed_audio(pcm)

#             elif ev == "stop":
#                 logger.info(f" Stream stopped")
#                 await _flush_pending_now()  # process last utterance, if any
#                 try:
#                     await claims_agent.end_session(call_control_id)
#                 except Exception:
#                     pass   
#                 if call_state:
#                     call_state.claim_mode = False   # NEW
          
#                 break

#     except WebSocketDisconnect:
#         if call_state:
#             call_state.status = "websocket_close"
#         logger.info(f" WebSocket disconnected")

#     except Exception as e:
#         logger.error(f" Stream error: {e}")
#     finally:
#         # Flush any pending STT chunks into one last utterance
#         try:
#             await _flush_pending_now()
#         except Exception:
#             pass

#         # If the socket closed first (common), we do a full cleanup and also send hangup.
#         # If the webhook already ran and removed the call, this will no-op.
#         if call_control_id and call_control_id in active_calls:
#             try:
#                 await ensure_call_cleanup(
#                     call_control_id,
#                     reason="websocket: finally/disconnect",
#                     send_hangup=True
#                 )
#             except Exception as e:
#                 logger.error(f"[{websocket_id}] ensure_call_cleanup error: {e}")




# ─── 1. handle_user_speech: decorate transcript into a full prompt ────────────

async def handle_user_speech(transcript: str, call_control_id: str):
    text = transcript.strip()
    if not transcript or len(transcript) < 3:
        logger.warning(f"Transcript too short, skipping")
        return

    call_state = active_calls.get(call_control_id)

    # ── claim routing (the only logic in main) ──────────────────────────────
    if call_state:
        if is_claim_not_found(text):
            logger.info("❌ No claims found for this patient. Ending call.")
            await ensure_call_cleanup(call_control_id, reason="claims: not found", send_hangup=True)
            return


        # ENTER claim mode
        if not call_state.claim_mode and is_claim_start(text):
            call_state.claim_mode = True
            # NEW: bump debounce while in claims flow
            print("dEBOUNCE TIME CHANGES")
            call_state.debounce_seconds = config_manager.get_claim_debounce_seconds()
            call_state.need_debounce_reset = True

            await claims_agent.start_session(call_control_id)
            await claims_agent.handle_final(call_control_id, text)  # send first debounced chunk
            return

        # STAY/EXIT claim mode
        if call_state.claim_mode:
            # while in claim mode, every debounced chunk goes to claims.py
            await claims_agent.handle_final(call_control_id, text)

            # if the claims session ended, drop out and revert debounce
            if hasattr(claims_agent, "is_active") and not claims_agent.is_active(call_control_id):
                call_state.claim_mode = False
                call_state.debounce_seconds = config_manager.get_debounce_seconds()  # revert to baseline
                call_state.need_debounce_reset = True
            return



    prompt_template = get_main_prompt_template()  # Gets correct template for current insurance
    prompt = prompt_template.format(
        transcript=transcript,
        tax_id="833613394",
        npi= "1407891245",
        customer_id= "H44918729",
        dob=  "8/7/1945",
        member_name= "PAUL HESS",
        dos="1/23/2025"
    )


    t0 = time.perf_counter()
    response = await call_llama_api(prompt)
    llama_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"Llama latency: {llama_ms:.0f} ms")
    logger.info(f"Llama response: {response!r}")

    await process_llama_response(response, call_control_id)




async def call_llama_api(prompt: str) -> str:
    """
    Call your Llama API, log the raw JSON and parsed field,
    and return the 'response' or a default marker.
    """
    url = "http://20.172.5.137:9010/api/generate_response/"
    payload = {
        "doctor_query": prompt,
        "role": "You are an outbound calling agent for insurance IVR handling claim status calls.",
        "max_new_tokens": 100
    }

    try:
        
        #logger.info(f"🦙 Sending prompt to Llama: {prompt!r}")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)

            # resp.json() is synchronous, so don’t await it
            raw = resp.json()
            #logger.info(f"🦙 Llama API raw JSON (status {resp.status_code}): {raw!r}")

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





async def process_llama_response(response: str, call_control_id: str):
    """
      - speak only for say/value/confirm
      - send dtmf only for dtmf
      - hangup on end/endcall/hangup
      - ignore everything else (incl. 'fallback')
    """
    if not response:
        logger.info("❗ Llama returned empty response")
        return

    s = response.strip().strip("`").strip()
    if not s:
        return

    # If it's just a quoted string, treat it as: say <text>
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = f"say {s[1:-1].strip()}"

    low = s.lower()

    # Helper: text after keyword (first occurrence), skipping separators and optional quotes
    def after(keyword: str):
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

    # Ignore explicit "fallback" (do nothing)
    if low == "fallback" or low.startswith("fallback "):
        logger.info("Llama returned fallback; ignoring")
        return

    # 1) DTMF (explicit only)
    if low.startswith("dtmf"):
        tail = after("dtmf") or ""
        digits = "".join(ch for ch in tail if ch.isdigit() or ch in "*#")
        if digits:
            logger.info(f"→ Sending DTMF: {digits}")
            await send_dtmf(digits, call_control_id)
        else:
            logger.info("DTMF payload empty after sanitizing; ignoring")
        return

    # 2) SAY / VALUE / CONFIRM → speak
    for kw in ("say", "value", "confirm"):
        val = after(kw)
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


async def send_dtmf(digits: str, call_control_id: str):
    """Send DTMF tones to the call (digits already sanitized by caller)."""
    try:
        cleaned = "".join(ch for ch in digits if ch.isdigit() or ch in "*#")
        # If you need durations, we can extend telnyx_client to accept them.
        await telnyx_client.send_dtmf(call_control_id, cleaned, TELNYX_BASE_URL, HEADERS)
        logger.info(f"✅ DTMF sent: {cleaned}")
    except Exception as e:
        logger.error(f"❌ Error sending DTMF: {str(e)}")






# --- tiny-pause spelling (no global slowdown) ---
PAUSE_MS_EACH       = 120    # pause between EACH letter/digit
NUMERIC_HEAVY_RATIO = 0.60   # ~60% digits → treat as code/number

# simple date matcher: M/D/YY, MM/DD/YYYY, etc. (with slashes)
DATE_SLASH_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")

def _is_date_token(s: str) -> bool:
    """Detects simple slash-based dates like 06/05/2024 (any M/D/YY or MM/DD/YYYY)."""
    return bool(DATE_SLASH_RE.match(s))

def _is_code_token(s: str) -> bool:
    """
    Single token (no spaces) that looks like a code or long number
    — but NOT a slash date. We explicitly exclude '/' so dates don't get spelled out.
    """
    if not s or any(ch.isspace() for ch in s):
        return False
    if "/" in s:               # <-- treat slashy tokens as dates, not codes
        return False
    has_letters = any(ch.isalpha() for ch in s)
    has_digits  = any(ch.isdigit() for ch in s)
    if not has_digits:
        return False
    digit_ratio = sum(ch.isdigit() for ch in s) / len(s)
    return has_letters or digit_ratio >= NUMERIC_HEAVY_RATIO

voice_name = "en-US-JennyNeural"  # more stable than NovaTurboMultilingual

def _build_ssml_for(text: str) -> str:
    clean = " ".join(text.split())

    # 1) Dates like 06/05/2024 → speak normally
    if _is_date_token(clean):
        return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    {clean}
  </voice>
</speak>
""".strip()

    # 2) Single code/number token → spell EVERY symbol with small pauses
    if _is_code_token(clean):
        tokens = []
        for ch in clean:
            if ch.isalpha():
                tokens.append(f'<lang xml:lang="en-US"><say-as interpret-as="characters">{ch}</say-as></lang>')
            elif ch.isdigit():
                tokens.append(f'<lang xml:lang="en-US"><say-as interpret-as="digits">{ch}</say-as></lang>')
            else:
                tokens.append(f'<break time="{PAUSE_MS_EACH}ms"/>')  # punctuation
        inner = f' <break time="{PAUSE_MS_EACH}ms"/> '.join(tokens)
        return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    {inner}
  </voice>
</speak>
""".strip()

    # 3) Everything else → normal speech
    return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice xml:lang="en-US" xml:gender="Female" name="{voice_name}">
    {clean}
  </voice>
</speak>
""".strip()



# ─── 2. speak_with_azure: pull Azure TTS, chunk, and send via your WebSocket ─
TTS_REENABLE_ASR_DELAY = 0.2   # seconds
async def speak_with_azure(text: str, call_control_id: str):
    """
    Generate Azure TTS audio (8 kHz μ-law) and stream it back to Telnyx
    over the same WebSocket that’s feeding you media.
    """
    call_state = active_calls.get(call_control_id)
    ws = getattr(call_state, "websocket", None)
    if not ws:
        logger.error("No WebSocket found for TTS")
        return

    # NEW: serialize TTS per call and flag we're speaking
    if not hasattr(call_state, "tts_lock"):
        call_state.tts_lock = asyncio.Lock()

    async with call_state.tts_lock:
        setattr(call_state, "is_tts_active", True)
        try:
            logger.info(f" Generating TTS: {text!r}")
            ssml = _build_ssml_for(text)

            url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
            headers = {
                "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, content=ssml, headers=headers)
                resp.raise_for_status()
                audio_bytes = resp.content

            # Stream ~100 ms frames (800 bytes @ 8kHz μ-law)
            chunk_size = 800
            for offset in range(0, len(audio_bytes), chunk_size):
                chunk = audio_bytes[offset:offset + chunk_size]
                payload = base64.b64encode(chunk).decode("ascii")
                msg = {"event": "media", "media": {"track": "outbound", "payload": payload}}
                await ws.send_text(json.dumps(msg))
                await asyncio.sleep(0.1)

            # small guard before we re-enable ASR feeding
            await asyncio.sleep(TTS_REENABLE_ASR_DELAY)
            logger.info("TTS streaming complete")
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            setattr(call_state, "is_tts_active", False)
# REGISTER TTS HANDLER
claims_agent.register_tts(speak_with_azure)



async def hangup_call(call_control_id: str):
    """Hangup the call"""
    try:
        await telnyx_client.hangup(call_control_id, TELNYX_BASE_URL, HEADERS)
        logger.info(f"✅ Call hung up: {call_control_id}")
    except Exception as e:
        logger.error(f"❌ Error hanging up call: {str(e)}")


####   Function to auto hangup calls after a delay
# This function will be called in the background to auto hangup calls after a delay


async def _auto_hangup(call_control_id: str, delay_seconds: int = 900):
    """
    Wait `delay_seconds`, and if the call is still active, hang it up.
    """
    await asyncio.sleep(delay_seconds)
    if call_control_id in active_calls:
        logger.info(f"⌛ Auto-hanging up call {call_control_id} after {delay_seconds} seconds")
        await ensure_call_cleanup(call_control_id, reason="auto_hangup", send_hangup=True)


claims_agent.register_hangup(hangup_call)

# Mount the orchestrate router (uses the SAME shared state/funcs from main.py)
app.include_router(
    make_orchestrate_router(
        active_calls,
        initiated_events,
        TELNYX_BASE_URL=TELNYX_BASE_URL,
        HEADERS=HEADERS,
        TEL_FROM=TEL_FROM,
        CALL_CONTROL_APP_ID=CALL_CONTROL_APP_ID,
        WEBHOOK_BASE_URL=WEBHOOK_BASE_URL,
        STREAM_BASE_URL=STREAM_BASE_URL,
        auto_hangup_fn=_auto_hangup,
    )
)

# NEW: mount webhooks router (pass the SAME live state + cleanup fn)
app.include_router(
    make_webhooks_router(
        active_calls,
        initiated_events,
        ensure_call_cleanup=ensure_call_cleanup,
    )
)

# after you define: ensure_call_cleanup, handle_user_speech, etc.

app.include_router(
    make_stream_router(
        active_calls=active_calls,
        DEBOUNCE_SECONDS=DEBOUNCE_SECONDS,
        stt_manager=stt_manager,
        convert_mulaw_to_pcm=convert_mulaw_to_pcm,
        claims_agent=claims_agent,
        ensure_call_cleanup=ensure_call_cleanup,
        handle_user_speech=handle_user_speech,
    )
)



@app.on_event("shutdown")
async def on_shutdown():
    logger.info("🔌 Shutdown event: hanging up all active calls…")
    for call_id in list(active_calls.keys()):
        try:
            await ensure_call_cleanup(call_id, reason="shutdown", send_hangup=True)
        except Exception as e:
            logger.error(f"❌ Cleanup error for {call_id}: {e}")
    stt_manager.cleanup_all()
    logger.info("✅ All calls hung up and STT sessions cleaned up. Goodbye!")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)


#  TO run hit the start_call endpoint

#curl -X POST http://localhost:5000/start_call -H "Content-Type: application/json" -d "{}"
#curl -X POST "http://localhost:5000/orchestrate_call_simple?wait_for_initiated_ms=10000" -H "Content-Type: application/json" -d "{\"agent_id\":\"AG001\",\"app_id\":\"APP123\"}"
