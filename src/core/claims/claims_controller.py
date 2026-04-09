import time
import asyncio
import logging
from src.config.insurance_config import config_manager
from src.core.prompts.claims_prompts import get_claims_prompt
from src.services.llm.llm_service import _call_gpt_api
from src.core.claims.claims_intent_mapper import map_keyword

logger = logging.getLogger(__name__)


def get_claims_tail_chars() -> int:
    """Get claims tail chars for current insurance"""
    return config_manager.get_claims_tail_chars()


def get_controller_prompt_template() -> str:
    """Get the controller prompt template for current insurance"""
    config = config_manager.get_config()
    return get_claims_prompt(config.claims_prompt_template)


async def handle_final(call_id: str, utterance: str):
    """
    Main calls this for EVERY debounced Final while in claim mode.
    Append -> send tail (last N chars) of transcript to GPT -> act on keyword.

    Imports claims_agent lazily to avoid circular import.
    """
    # Lazy import to break circular dependency
    from src.core.claims import claims_agent

    s = claims_agent._sessions.get(call_id)
    if not s or not s.get("active"):
        return

    utterance = (utterance or "").strip()
    if not utterance:
        return

    lock = claims_agent._locks.setdefault(call_id, asyncio.Lock())
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
        )
        s["last_response"] = intent

        # Store conversation history here (not inside _ask_gpt_keyword)
        # so we always capture the actual utterance, not the trimmed chunk
        claims_agent._append_conversation_step(call_id, utterance, intent)

        if intent.startswith("DTMF:"):
            digit = intent.split(":", 1)[1]
            if claims_agent._dtmf_cb:
                try:
                    await claims_agent._dtmf_cb(digit, call_id)
                    logger.info(f"[{call_id}] Sent DTMF: {digit}")
                except Exception as e:
                    logger.error(f"[{call_id}] Error sending DTMF: {e}")
            return

        if intent == "STOP":
            await claims_agent.end_session(call_id, already_locked=True)
            return

        if intent == "NEXT":
            claims_agent._finalize_current(s)
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("Next claim", call_id)
                except Exception:
                    pass
            return

        if intent == "DETAILS":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("Details", call_id)
                except Exception:
                    pass
            return

        if intent == "CONFIRM":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("Yes", call_id)
                except Exception:
                    pass
            return

        if intent == "NO":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("No", call_id)
                except Exception:
                    pass
            return

        if intent == "FAX-ID":
            if claims_agent._tts_cb:
                try:
                    await claims_agent._tts_cb("2144465424", call_id)
                except Exception:
                    pass
            return

        return


# ---------- GPT-4o controller (minimal logging) ----------

async def _ask_gpt_keyword(
    call_id: str,
    transcript_chunk: str,
    last_response: str,
) -> str:
    """
    Use GPT to return one control intent.
    Conversation history is stored by the caller (handle_final), not here.
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
            return "CONTINUE"

        intent = map_keyword(raw.upper())
        logger.info(f"[{call_id}] <- GPT: {raw!r} -> {intent} ({ms:.0f}ms)")
        return intent

    except Exception as e:
        logger.error(f"[{call_id}] GPT controller error: {e}")
        return "CONTINUE"
