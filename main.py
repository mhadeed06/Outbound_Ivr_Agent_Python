import asyncio
import time
import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from typing import Dict
from dataclasses import dataclass, field
from datetime import datetime
import logging
from src.services.azure.stt_service import stt_manager, convert_mulaw_to_pcm, AzureRealtimeSttService
from pydantic import BaseModel
import re
from functools import partial
#from prompt import PROMPT_TEMPLATE
import src.core.agents.claims_agent as claims_agent
from src.config.insurance_config import config_manager
from src.core.prompts.manager import get_main_prompt_template
from src.models.data_models import CallState, SimpleCallRequest
import src.services.telnyx.client as telnyx_client
from src.api.v1.orchestrate import make_orchestrate_router
from src.api.v1.webhooks import make_webhooks_router
from src.api.v1.stream import make_stream_router
#from services.azure_tts_service import speak_with_azure
from src.services.azure.tts_service import speak_with_azure as _speak_with_azure
from src.services.llm_service import _call_gpt_api, _process_llama_response
from src.services.claims_helpers import is_claim_not_found, is_claim_start
from src.services.call_lifecycle import hangup_call, auto_hangup
from src.services.call_lifecycle import hangup_call as _hangup_call

from src.services.call_cleanup import ensure_call_cleanup as _ensure_call_cleanup
from src.utils.logging_config import setup_logging




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
os.environ["SSL_KEY_PASSWORD"] = "PEHR$3quelM3d27"


TEL_TO = config_manager.get_phone_number()  

DEBOUNCE_SECONDS = config_manager.get_debounce_seconds()
CLAIM_DEBOUNCE_SECONDS = config_manager.get_claim_debounce_seconds()


HEADERS = {
    "Authorization": f"Bearer {TELNYX_API_KEY}",
    "Content-Type": "application/json"
}

app = FastAPI()
setup_logging()


logger = logging.getLogger(__name__)


initiated_events: Dict[str, asyncio.Event] = {}


# Global state management
active_calls: Dict[str, CallState] = {}

bound_hangup = partial(
    _hangup_call,
    telnyx_client=telnyx_client,
    TELNYX_BASE_URL=TELNYX_BASE_URL,
    HEADERS=HEADERS,
)


# Pre-fill Azure credentials and active_calls so other parts can just call speak_with_azure(text, call_id)
speak_with_azure = partial(
    _speak_with_azure,
    active_calls=active_calls,
    AZURE_SPEECH_KEY=AZURE_SPEECH_KEY,
    AZURE_SPEECH_REGION=AZURE_SPEECH_REGION,
)



ensure_call_cleanup = partial(
    _ensure_call_cleanup,
    active_calls=active_calls,
    claims_agent=claims_agent,
    stt_manager=stt_manager,
    hangup_call=bound_hangup,   # ← use the bound version here
)

def append_conversation_step(call_state, transcript: str, gpt_result: str):
    if not call_state:
        return
    transcript = (transcript or "").strip()
    gpt_result = (gpt_result or "").strip()
    if not transcript and not gpt_result:
        return

    call_state.conversation_history.append({
        "transcript": transcript,
        "gpt_result": gpt_result
    })

# ─── 1. handle_user_speech: decorate transcript into a full prompt ────────────

