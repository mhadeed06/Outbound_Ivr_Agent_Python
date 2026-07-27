import asyncio
import time
import os
from fastapi import FastAPI, HTTPException, Request
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
import src.core.claims.claims_agent as claims_agent
from src.config.insurance_config import config_manager
from src.core.prompts.manager import get_main_prompt_template, get_denial_prompt_template
from src.core.denials.transition import is_transfer_signal
from src.core.denials.detection import confirm_denial_via_gpt
from src.core.denials.context import build_denial_format_kwargs, render_denial_context
from src.core.denials.reason_classifier import (
    match_reason_rules,
    has_reason_cue,
    classify_reason_gpt,
)
from src.models.data_models import CallState, SimpleCallRequest
import src.services.telnyx.client as telnyx_client
from src.api.v1.orchestrate import make_orchestrate_router
from src.api.v1.webhooks import make_webhooks_router
from src.api.v1.stream import make_stream_router
#from services.azure_tts_service import speak_with_azure
from src.services.azure.tts_service import speak_with_azure as _speak_with_azure
from src.services.llm.llm_service import _call_gpt_api, _process_llama_response
from src.core.claims.claims_helpers import is_claim_not_found, is_claim_start
from src.services.call_lifecycle import hangup_call, auto_hangup
from src.services.call_lifecycle import hangup_call as _hangup_call

from src.services.call_cleanup import ensure_call_cleanup as _ensure_call_cleanup
from src.utils.logging_config import setup_logging, set_call_id
from src.utils.transcript import append_ivr, append_agent_dtmf




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


# NOTE: insurance-specific values (TEL_TO, debounce timings, etc.) are read
# per call from the active insurance config (ContextVar). Do not materialize
# them at module load — no insurer is active until a request arrives.


HEADERS = {
    "Authorization": f"Bearer {TELNYX_API_KEY}",
    "Content-Type": "application/json"
}

app = FastAPI()
setup_logging()

# Ship logs + traces to Azure Application Insights when the connection string
# is set (Azure App Service injects it once App Insights is enabled in the
# portal). On local dev the env var is absent and this is a no-op.
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(logger_name=None)  # captures the root logger → all our logs


logger = logging.getLogger(__name__)


# Convert every HTTPException (including JWT auth failures raised by
# src/auth/jwt_auth.py) to the frontend-standard {succeeded, message} shape.
# Keeps response format uniform across success and error paths.
@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"succeeded": False, "message": detail},
    )


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

    call_state.add_history({
        "transcript": transcript,
        "gpt_result": gpt_result
    })


_CLAIM_HISTORY_MAX_TURNS = 3

def _format_claim_history(call_state) -> str:
    """Last few IVR↔bot turns for the claim-status prompt, so GPT has context
    for back-references like "can you repeat that?" (otherwise it has no idea
    what it just said). Kept SHORT — the IVR flow is fast and menu-driven, so a
    long history would bloat the prompt and risk re-driving old phases. Only the
    {transcript, gpt_result} entries are shown (assistant-content entries just
    duplicate the gpt_result)."""
    if not call_state:
        return "(no prior turns yet)"
    turns = []
    for e in (call_state.conversation_history or []):
        t = (e.get("transcript") or "").strip()
        g = (e.get("gpt_result") or "").strip()
        if t or g:
            turns.append((t, g))
    turns = turns[-_CLAIM_HISTORY_MAX_TURNS:]
    if not turns:
        return "(no prior turns yet)"
    lines = []
    for t, g in turns:
        if t:
            lines.append(f'IVR said: "{t}"')
        if g:
            lines.append(f'You responded: {g}')
    return "\n".join(lines)


def _is_gpt_claim_mode_signal(response: str) -> bool:
    """Detect the 'claim_mode' safety-net signal from GPT — used as a fallback
    when is_claim_start() missed the IVR phrasing. Strict-equality on the
    compact form so no normal say/value/confirm response can accidentally
    match (no substring, no prefix)."""
    if not response:
        return False
    compact = response.strip().lower().replace(" ", "").replace("_", "").rstrip(".,!?:;'\"")
    return compact == "claimmode"


