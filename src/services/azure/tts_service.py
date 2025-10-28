import asyncio
import base64
import json
import re
import time
import httpx
import logging

logger = logging.getLogger(__name__)

# --- Tiny-pause spelling (no global slowdown) ---
PAUSE_MS_EACH       = 120
NUMERIC_HEAVY_RATIO = 0.60

DATE_SLASH_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")

def _is_date_token(s: str) -> bool:
    return bool(DATE_SLASH_RE.match(s))

def _is_code_token(s: str) -> bool:
    if not s or any(ch.isspace() for ch in s):
        return False
    if "/" in s:
        return False
    has_letters = any(ch.isalpha() for ch in s)
    has_digits  = any(ch.isdigit() for ch in s)
    if not has_digits:
        return False
    digit_ratio = sum(ch.isdigit() for ch in s) / len(s)
    return has_letters or digit_ratio >= NUMERIC_HEAVY_RATIO


voice_name = "en-US-JennyNeural"

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

    # 2) Codes and long numbers → spell each character
    if _is_code_token(clean):
        tokens = []
        for ch in clean:
            if ch.isalpha():
                tokens.append(f'<lang xml:lang="en-US"><say-as interpret-as="characters">{ch}</say-as></lang>')
            elif ch.isdigit():
                tokens.append(f'<lang xml:lang="en-US"><say-as interpret-as="digits">{ch}</say-as></lang>')
            else:
                tokens.append(f'<break time="{PAUSE_MS_EACH}ms"/>')
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


TTS_REENABLE_ASR_DELAY = 0.2

async def speak_with_azure(
    text: str,
    call_control_id: str,
    active_calls: dict,
    AZURE_SPEECH_KEY: str,
    AZURE_SPEECH_REGION: str
):
    """Generate Azure TTS audio and stream it back via the same WebSocket."""
    call_state = active_calls.get(call_control_id)
    ws = getattr(call_state, "websocket", None)
    if not ws:
        logger.error("No WebSocket found for TTS")
        return
    
    try:
        if call_state is not None:
            call_state.conversation_history.append({"role": "assistant", "content": text})
        
    except Exception as e:
        logger.error(f"Error appending to conversation history: {e}")

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

            chunk_size = 800
            for offset in range(0, len(audio_bytes), chunk_size):
                chunk = audio_bytes[offset:offset + chunk_size]
                payload = base64.b64encode(chunk).decode("ascii")
                msg = {"event": "media", "media": {"track": "outbound", "payload": payload}}
                await ws.send_text(json.dumps(msg))
                await asyncio.sleep(0.1)

            await asyncio.sleep(TTS_REENABLE_ASR_DELAY)
            logger.info("TTS streaming complete")

        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            setattr(call_state, "is_tts_active", False)