async def handle_user_speech(transcript: str, call_control_id: str):

    #logger.info(f"🎯 handle_user_speech CALLED with transcript length: {len(transcript)}")
    #logger.info(f"📝 Whole transcript: {transcript}")

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
            logger.info("Debounce time changed for claims flow")
            call_state.debounce_seconds = config_manager.get_claim_debounce_seconds()
            call_state.need_debounce_reset = True
            claim_seg_timeout = config_manager.get_claim_segmentation_silence_ms()
            call_state.segmentation_silence_ms = claim_seg_timeout
            if hasattr(call_state, 'azure_stt_session') and call_state.azure_stt_session:
                call_state.azure_stt_session.update_segmentation_timeout(claim_seg_timeout)
                logger.info(f"✅ Segmentation timeout changed to {claim_seg_timeout}ms for claims")

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

                # NEW: Revert segmentation timeout
                normal_seg_timeout = config_manager.get_segmentation_silence_ms()
                call_state.segmentation_silence_ms = normal_seg_timeout
                if hasattr(call_state, 'azure_stt_session') and call_state.azure_stt_session:
                    call_state.azure_stt_session.update_segmentation_timeout(normal_seg_timeout)
                    logger.info(f"✅ Segmentation timeout reverted to {normal_seg_timeout}ms")

            return
 


    prompt_template = get_main_prompt_template()  # Gets correct template for current insurance
     
    # Example: BAYLOR SCOOT & WHITE
    # prompt = prompt_template.format(
    #     transcript=transcript,
    #     tax_id="833613394",
    #     npi= "1285144311",
    #     customer_id= "100099748800",
    #     dob=  "8/3/1970",
    #     member_name= "INDIA WALKER",
    #     dos="4/2/2025"
    # )

    #    Humana
    # prompt = prompt_template.format(
    #     transcript=transcript,
    #     tax_id="833613394",
    #     npi= "1407891245",
    #     customer_id= "h70726498",
    #     dob=  "8/11/1948",
    #     member_name= "JOYCE TURNER",
    #     dos="6/11/2025"
    # )
     

    # CIGNA
    prompt = prompt_template.format(
        transcript=transcript,
        tax_id="833613394",
        npi= "1437285970",
        customer_id= "102775279",
        dob=  "4/14/1990",
        member_name= "JACOB RITTIMANN",
        dos="6/16/2025"
    )

       # OSCAR
    # prompt = prompt_template.format(
    #     transcript=transcript,
    #     tax_id="874546086",
    #     customer_id= "7618978201",
    #     npi= "1497595284",
    #     dos="10/17/2025"
    # )
    

    # health first

    # prompt = prompt_template.format(
    #     transcript=transcript,
    #     claim_number= " 0105172504677",
    #     Member_id= "WY15318S",
    #     dob= "05/02/1962",
    # )


    t0 = time.perf_counter()
    response = await _call_gpt_api(prompt)
    if call_state:
        append_conversation_step(call_state, text, response)
    gpt_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"GPT latency: {gpt_ms:.0f} ms")
    logger.info(f"GPT response: {response!r}")


    await process_llama_response(response, call_control_id)



async def send_dtmf(digits: str, call_control_id: str):
    """Send DTMF tones to the call (digits already sanitized by caller)."""
    try:
        cleaned = "".join(ch for ch in digits if ch.isdigit() or ch in "*#")
        # If you need durations, we can extend telnyx_client to accept them.
        await telnyx_client.send_dtmf(call_control_id, cleaned, TELNYX_BASE_URL, HEADERS)
        logger.info(f"✅ DTMF sent: {cleaned}")
    except Exception as e:
        logger.error(f"❌ Error sending DTMF: {str(e)}")

# keep same signature: process_llama_response(response, call_control_id)
process_llama_response = partial(
    _process_llama_response,
    speak_with_azure=speak_with_azure,       # already partial-bound above
    send_dtmf=send_dtmf,                     # requires send_dtmf to be defined first
    ensure_call_cleanup=ensure_call_cleanup,
    active_calls=active_calls,
)



claims_agent.register_hangup(bound_hangup)  # ← same 1-arg signature
claims_agent.register_active_calls(active_calls)


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
        # Wrap auto_hangup so dependencies are passed automatically
        auto_hangup_fn=lambda call_id, delay_seconds=1500: auto_hangup(
            call_id,
            active_calls,
            ensure_call_cleanup,
            delay_seconds
        ),
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

claims_agent.register_tts(speak_with_azure)
claims_agent.register_dtmf(send_dtmf)


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
    uvicorn.run(app, 
                host="0.0.0.0",
                port=9080,
                reload=False,
                )


#  TO run hit the start_call endpoint

#curl -X POST http://localhost:5000/start_call -H "Content-Type: application/json" -d "{}"
#curl -X POST "http://localhost:5000/orchestrate_call_simple?wait_for_initiated_ms=10000" -H "Content-Type: application/json" -d "{\"agent_id\":\"AG001\",\"app_id\":\"APP123\"}"
