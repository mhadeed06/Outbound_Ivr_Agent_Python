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







# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_BASE_URL = "https://api.telnyx.com/v2"
TEL_TO = os.getenv("TEL_TO")  # Number to call
TEL_FROM = os.getenv("TEL_FROM")  # Your Telnyx number
CALL_CONTROL_APP_ID = os.getenv("CALL_CONTROL_APP_ID")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")  # Your server URL
STREAM_BASE_URL = WEBHOOK_BASE_URL.replace("https://", "wss://")
AZURE_SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")


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
@dataclass
class CallState:
    """State management for active calls"""
    call_control_id: str
    status: str = "initiated"
    start_time: datetime = field(default_factory=datetime.now)
    websocket_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    azure_stt_session: Optional[AzureRealtimeSttService] = None
    conversation_history: list = field(default_factory=list)
    # NEW: store the IDs we receive
    agent_id: Optional[str] = None
    app_id: Optional[str] = None


# Global state management
active_calls: Dict[str, CallState] = {}




# Your GPT prompt template for IVR call bot CIGNA


#GPT prompt template for IVR call bot Baylor Scott and white health plan


PROMPT_TEMPLATE = """
You are an IVR call assistant (outbound) responding on behalf of a healthcare provider's automated phone system.

Your job is to process IVR system prompts during an interactive phone call.  
When the system speaks a message (provided as the IVR: message below), respond with the action we should take — in one of these exact response formats:

*********
Allowed Response Formats:
- say:<phrase> → speak a word or phrase aloud
- value:<value> → provide a Member ID, Date of Birth, or NPI
- dtmf:<digit> → press a keypad digit


- confirm:<yes/no> → confirm a heard value
- endcall → terminate the call
- fallback → if the IVR message is unclear or unsupported, IF YOU THINK NO ASNWER IS NEEDED, just reply with "fallback" and we will continue listening for the next IVR prompt.
- ONLY RESPOSNE WITH A VALUE OR SAY IF HE IS ASKING FOR ANY INFORMATION OR CONFINMATION, OTHERWISE JUST REPLY WITH FALLBACK
***********
**Rules**: Only use one of the above formats—no extra text.

### Call Flow Outline

Call Information
***
Plan Name: SCOTT & WHITE HEALTH PLANS
NPI: 1407891245
Member Name: HELEN TREDWAY
Member Id: B S W one zero zero zero three six nine zero zero
DOB: 06/18/1964
DOS: 08/08/2024

what this does
*Call Flow Instructions*

**** Important******
ALWAYS RESPOND FROM THE RESPONSE COLUMN, NOT WHAT THE IVR ASKS OR IN THE TRANSCRIPT, MATCH THE INTENT OF THE TRANSCRIPT WITH THE ONE OF THE BELOW STEPS AND RESPOND ACCORDINGLY 

Step 1: Caller Type
    IVR Prompt: "you can say I'm a provider, or I'm neither of those"
    Response: say:provider or dtmf:2


Step 2: Main Menu
    IVR asks: "Enrollment status, claim status, benefit details, claims address, authorizations, health services, or network status"
    Response: say:claim status or dtmf:2


Step 3: NPI
    IVR asks: "Please say or enter your NPI"
    Response: value:1407891245


Step 4: Member ID
    IVR asks: "Please say or enter the member ID or social security number"
    Response: value: "B S W one zero zero zero three six nine zero zero"


Step 5: Date of Birth
    IVR asks: "What's the date of birth"
    Response: value:06/18/1964

Step 6: DOB Confirmation
    IVR asks: "If the IVR confirms the DOB as 06/18/1964"
    Response: confirm:yes OR dmtf:1
    Otherwise say: no or dmtf:2
    
Step 6: Member ID or DOB Confirmation
    IVR asks: "Did you say BSW100036900"
    Response: confirm:yes


Step 6: Date of Service
    IVR asks: "What's the date of service you'd like to check"
    Response: value:08/08/2024

Step 7: Found Claim
    IVR provides: If the IVR says I found your claims, "I found two claims on that date"  OR  "This claim was received on....."
    End the call with endcall (Donot say endcall, just end the call by returning endcall)

Step 8: Didnot found claims
    IVR Provides: I didnot found your claims 
    End the call with endcall (Donot say endcall, just end the call by returning endcall)

***Response Guidelines***

Voice Responses: Always speak clearly and wait for IVR prompts to complete
Keypad Entries: Enter numbers precisely as shown above
Confirmations: Always confirm "Yes" when information matches
***********If you ever receiv a transcript, which doesnot match with the above steps, just send fallback**********
**If you get a incomplete resposne which you think is not enough to continue, just reply with "fallback" and we will continue listening for the next IVR prompt.**

Error Handling

If asked to repeat information, provide the same data exactly as listed above
If the system doesn't recognize voice input, try speaking more clearly or 
If member name doesn't match, verify the Member ID was entered correctly
### 🚨 Important Rules

- Respond with **only one** exact format — no extra text.
- Supply value: when the system expects numeric or alphanumeric input.
- **Use say: when the system expects a spoken response.**
- Observe confirmation questions and reply yes/no.
- If unsure, use fallback.
- We will end the call in 2 scenarios: 1- if we get what we want or 2- if we are not able to get what we want, so end the call with endcall. like the system says something like there is no data for this claim, don't end call for any other reason 
- Please end the call when the IVR says something like "Looks like you're having trouble. Let's connect you to the agent."
   then hung up the call with endcall.

---

Now, read the following IVR prompt and reply accordingly using the correct format only:
Process this prompt and don't press any key until you find an explicit instruction to respond.

IVR Message: "{transcript}"
""".strip()