async def _enter_claim_mode_and_forward(call_state, call_control_id: str, first_chunk: str):
    """Flip claim_mode, bump debounce + STT segmentation for claims flow,
    start the claims session, and forward the first chunk. Shared by the
    real-time is_claim_start path and the post-GPT claim_mode fallback so
    both behave identically."""
    if call_state.claim_mode:
        return
    call_state.claim_mode = True
    logger.info("Debounce time changed for claims flow")
    call_state.debounce_seconds = config_manager.get_claim_debounce_seconds()
    call_state.need_debounce_reset = True
    claim_seg_timeout = config_manager.get_claim_segmentation_silence_ms()
    call_state.segmentation_silence_ms = claim_seg_timeout
    if hasattr(call_state, 'azure_stt_session') and call_state.azure_stt_session:
        call_state.azure_stt_session.update_segmentation_timeout(claim_seg_timeout)
        logger.info(f"✅ Segmentation timeout changed to {claim_seg_timeout}ms for claims")

    await claims_agent.start_session(call_control_id)
    await claims_agent.handle_final(call_control_id, first_chunk)


# ─── Denial follow-up (in-call pivot) ─────────────────────────────────────────

# How many recent turns of the denial conversation to inject into the rep
# prompt. A real denial call has ~15-25 exchanges; 12 turns covers the active
# context without blowing up the token budget.
_DENIAL_HISTORY_MAX_TURNS = 12


def _is_gpt_rep_mode_signal(response: str) -> bool:
    """Detect the 'rep_mode' safety-net signal from the denial-IVR prompt —
    a live human picked up without any transfer phrase being heard. Strict
    equality on the compact form (mirror of _is_gpt_claim_mode_signal)."""
    if not response:
        return False
    compact = response.strip().lower().replace(" ", "").replace("_", "").rstrip(".,!?:;'\"")
    return compact == "repmode"


# Hold-silence watchdog knobs. If the rep line is quiet this long during the
# rep phase, nudge with a "hello". Capped so a truly dead line isn't nagged
# forever — auto_hangup ends it.
_HOLD_SILENCE_S = float(os.getenv("DENIAL_HOLD_SILENCE_S", "180"))
_HOLD_CHECK_INTERVAL_S = 20.0
_MAX_HOLD_NUDGES = int(os.getenv("DENIAL_MAX_HOLD_NUDGES", "3"))


async def _hold_watchdog(call_control_id: str):
    """While talking to a live rep, if the line goes silent for a long stretch
    (rep put us on hold and wandered off), proactively check we're still
    connected — like a person would — instead of sitting mute forever."""
    try:
        while True:
            await asyncio.sleep(_HOLD_CHECK_INTERVAL_S)
            cs = active_calls.get(call_control_id)
            if not cs or getattr(cs, "phase", None) != "denial_rep":
                return  # call ended or left the rep phase
            if getattr(cs, "is_tts_active", False):
                continue  # bot is currently speaking
            last = getattr(cs, "last_activity_ts", None)
            if last is None:
                cs.last_activity_ts = time.time()
                continue
            if (time.time() - last) < _HOLD_SILENCE_S:
                continue
            if getattr(cs, "hold_nudge_count", 0) >= _MAX_HOLD_NUDGES:
                continue  # stop nagging a dead line; auto_hangup will end it
            cs.hold_nudge_count = getattr(cs, "hold_nudge_count", 0) + 1
            cs.last_activity_ts = time.time()  # reset so we wait again before the next nudge
            logger.info(
                f"🔔 Hold-silence nudge #{cs.hold_nudge_count} "
                f"(quiet ≥ {_HOLD_SILENCE_S:.0f}s)"
            )
            try:
                await speak_with_azure("Hello, are you still there?", call_control_id)
            except Exception as e:
                logger.warning(f"Hold nudge TTS failed: {e}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Hold watchdog error: {e}")


def _mark_rep_activity(call_state):
    """Rep spoke (or we're entering rep phase) — reset the hold-silence clock
    and the nudge count so a responsive rep is never nudged."""
    call_state.last_activity_ts = time.time()
    call_state.hold_nudge_count = 0


