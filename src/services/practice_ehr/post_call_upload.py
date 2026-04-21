"""
End-of-call orchestration:
  1) Wait briefly for Telnyx to finalize the recording
  2) Fetch recording from Telnyx
  3) Build JSON transcript from call_state
  4) Upload both to PracticeEHR with different UUIDs under the same folder
"""
import asyncio
import logging
import os
import uuid

from src.services.practice_ehr.telnyx_recording import fetch_recording_wav
from src.services.practice_ehr.transcript_builder import build_transcript_json
from src.services.practice_ehr.uploader import upload_file

logger = logging.getLogger(__name__)

RECORDING_AVAILABILITY_DELAY_S = 5


async def upload_call_artifacts(snapshot: dict) -> None:
    """
    Uploads the recording and the transcript JSON for a finished call.

    `snapshot` is a dict captured BEFORE active_calls cleanup so we don't
    depend on the CallState still being around.
    Expected keys: call_session_id, customer_id, auth_token, full_transcript.
    """
    call_session_id = snapshot.get("call_session_id")
    customer_id = snapshot.get("customer_id")
    visit_id = snapshot.get("visit_id")
    auth_token = snapshot.get("auth_token")
    transcript_lines = len(snapshot.get("full_transcript", []) or [])

    logger.info(
        f"📤 post_call_upload START: customer_id={customer_id} visit_id={visit_id} "
        f"call_session_id={call_session_id} transcript_lines={transcript_lines} "
        f"auth_token_present={bool(auth_token)}"
    )

    if not customer_id:
        logger.warning("post_call_upload: no customer_id, skipping")
        return
    if not visit_id:
        logger.warning("post_call_upload: no visit_id, skipping")
        return
    if not auth_token:
        logger.warning("post_call_upload: no auth_token, skipping")
        return

    folder = f"billing/agent/claim/{customer_id}/{visit_id}"

    # Give Telnyx a few seconds to save the recording on its side.
    logger.info(f"⏳ Sleeping {RECORDING_AVAILABILITY_DELAY_S}s for Telnyx to finalize recording")
    await asyncio.sleep(RECORDING_AVAILABILITY_DELAY_S)

    # 1) Recording
    telnyx_api_key = os.getenv("TELNYX_API_KEY", "")
    if call_session_id:
        logger.info(f"🎙 Fetching Telnyx recording for session {call_session_id}")
        result = await fetch_recording_wav(call_session_id, telnyx_api_key)
        if result:
            wav_bytes, duration = result
            recording_uuid = str(uuid.uuid4())
            recording_path = f"{folder}/{recording_uuid}.wav"
            logger.info(
                f"📥 Got recording: {len(wav_bytes)} bytes, {duration}s — uploading to {recording_path}"
            )
            await upload_file(
                file_bytes=wav_bytes,
                file_path=recording_path,
                filename=f"{recording_uuid}.wav",
                content_type="audio/wav",
                auth_token=auth_token,
            )
        else:
            logger.warning("post_call_upload: no recording to upload")
    else:
        logger.warning("post_call_upload: no call_session_id, skipping recording")

    # 2) Transcript
    transcript_bytes = build_transcript_json_from_snapshot(snapshot)
    transcript_uuid = str(uuid.uuid4())
    transcript_path = f"{folder}/{transcript_uuid}.json"
    logger.info(
        f"📝 Uploading transcript ({len(transcript_bytes)} bytes, {transcript_lines} lines) to {transcript_path}"
    )
    await upload_file(
        file_bytes=transcript_bytes,
        file_path=transcript_path,
        filename=f"{transcript_uuid}.json",
        content_type="application/json",
        auth_token=auth_token,
    )
    logger.info("✅ post_call_upload DONE")


def build_transcript_json_from_snapshot(snapshot: dict) -> bytes:
    """Helper to reuse build_transcript_json against a snapshot dict."""
    class _Shim:
        pass
    shim = _Shim()
    shim.full_transcript = snapshot.get("full_transcript", [])
    return build_transcript_json(shim)


def snapshot_call_state(call_state) -> dict:
    """Capture the minimum data needed for a background upload."""
    return {
        "call_session_id": getattr(call_state, "call_session_id", None),
        "customer_id": getattr(call_state, "customer_id", None),
        "visit_id": getattr(call_state, "visit_id", None),
        "auth_token": getattr(call_state, "auth_token", None),
        "full_transcript": list(getattr(call_state, "full_transcript", []) or []),
    }