class SimpleCallRequest(BaseModel):
    agent_id: str
    app_id: str
    # 0 = don’t wait; default wait 2s for webhook to flip to "initiated"
    wait_for_initiated_ms: int | None = 2000



@app.post("/orchestrate_call_simple")
async def orchestrate_call_simple(request: Request, wait_for_initiated_ms: int = 10000):
    """
    Receive agent_id + app_id, start the Telnyx call (same flow as /start_call),
    optionally wait briefly for 'call.initiated', then return status.
    """
    try:
        # ── parse body ───────────────────────────────────────────────────────
        try:
            incoming = await request.json()
        except Exception:
            logger.exception("❌ Invalid JSON body")
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        agent_id = incoming.get("agent_id")
        app_id   = incoming.get("app_id")

        if not agent_id or not app_id:
            return JSONResponse(
                {"error": "agent_id and app_id are required"},
                status_code=400
            )

        # ── same Telnyx call payload as /start_call ─────────────────────────
        call_payload = {
            "to": TEL_TO,
            "from": TEL_FROM,
            "connection_id": CALL_CONTROL_APP_ID,
            "webhook_url": f"{WEBHOOK_BASE_URL}/webhooks/calls",
            "webhook_url_method": "POST",
            "stream_url": f"{STREAM_BASE_URL}/stream",
            "stream_track": "both_tracks",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
            "send_silence_when_idle": True
        }

        # ── start the call with Telnyx ───────────────────────────────────────
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELNYX_BASE_URL}/calls",
                json=call_payload,
                headers=HEADERS
            )

        # parse Telnyx response safely
        try:
            body = response.json()
        except Exception:
            logger.exception("❌ Failed to parse JSON from Telnyx")
            return JSONResponse(
                {"error": f"Invalid JSON from Telnyx: {response.text}"},
                status_code=500
            )

        if not (200 <= response.status_code < 300):
            logger.error(f"❌ Telnyx error {response.status_code}: {body!r}")
            return JSONResponse(
                {"error": f"Telnyx returned {response.status_code}: {body!r}"},
                status_code=500
            )

        # ── extract data ─────────────────────────────────────────────────────
        data = body.get("data", {})
        call_control_id = data.get("call_control_id")
        call_session_id = data.get("call_session_id")
        is_alive        = data.get("is_alive")

        if not call_control_id:
            logger.error(f"❌ Missing call_control_id in response: {body!r}")
            return JSONResponse(
                {"error": f"Missing call_control_id in Telnyx response: {body!r}"},
                status_code=500
            )

        # ── store call state + your two IDs ──────────────────────────────────
        active_calls[call_control_id] = CallState(
            call_control_id=call_control_id,
            agent_id=agent_id,
            app_id=app_id
        )
        asyncio.create_task(_auto_hangup(call_control_id, delay_seconds=600))

        # ── race-proof wait for 'call.initiated' ─────────────────────────────
        status = "queued"
        if (wait_for_initiated_ms or 0) > 0:
            # create/reuse the event BEFORE checking status to avoid race
            ev = initiated_events.setdefault(call_control_id, asyncio.Event())

            # if webhook already flipped status, set event now
            cs = active_calls.get(call_control_id)
            if cs and getattr(cs, "status", None) == "initiated":
                ev.set()

            try:
                await asyncio.wait_for(
                    ev.wait(),
                    timeout=(wait_for_initiated_ms / 1000.0)
                )
                status = "initiated"
            except asyncio.TimeoutError:
                status = "queued"  # fallback after timeout
            finally:
                initiated_events.pop(call_control_id, None)

        logger.info(f"✅ Call queued: {call_control_id} (is_alive={is_alive}) status={status}")

        # ── response ─────────────────────────────────────────────────────────
        return JSONResponse({
            "success": True,
            "status": status,
            "agent_id": agent_id,
            "app_id": app_id,
            "call_control_id": call_control_id,
            "call_session_id": call_session_id,
            "is_alive": is_alive
        })

    except Exception:
        logger.exception("❌ Unexpected error orchestrating call")
        return JSONResponse(
            {"error": "Internal error starting call; check server logs"},
            status_code=500
        )