def _enter_denial_rep_phase(call_state):
    """Flip to the live-representative phase: looser speech timings (humans
    pause more than IVR menus) + rep prompt from the next turn on."""
    call_state.phase = "denial_rep"
    rep_debounce = config_manager.get_denial_rep_debounce_seconds()
    call_state.debounce_seconds = rep_debounce
    call_state.need_debounce_reset = True
    rep_seg = config_manager.get_denial_rep_segmentation_silence_ms()
    call_state.segmentation_silence_ms = rep_seg
    if getattr(call_state, "azure_stt_session", None):
        call_state.azure_stt_session.update_segmentation_timeout(rep_seg)

    # Start the hold-silence watchdog for this call (once).
    _mark_rep_activity(call_state)
    prior = getattr(call_state, "hold_watchdog_task", None)
    if prior is None or prior.done():
        call_state.hold_watchdog_task = asyncio.create_task(
            _hold_watchdog(call_state.call_control_id)
        )

    logger.info(
        f"🧑‍💼 Denial REP phase entered (debounce={rep_debounce}s, segmentation={rep_seg}ms)"
    )


# GPT reason-classification fallback fires at most this many times per call
# GPT is the PRIMARY reason classifier (keyword rules are only a free fast-path
# — see _update_denial_reason). It runs in the background off the speech loop,
# so a few attempts are cheap; capped so it can't run away while unclassified.
_MAX_REASON_GPT_ATTEMPTS = 5
# Rep utterances shorter than this (and without an obvious reason cue) are
# treated as too thin to classify from — we wait for a substantive explanation.
_MIN_SUBSTANTIVE_LEN = 30


def _schedule_reason_classification(call_state, transcript_tail: str):
    """Fire the GPT reason-classification as a BACKGROUND task — never in the
    speech loop. The task writes denial_reason_key/verbatim onto CallState;
    the next turn's re-rendered prompt picks it up. Cancelled in cleanup
    step 0 via CallState.denial_reason_task."""
    attempts = getattr(call_state, "denial_gpt_attempts", 0)
    if attempts >= _MAX_REASON_GPT_ATTEMPTS:
        return
    prior = getattr(call_state, "denial_reason_task", None)
    if prior is not None and not prior.done():
        return  # one in flight at a time
    call_state.denial_gpt_attempts = attempts + 1

    async def _run():
        try:
            key, verbatim = await classify_reason_gpt(transcript_tail)
            if not key:
                return
            current = getattr(call_state, "denial_reason_key", None)
            provisional = getattr(call_state, "denial_reason_provisional", False)
            # GPT is AUTHORITATIVE: set the reason when it's unknown, OR override
            # a provisional keyword guess. A GPT-confirmed reason (non-provisional)
            # is left alone so we don't thrash turn to turn.
            if current is None or provisional:
                if current is not None and current != key:
                    logger.info(f"🧭 GPT overrode provisional keyword guess: {current} → {key}")
                else:
                    logger.info(f"🧭 Denial reason set by GPT: {key}")
                call_state.denial_reason_key = key
                if verbatim:
                    call_state.denial_reason_verbatim = verbatim
                call_state.denial_reason_provisional = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Reason-classification task failed (ignored): {e}")

    call_state.denial_reason_task = asyncio.create_task(_run())


def _update_denial_reason(call_state, payer_text: str):
    """Per-utterance reason maintenance — GPT is the AUTHORITATIVE classifier.

    Reps phrase the SAME denial many different ways ("docs don't meet criteria"
    / "coded incorrectly" / "we need records to support the code"), so keyword
    rules alone misfire — a stray generic word ("billing error") can hijack the
    real reason. So GPT judges the reason from the FULL conversation, and:

    - GPT runs on every SUBSTANTIVE rep utterance while the reason is unknown or
      only a keyword GUESS (provisional). GPT decides one-of-13 / out-of-scope /
      unknown and OVERRIDES a provisional guess.
    - Keyword rules only supply an INSTANT PROVISIONAL guess when nothing is set
      yet — so the very next turn already has a checklist while GPT thinks in the
      background. Keywords NEVER overwrite a reason once set; GPT owns corrections.
    """
    current = getattr(call_state, "denial_reason_key", None)
    provisional = getattr(call_state, "denial_reason_provisional", False)
    substantive = len(payer_text.strip()) >= _MIN_SUBSTANTIVE_LEN or has_reason_cue(payer_text)

    # GPT primary: (re)classify while the reason is unknown or an unconfirmed guess.
    if call_state.phase == "denial_rep" and substantive and (current is None or provisional):
        tail = _format_denial_history(call_state)
        _schedule_reason_classification(call_state, f"{tail}\nRep: {payer_text}")

    # Instant provisional guess ONLY when nothing is set yet (gives an immediate
    # checklist before GPT returns). Never overwrites; GPT will confirm/correct.
    if current is None:
        hit = match_reason_rules(payer_text)
        if hit is not None:
            call_state.denial_reason_key = hit.key
            call_state.denial_reason_verbatim = payer_text.strip()[:300]
            call_state.denial_reason_provisional = True
            logger.info(f"🧭 Provisional reason from keywords (GPT will confirm): {hit.key}")


