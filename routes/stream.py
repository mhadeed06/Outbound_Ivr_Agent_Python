# routes/stream.py
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Dict, Callable, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from data_models import CallState  # your dataclass

logger = logging.getLogger(__name__)

def make_stream_router(
    *,
    active_calls: Dict[str, CallState],
    DEBOUNCE_SECONDS: float,
    stt_manager,                          # pass the module/object from main
    convert_mulaw_to_pcm: Callable[[bytes], bytes],
    claims_agent,                         # pass the module
    ensure_call_cleanup: Callable[..., Any],
    handle_user_speech: Callable[[str, str], Any],
) -> APIRouter:
    """
    Factory that returns a router exposing /stream.
    It closes over your live state and helpers from main.py.
    """
    router = APIRouter()

    @router.websocket("/stream")
    async def media_stream_endpoint(websocket: WebSocket):
        logger.info("🔗 New WebSocket connection")
        await websocket.accept()

        call_control_id = None
        call_state      = None
        websocket_id    = str(uuid.uuid4())

        # ───────── Debounce state (per-connection) ─────────
        from types import SimpleNamespace
        state = SimpleNamespace(
            pending_finals=[],                                  # accumulate final STT chunks here
            debounce_task=None,                                 # the timer task we cancel/restart
            debounce_time=DEBOUNCE_SECONDS,                     # how long to wait for "silence" before processing
        )

        async def _process_after_quiet():
            try:
                await asyncio.sleep(state.debounce_time)
            except asyncio.CancelledError:
                return

            if not state.pending_finals:
                return

            text = " ".join(state.pending_finals).strip()
            state.pending_finals.clear()
            if not text:
                return

            if call_state and hasattr(call_state, "last_media_ts"):
                ms = (time.perf_counter() - call_state.last_media_ts) * 1000
                logger.info(f" Debounced STT latency: {ms:.0f} ms")

            if call_state:
                call_state.conversation_history.append({"role": "user", "content": text})
            await handle_user_speech(text, call_control_id)

        def _reschedule_debounce():
            if state.debounce_task and not state.debounce_task.done():
                state.debounce_task.cancel()
            state.debounce_task = asyncio.create_task(_process_after_quiet())

        async def _flush_pending_now():
            if state.debounce_task and not state.debounce_task.done():
                state.debounce_task.cancel()

            if state.pending_finals:
                text = " ".join(state.pending_finals).strip()
                state.pending_finals.clear()
                if text:
                    if call_state and hasattr(call_state, "last_media_ts"):
                        ms = (time.perf_counter() - call_state.last_media_ts) * 1000
                        logger.info(f" Debounced STT latency (flush): {ms:.0f} ms")
                    if call_state:
                        call_state.conversation_history.append({"role": "user", "content": text})
                    await handle_user_speech(text, call_control_id)

        # ───────── Azure STT callbacks ─────────
        async def on_partial(text: str):
            if call_state:
                desired = getattr(call_state, "debounce_seconds", DEBOUNCE_SECONDS)
                if state.debounce_time != desired:
                    state.debounce_time = desired
                if getattr(call_state, "need_debounce_reset", False):
                    _reschedule_debounce()
                    call_state.need_debounce_reset = False

            _reschedule_debounce()

        async def on_final(text: str):
            text = text.strip()
            if not text:
                return

            if call_state:
                desired = getattr(call_state, "debounce_seconds", DEBOUNCE_SECONDS)
                if state.debounce_time != desired:
                    state.debounce_time = desired
                if getattr(call_state, "need_debounce_reset", False):
                    _reschedule_debounce()
                    call_state.need_debounce_reset = False

            if call_state and hasattr(call_state, "last_media_ts"):
                ms = (time.perf_counter() - call_state.last_media_ts) * 1000
                logger.info(f" STT final piece latency: {ms:.0f} ms")

            state.pending_finals.append(text)
            _reschedule_debounce()

        async def on_error(err: str):
            logger.error(f"[{websocket_id}] STT error: {err}")

        # ───────── WebSocket receive loop ─────────
        try:
            while True:
                frame = await websocket.receive_text()
                msg   = json.loads(frame)
                ev    = msg.get("event")

                if ev == "start":
                    call_control_id = msg["start"]["call_control_id"]
                    logger.info(f" Call started: {call_control_id}")

                    call_state = active_calls.get(call_control_id)
                    if not call_state:
                        logger.warning(" Unknown call ID")
                        continue

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
                        if getattr(call_state, "is_tts_active", False):
                            pass
                        else:
                            call_state.azure_stt_session.feed_audio(pcm)

                elif ev == "stop":
                    logger.info(" Stream stopped")
                    await _flush_pending_now()
                    try:
                        await claims_agent.end_session(call_control_id)
                    except Exception:
                        pass
                    if call_state:
                        call_state.claim_mode = False
                    break

        except WebSocketDisconnect:
            if call_state:
                call_state.status = "websocket_close"
            logger.info(" WebSocket disconnected")

        except Exception as e:
            logger.error(f" Stream error: {e}")

        finally:
            try:
                await _flush_pending_now()
            except Exception:
                pass

            if call_control_id and call_control_id in active_calls:
                try:
                    await ensure_call_cleanup(
                        call_control_id,
                        reason="websocket: finally/disconnect",
                        send_hangup=True
                    )
                except Exception as e:
                    logger.error(f"[{websocket_id}] ensure_call_cleanup error: {e}")

    return router