@app.post("/start_call")
async def start_outbound_call():
    """Start an outbound call with Azure STT streaming"""
    try:
        call_payload = {
            "to": TEL_TO,
            "from": TEL_FROM,
            "connection_id": CALL_CONTROL_APP_ID,
            "webhook_url": f"{WEBHOOK_BASE_URL}/webhooks/calls",
            "webhook_url_method": "POST",
            "stream_url": f"{STREAM_BASE_URL}/stream",
            "stream_track": "both_tracks",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
            "send_silence_when_idle": True
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELNYX_BASE_URL}/calls",
                json=call_payload,
                headers=HEADERS
            )

        # Try to parse JSON, but handle non-JSON bodies
        try:
            body = response.json()
        except Exception as parse_err:
            logger.exception("❌ Failed to parse JSON from Telnyx")
            return JSONResponse(
                {"error": f"Invalid JSON from Telnyx: {response.text}"},
                status_code=500
            )

        # If Telnyx didn’t return 2xx, surface their error
        if not (200 <= response.status_code < 300):
            logger.error(f"❌ Telnyx error {response.status_code}: {body!r}")
            return JSONResponse(
                {"error": f"Telnyx returned {response.status_code}: {body!r}"},
                status_code=500
            )

        # Success path: extract data
        data = body.get("data", {})
        call_control_id = data.get("call_control_id")
        call_session_id = data.get("call_session_id")
        is_alive        = data.get("is_alive")

        if not call_control_id:
            logger.error(f"❌ Missing call_control_id in response: {body!r}")
            return JSONResponse(
                {"error": f"Missing call_control_id in Telnyx response: {body!r}"},
                status_code=500
            )

        # Store call state
        active_calls[call_control_id] = CallState(call_control_id=call_control_id)
                # 🔔 schedule a 10-minute auto-hangup
        asyncio.create_task(_auto_hangup(call_control_id, delay_seconds=600))

        logger.info(f"✅ Call queued: {call_control_id} (is_alive={is_alive})")

        return JSONResponse({
            "success": True,
            "call_control_id": call_control_id,
            "call_session_id": call_session_id,
            "is_alive": is_alive
        })

    except Exception:
        # Log full stack trace
        logger.exception("❌ Unexpected error starting outbound call")
        return JSONResponse(
            {"error": "Internal error starting call; check server logs"},
            status_code=500
        )