def _format_denial_history(call_state) -> str:
    """Render the denial-phase conversation history for the rep prompt.

    Only entries appended AFTER the pivot (denial_history_start) are shown —
    the earlier claim-status/claims-controller turns (DTMF menus, one-word
    intents) would be noise to the rep conversation. Bot lines for fallback/
    endcall are skipped (no audio was produced for them)."""
    start = getattr(call_state, "denial_history_start", 0)
    entries = (call_state.conversation_history or [])[start:][-_DENIAL_HISTORY_MAX_TURNS:]
    lines = []
    for e in entries:
        heard = (e.get("transcript") or "").strip()
        replied = (e.get("gpt_result") or "").strip()
        if heard:
            lines.append(f"Rep: {heard}")
        if replied:
            low = replied.lower()
            compact = low.replace(" ", "")
            if low.startswith("fallback") or compact in ("endcall", "end", "hangup", "repmode"):
                continue
            if low.startswith("say:"):
                replied = replied[4:].strip()
            lines.append(f"You: {replied}")
    if not lines:
        return "(no prior conversation yet — this is the first turn)"
    return "\n".join(lines)


async def _pivot_to_denial_flow(call_id: str, claims_text: str) -> bool:
    """Denial pivot decision + execution. Called from the claims controller's
    STOP path (inside the claims lock) right before the normal end-of-claims
    hangup. Returns True ONLY when the call is pivoting into the denial
    follow-up flow — the caller then ends the claims session WITHOUT hangup.

    Gate (all must hold): request opted in AND payer supports the flow AND a
    denial cue was heard live AND one GPT check confirms the final claim is
    denied. Any failure anywhere → False → the call hangs up exactly as today.
    """
    call_state = active_calls.get(call_id)
    if not call_state:
        return False
    if call_state.phase != "claim_status" or getattr(call_state, "denial_pivoted", False):
        return False
    if not getattr(call_state, "denial_follow_up", False):
        return False
    try:
        if not config_manager.get_supports_denial_inquiry():
            return False
    except RuntimeError:
        return False
    if not getattr(call_state, "denial_candidate", False):
        return False

    # Deterministic-first: a STRONG denial phrase in the readout ("was denied",
    # "line item was denied", "denied because") is certain — pivot without GPT
    # so a GPT hallucination can never veto a real denial. Only fall back to
    # the GPT confirm for WEAK/ambiguous signals ("denial" in passing,
    # conditional phrasings, "not covered").
    from src.core.denials.detection import denial_signal
    signal = denial_signal(claims_text)
    if signal == "strong":
        logger.info("🩺 Denial pivot: STRONG denial signal in readout — pivoting (no GPT needed)")
    elif signal == "weak":
        if not await confirm_denial_via_gpt(claims_text):
            logger.info("🩺 Denial pivot skipped — weak signal, GPT did not confirm denial")
            return False
        logger.info("🩺 Denial pivot: weak signal confirmed by GPT")
    else:
        logger.info("🩺 Denial pivot skipped — no denial signal in final readout")
        return False

    # ── PIVOT ────────────────────────────────────────────────────────────
    call_state.denial_pivoted = True
    call_state.claim_mode = False
    call_state.phase = "denial_ivr"
    call_state.denial_history_start = len(call_state.conversation_history)

    # Revert speech timings from claim-mode values back to the IVR baseline.
    call_state.debounce_seconds = config_manager.get_debounce_seconds()
    call_state.need_debounce_reset = True
    normal_seg = config_manager.get_segmentation_silence_ms()
    call_state.segmentation_silence_ms = normal_seg
    if getattr(call_state, "azure_stt_session", None):
        call_state.azure_stt_session.update_segmentation_timeout(normal_seg)

    # Re-arm the auto-hangup watchdog with the denial budget — rep hold
    # queues outlive the original claim-status timer.
    try:
        # Create the NEW timer first, then cancel the old one — so if the config
        # lookup or create_task throws, the call still has its original watchdog
        # (rather than being left with NO auto-hangup, which could strand the
        # CallState in active_calls forever).
        denial_secs = config_manager.get_denial_auto_hangup_seconds()
        new_task = asyncio.create_task(
            auto_hangup(call_id, active_calls, ensure_call_cleanup, denial_secs)
        )
        old_task = getattr(call_state, "auto_hangup_task", None)
        call_state.auto_hangup_task = new_task
        if old_task is not None and not old_task.done():
            old_task.cancel()
        logger.info(f"⏲️ Auto-hangup re-armed for denial flow: {denial_secs}s")
    except Exception as e:
        logger.warning(f"Auto-hangup re-arm failed (original timer still active): {e}")

    logger.info(
        "🔀 DENIAL PIVOT: final claim is DENIED — asking the IVR for a "
        "representative instead of hanging up"
    )

    # Reason classification — GPT is authoritative. Keyword rules give an INSTANT
    # provisional guess over the claim readout (the IVR often names the reason,
    # e.g. "documentation submitted does not meet the code criteria"), but GPT
    # ALWAYS runs in the background to confirm/correct it — its result lands
    # before the rep picks up, and it overrides the keyword guess if they differ.
    if getattr(call_state, "denial_reason_key", None) is None:
        hit = match_reason_rules(claims_text)
        if hit is not None:
            call_state.denial_reason_key = hit.key
            call_state.denial_reason_verbatim = claims_text.strip()[-300:]
            call_state.denial_reason_provisional = True
            logger.info(f"🧭 Provisional reason from claim readout (keywords, GPT will confirm): {hit.key}")
        _schedule_reason_classification(call_state, claims_text)

    # Proactively ask for a live human — the claims menu is awaiting a command
    # right now. The phrase is payer-specific ("Representative" for Humana,
    # "customer service advocate" for Cigna). If TTS fails, the denial-IVR
    # prompt drives the next turn anyway.
    try:
        request_phrase = config_manager.get_denial_ivr_request_phrase()
        await speak_with_azure(request_phrase, call_id)
    except Exception as e:
        logger.warning(f"Pivot TTS failed (denial-IVR prompt will drive next turn): {e}")

    return True


async def _handle_denial_speech(text: str, call_control_id: str):
    """Handle one debounced utterance while the call is in a denial phase
    (denial_ivr → reaching a representative, denial_rep → live conversation).

    Serialized per call: two debounced chunks arriving close together used to
    fire two overlapping GPT turns that both spoke (garbled double-reply). A
    per-call lock now serializes turns, and a sequence guard drops a turn that
    was superseded by a newer utterance while it waited for the lock — so we
    respond ONCE, to the most recent utterance.
    """
    call_state = active_calls.get(call_control_id)
    if not call_state:
        return
    if not hasattr(call_state, "denial_turn_lock"):
        call_state.denial_turn_lock = asyncio.Lock()
    call_state.denial_turn_seq = getattr(call_state, "denial_turn_seq", 0) + 1
    my_seq = call_state.denial_turn_seq

    async with call_state.denial_turn_lock:
        # A newer utterance arrived while we waited → this turn is stale; the
        # newer one will respond with fuller context. Still record what the rep
        # said (append_ivr already did that in handle_user_speech) but don't
        # emit a second reply.
        if my_seq != getattr(call_state, "denial_turn_seq", my_seq):
            logger.info(f"⏭️ Superseded denial turn (seq {my_seq}) — skipping duplicate reply")
            return
        await _handle_denial_speech_locked(text, call_state, call_control_id)