@app.post("/webhooks/calls")
async def handle_call_webhooks(request: Request):
    """Handle Telnyx call control webhooks"""
    try:
        body = await request.json()
        data = body.get("data", {})
        event_type = data.get("event_type")
        payload = data.get("payload", {})
        call_control_id = payload.get("call_control_id")
        
        logger.info(f"📞 Call Event: {event_type}")
        
        if call_control_id not in active_calls:
            logger.warning(f"⚠️ Unknown call ID: {call_control_id}")
            return JSONResponse({"status": "ok"})
            
        call_state = active_calls[call_control_id]
        
        if event_type == "call.initiated":
            logger.info("📞 Call initiated")
            call_state.status = "initiated"
            if call_control_id in initiated_events:
                initiated_events[call_control_id].set()

            
        elif event_type == "call.ringing":
            logger.info("🔔 Call ringing")
            call_state.status = "ringing"
            
        elif event_type == "call.answered":
            logger.info("✅ Call answered - Media streaming should start automatically")
            call_state.status = "answered"
            
        elif event_type == "call.hangup":
            logger.info("🔚 Call ended")
            # Cleanup STT session if exists
            if call_state.azure_stt_session:
                stt_manager.remove_session(call_state.websocket_id)
            # Remove from active calls
            del active_calls[call_control_id]
            
        elif event_type == "call.streaming.started":
            logger.info("🎵 Streaming started successfully")
            
        elif event_type == "call.streaming.stopped":
            logger.info("🎵 Streaming stopped")
            
        else:
            logger.info(f"📌 Unhandled event: {event_type}")
            
        return JSONResponse({"status": "ok"})
        
    except Exception as e:
        logger.error(f"❌ Error handling webhook: {str(e)}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ——— STREAMING ENDPOINT ————————————————————————————————————————


@app.websocket("/stream")
async def media_stream_endpoint(websocket: WebSocket):
    logger.info("🔗 New WebSocket connection")
    await websocket.accept()

    call_control_id = None
    call_state      = None
    websocket_id    = str(uuid.uuid4())

    # STT callbacks
    async def on_partial(text: str):
        logger.debug(f"[{websocket_id}] Interim: {text!r}")

    async def on_final(text: str):
        text = text.strip()
        if not text:
            return

        now = time.perf_counter()
        if call_state and hasattr(call_state, "last_media_ts"):
            ms = (now - call_state.last_media_ts) * 1000
            logger.info(f"[{websocket_id}] STT latency: {ms:.0f} ms")

        ##logger.info(f"[{websocket_id}] Final transcript: {text!r}")
        if call_state:
            call_state.conversation_history.append({"role": "user", "content": text})
        await handle_user_speech(text, call_control_id)

    async def on_error(err: str):
        logger.error(f"[{websocket_id}] STT error: {err}")

    try:
        while True:
            frame = await websocket.receive_text()
            msg   = json.loads(frame)
            ev    = msg.get("event")

            if ev == "start":
                call_control_id = msg["start"]["call_control_id"]
                logger.info(f"[{websocket_id}] Call started: {call_control_id}")

                call_state = active_calls.get(call_control_id)
                if not call_state:
                    logger.warning(f"[{websocket_id}] Unknown call ID")
                    continue

                # ======= NEW LINE ===========
                # so speak_with_azure() can send outbound media back
                call_state.websocket = websocket

                session = stt_manager.create_session(websocket_id)
                call_state.azure_stt_session = session

                session.initialize(
                    on_partial_result=on_partial,
                    on_final_result=on_final,
                    on_error=on_error,
                )
                session.start_continuous_recognition()
                session.start_async_event_handler(asyncio.get_running_loop())

            elif ev == "media":
                media = msg["media"]
                if media.get("track") == "inbound" and call_state:
                    pcm = convert_mulaw_to_pcm(base64.b64decode(media["payload"]))
                    call_state.last_media_ts = time.perf_counter()
                    call_state.azure_stt_session.feed_audio(pcm)

            elif ev == "stop":
                logger.info(f"[{websocket_id}] Stream stopped")
                break

    except WebSocketDisconnect:
        logger.info(f"[{websocket_id}] WebSocket disconnected")
    except Exception as e:
        logger.error(f"[{websocket_id}] Stream error: {e}")
    finally:
        if call_state and call_state.azure_stt_session:
            stt_manager.remove_session(websocket_id)
            logger.info(f"[{websocket_id}] STT session cleaned up")




# ─── 1. handle_user_speech: decorate transcript into a full prompt ────────────

async def handle_user_speech(transcript: str, call_control_id: str):
    #logger.info(f"Received transcript: {transcript!r}")
    text = transcript.strip()

    if not transcript or len(transcript) < 3:
        logger.warning(f"Transcript too short, skipping")
        return
    


    # 1) build full prompt
    prompt = PROMPT_TEMPLATE.format(transcript=transcript)
    ##logger.info(f"[{call_control_id}] Full Llama prompt: {prompt!r}")

    # 2) call Llama
    t0 = time.perf_counter()
    response = await call_llama_api(prompt)
    llama_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"Llama latency: {llama_ms:.0f} ms")
    logger.info(f"Llama response: {response!r}")

    # 3) dispatch
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
    Process Llama response and take appropriate action
    """
    if not response:
        logger.info("❗ Llama returned empty response")
        return
    
    resp = response.strip()
    
    if resp.startswith("dtmf:"):
        digits = resp.split("dtmf:")[1].strip()
        logger.info(f"→ Sending DTMF: {digits}")
        await send_dtmf(digits, call_control_id)
        
    elif resp.startswith(("say:", "value:", "confirm:")):
        phrase = ":".join(resp.split(":")[1:]).strip()
        logger.info(f"→ Speaking phrase: '{phrase}'")
        await speak_with_azure(phrase, call_control_id)
        
    elif "no response" in resp.lower() or resp == "noresponse":
        logger.info("→ Llama returned no response: continuing to listen")
        return
        
    elif resp == "endcall":
        logger.info("→ Hanging up per Llama instruction")
        await hangup_call(call_control_id)
        
    else:
        logger.info(f"❗ Llama returned unknown action: '{resp}' — fallback listening")
        return
    



async def send_dtmf(digits: str, call_control_id: str):
    """Send DTMF tones to the call"""
    try:
        url = f"{TELNYX_BASE_URL}/calls/{call_control_id}/actions/send_dtmf"
        payload = {
            "digits": digits,
            "duration_millis": 250,
            "inter_digit_duration_millis": 250
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=HEADERS)
            logger.info(f"✅ DTMF sent: {digits}")
            
    except Exception as e:
        logger.error(f"❌ Error sending DTMF: {str(e)}")

# ─── 2. speak_with_azure: pull Azure TTS, chunk, and send via your WebSocket ─

async def speak_with_azure(text: str, call_control_id: str):
    """
    Generate Azure TTS audio (8 kHz μ-law) and stream it back to Telnyx
    over the same WebSocket that’s feeding you media.
    """
    call_state = active_calls.get(call_control_id)
    ws = getattr(call_state, "websocket", None)
    if not ws:
        logger.error(f"No WebSocket found for TTS")
        return

    logger.info(f" Generating TTS: {text!r}")
    # build SSML
    ssml = f"""
    <speak version="1.0" xml:lang="en-US">
      <voice xml:lang="en-US" xml:gender="Female"
             name="en-US-NovaTurboMultilingualNeural">
        {text}
      </voice>
    </speak>
    """.strip()

    url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, content=ssml, headers=headers)
            resp.raise_for_status()
            audio_bytes = resp.content

        #logger.info(f" TTS audio size: {len(audio_bytes)} bytes")

        # send in 100 ms chunks (8000 bytes/sec → 800 bytes per 100 ms)
        chunk_size = 800
        for offset in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[offset:offset + chunk_size]
            payload = base64.b64encode(chunk).decode("ascii")
            msg = {
                "event": "media",
                "media": {"track": "outbound", "payload": payload}
            }
            await ws.send_text(json.dumps(msg))
            await asyncio.sleep(0.1)

        logger.info(f"TTS streaming complete")

    except Exception as e:
        logger.error(f"TTS error: {e}")


async def hangup_call(call_control_id: str):
    """Hangup the call"""
    try:
        url = f"{TELNYX_BASE_URL}/calls/{call_control_id}/actions/hangup"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=HEADERS)
            logger.info(f"✅ Call hung up: {call_control_id}")
            
    except Exception as e:
        logger.error(f"❌ Error hanging up call: {str(e)}")


####   Function to auto hangup calls after a delay
# This function will be called in the background to auto hangup calls after a delay

async def _auto_hangup(call_control_id: str, delay_seconds: int = 600):
    """
    Wait `delay_seconds`, and if the call is still active, hang it up.
    """
    await asyncio.sleep(delay_seconds)
    if call_control_id in active_calls:
        logger.info(f"⌛ Auto-hanging up call {call_control_id} after {delay_seconds} seconds")
        await hangup_call(call_control_id)



@app.on_event("shutdown")
async def on_shutdown():
    logger.info("🔌 Shutdown event: hanging up all active calls…")
    # Hang up any still-active calls
    for call_id in list(active_calls.keys()):
        try:
            await hangup_call(call_id)
        except Exception as e:
            logger.error(f"❌ Error hanging up call {call_id}: {e}")
    # Clean up all Azure STT sessions
    stt_manager.cleanup_all()
    logger.info("✅ All calls hung up and STT sessions cleaned up. Goodbye!")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)




#  TO run hit the start_call endpoint

#curl -X POST http://localhost:5000/start_call -H "Content-Type: application/json" -d "{}"
#curl -X POST "http://localhost:5000/orchestrate_call_simple?wait_for_initiated_ms=10000" -H "Content-Type: application/json" -d "{\"agent_id\":\"AG001\",\"app_id\":\"APP123\"}"