async def _handle_denial_speech_locked(text: str, call_state, call_control_id: str):
    # The rep just said something → reset the hold-silence clock (and clear the
    # nudge count, since a responsive rep shouldn't be nudged).
    if call_state.phase == "denial_rep":
        _mark_rep_activity(call_state)

    # IVR announced the transfer → flip to rep phase before template selection.
    if call_state.phase == "denial_ivr" and is_transfer_signal(text):
        _enter_denial_rep_phase(call_state)

    # Maintain the denial-reason classification from this payer utterance
    # (free rules; GPT fallback scheduled in the background when cued).
    _update_denial_reason(call_state, text)

    fmt = build_denial_format_kwargs(call_state)

    def _build_prompt() -> str:
        if call_state.phase == "denial_rep":
            template = get_denial_prompt_template("representative")
            return template.format(
                transcript=text,
                conversation_history=_format_denial_history(call_state),
                denial_context_block=render_denial_context(call_state),
                **fmt,
            )
        template = get_denial_prompt_template("ivr")
        return template.format(transcript=text, **fmt)

    # Rep-phase replies are full conversational sentences — the default
    # max_tokens=50 (sized for one-word IVR commands) can truncate them
    # mid-sentence. 80 covers the longest template-style replies with room.
    _DENIAL_MAX_TOKENS = 80

    t0 = time.perf_counter()
    response = await _call_gpt_api(_build_prompt(), max_tokens=_DENIAL_MAX_TOKENS)
    gpt_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"GPT latency (denial/{call_state.phase}): {gpt_ms:.0f} ms")
    logger.info(f"GPT response (denial/{call_state.phase}): {response!r}")

    # rep_mode safety net: a live human picked up but no transfer phrase was
    # heard. Flip phase and re-run THIS chunk under the rep template so the
    # human's greeting gets a proper conversational response.
    if call_state.phase == "denial_ivr" and _is_gpt_rep_mode_signal(response):
        logger.info("→ GPT signaled rep_mode (human picked up — no transfer phrase heard)")
        _enter_denial_rep_phase(call_state)
        response = await _call_gpt_api(_build_prompt(), max_tokens=_DENIAL_MAX_TOKENS)
        logger.info(f"GPT response (denial/rep re-run): {response!r}")

    append_conversation_step(call_state, text, response)
    await process_llama_response(response, call_control_id)


# ─── 1. handle_user_speech: decorate transcript into a full prompt ────────────

async def handle_user_speech(transcript: str, call_control_id: str):

    #logger.info(f"🎯 handle_user_speech CALLED with transcript length: {len(transcript)}")
    #logger.info(f"📝 Whole transcript: {transcript}")

    text = transcript.strip()
    if not transcript or len(transcript) < 3:
        logger.warning(f"Transcript too short, skipping")
        return

    call_state = active_calls.get(call_control_id)

    # Bail if the call is already gone (late STT final arrived after cleanup).
    # Without this guard, visit_data becomes {} and the prompt template's
    # first {placeholder} (usually {tax_id}) triggers a KeyError that surfaces
    # as "Task exception was never retrieved" in App Insights.
    if not call_state:
        logger.warning(
            f"handle_user_speech: call_state gone for {call_control_id} — "
            f"skipping (late STT after cleanup)"
        )
        return

    append_ivr(call_state, text)

    # ── denial follow-up routing (post-pivot phases only) ───────────────────
    # Once the call pivoted into the denial flow, every utterance goes to the
    # denial handler — the claim-status ladder below is bypassed entirely.
    # Calls that never pivot (phase == "claim_status") are unaffected.
    if getattr(call_state, "phase", "claim_status") in ("denial_ivr", "denial_rep"):
        await _handle_denial_speech(text, call_control_id)
        return

    # ── claim routing (the only logic in main) ──────────────────────────────
    if call_state:
        if is_claim_not_found(text):
            logger.info("❌ No claims found for this patient. Ending call.")
            await ensure_call_cleanup(call_control_id, reason="claims: not found", send_hangup=True)
            return


        # ENTER claim mode (real-time keyword match)
        if not call_state.claim_mode and is_claim_start(text):
            await _enter_claim_mode_and_forward(call_state, call_control_id, text)
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

    # Visit data was fetched from the Clinical API in /v1/Billing-Agent/Call
    # and stored on CallState. Required fields were validated there, so by this
    # point visit_data has everything the prompt template needs.
    visit_data = (call_state.visit_data or {}) if call_state else {}
    # Caller (agent) identity — the bot is the CALLER from the provider's office,
    # NOT the patient. Used only by prompts that ask "who am I talking to" (e.g.
    # Cigna: "say and spell your first and last name"). Env-configurable; prompts
    # that don't reference these placeholders simply ignore the extra keys.
    fmt = {
        **visit_data,
        "transcript": transcript,
        "agent_persona_name": os.getenv("DENIAL_AGENT_PERSONA_NAME", "Miranda"),
        "agent_persona_last_name": os.getenv("DENIAL_AGENT_PERSONA_LAST_NAME", "Bell"),
        # Short recent-turn history so the prompt has context for "repeat that"
        # and other back-references. Templates that don't use {conversation_history}
        # simply ignore this key.
        "conversation_history": _format_claim_history(call_state),
    }
    prompt = prompt_template.format(**fmt)

    t0 = time.perf_counter()
    response = await _call_gpt_api(prompt)
    if call_state:
        append_conversation_step(call_state, text, response)
    gpt_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"GPT latency: {gpt_ms:.0f} ms")
    logger.info(f"GPT response: {response!r}")

    # GPT-driven claim_mode fallback: if the real-time is_claim_start missed
    # the IVR phrasing, GPT may recognize it semantically and return the
    # exact word "claim_mode". Strict-equality match — cannot collide with
    # say/value/confirm/dtmf/endcall/fallback formats.
    if _is_gpt_claim_mode_signal(response) and not call_state.claim_mode:
        logger.info("→ GPT signaled claim_mode (safety net — real-time detector missed)")
        await _enter_claim_mode_and_forward(call_state, call_control_id, text)
        return

    await process_llama_response(response, call_control_id)



async def send_dtmf(digits: str, call_control_id: str):
    """Send DTMF tones to the call (digits already sanitized by caller)."""
    try:
        cleaned = "".join(ch for ch in digits if ch.isdigit() or ch in "*#")
        # If you need durations, we can extend telnyx_client to accept them.
        await telnyx_client.send_dtmf(call_control_id, cleaned, TELNYX_BASE_URL, HEADERS)
        append_agent_dtmf(active_calls.get(call_control_id), cleaned)
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
# Denial pivot: the claims controller's STOP path consults this before the
# normal end-of-claims hangup (see _pivot_to_denial_flow above).
claims_agent.register_denial_pivot(_pivot_to_denial_flow)


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
        # Wrap auto_hangup so dependencies are passed automatically.
        # Note: delay_seconds is supplied by orchestrate.py from the insurance
        # config — the default here is only a safety net.
        auto_hangup_fn=lambda call_id, delay_seconds: auto_hangup(
            call_id,
            active_calls,
            ensure_call_cleanup,
            delay_seconds
        ),
    )
)


# DEV-ONLY test endpoint (inline visit data, no PracticeEHR writes).
# Route returns 404 unless ENABLE_TEST_CALL_ENDPOINT=true — safe to ship.
from src.api.v1.orchestrate_test import make_orchestrate_test_router
app.include_router(
    make_orchestrate_test_router(
        active_calls,
        initiated_events,
        TELNYX_BASE_URL=TELNYX_BASE_URL,
        HEADERS=HEADERS,
        TEL_FROM=TEL_FROM,
        CALL_CONTROL_APP_ID=CALL_CONTROL_APP_ID,
        WEBHOOK_BASE_URL=WEBHOOK_BASE_URL,
        STREAM_BASE_URL=STREAM_BASE_URL,
        auto_hangup_fn=lambda call_id, delay_seconds: auto_hangup(
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
            set_call_id(call_id)
            await ensure_call_cleanup(call_id, reason="shutdown", send_hangup=True)
        except Exception as e:
            logger.error(f"❌ Cleanup error for {call_id}: {e}")
    stt_manager.cleanup_all()
    # Let any in-flight post-call uploads finish (bounded) before tearing down
    # the shared HTTP client, so they aren't cut off or forced to rebuild a
    # fresh, never-closed client on the way out.
    from src.services.call_cleanup import drain_pending_uploads
    from src.services.http_client import aclose_http_client
    await drain_pending_uploads(timeout=float(os.getenv("SHUTDOWN_UPLOAD_DRAIN_S", "8")))
    await aclose_http_client()
    logger.info("✅ All calls hung up and STT sessions cleaned up. Goodbye!")


@app.get("/")
async def health_check():
    return {"status": "ok", "app_version": os.getenv("APP_VERSION", "unknown")}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, 
                host="0.0.0.0",
                port=5000,
                reload=False,
                )


#  TO run hit the start_call endpoint

#curl -X POST http://localhost:5000/start_call -H "Content-Type: application/json" -d "{}"
#curl -X POST "http://localhost:5000/v1/Billing-Agent/Call?wait_for_initiated_ms=10000" -H "Content-Type: application/json" -H "Authorization: Bearer <JWT>" -d "{\"visit_id\":\"...\",\"customer_id\":\"...\"}"