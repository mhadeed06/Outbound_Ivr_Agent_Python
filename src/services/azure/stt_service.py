import asyncio
import queue
import threading
import os
import azure.cognitiveservices.speech as speechsdk
from azure.cognitiveservices.speech.audio import AudioStreamFormat, PushAudioInputStream
from typing import Dict, Callable, Optional
from enum import Enum
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

AZURE_SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")


class EventType(Enum):
    PARTIAL = "partial"
    FINAL   = "final"
    ERROR   = "error"


@dataclass
class TranscriptionEvent:
    event_type: EventType
    text: str
    websocket_id: str


def convert_mulaw_to_pcm(mulaw_data: bytes) -> bytes:
    """Convert 8 kHz μ-law to 16-bit PCM."""
    import audioop
    return audioop.ulaw2lin(mulaw_data, 2)


class AzureRealtimeSttService:
    """
    Real-time STT using Azure Speech SDK.
    Automatically segments on silence and never stops listening.
    """

    def __init__(self, websocket_id: str):
        self.websocket_id = websocket_id
        self.recognizer: Optional[speechsdk.SpeechRecognizer] = None
        self.push_stream: Optional[PushAudioInputStream] = None
        self.audio_queue = queue.Queue()
        self.is_running = False
        self.recognition_thread: Optional[threading.Thread] = None

        # Your callbacks
        self.on_partial_result: Optional[Callable[[str], asyncio.Future]] = None
        self.on_final_result:   Optional[Callable[[str], asyncio.Future]] = None
        self.on_error:          Optional[Callable[[str], asyncio.Future]] = None

    def initialize(
        self,
        on_partial_result: Optional[Callable[[str], asyncio.Future]] = None,
        on_final_result:   Optional[Callable[[str], asyncio.Future]] = None,
        on_error:          Optional[Callable[[str], asyncio.Future]] = None,
        segmentation_silence_ms: int = None,  # Accept per-insurer baseline
    ):
        """Configure the recognizer, enable dictation and silence segmentation."""
        self.on_partial_result = on_partial_result
        self.on_final_result   = on_final_result
        self.on_error          = on_error

        speech_config = speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION
        )
        speech_config.speech_recognition_language = "en-US"
        speech_config.enable_dictation()

        # End-of-utterance silence (keep if you use it globally)
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
            "1600"
        )

        # Segmentation silence — now parameterized
        speech_config.set_property(
            speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
            str(segmentation_silence_ms)
        )

        # Audio format matches Telnyx μ-law stream
        audio_format = AudioStreamFormat(
            samples_per_second=8000, bits_per_sample=16, channels=1
        )
        self.push_stream = PushAudioInputStream(stream_format=audio_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)

        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config
        )

        # Wire up event handlers
        self.recognizer.recognizing.connect(self._handle_recognizing)
        self.recognizer.recognized.connect(self._handle_recognized)
        self.recognizer.session_started.connect(self._handle_session_started)
        self.recognizer.session_stopped.connect(self._handle_session_stopped)
        self.recognizer.canceled.connect(self._handle_canceled)

        self.is_running = True

    # ---------- NEW: live update for segmentation timeout ----------
    def update_segmentation_timeout(self, segmentation_silence_ms: int):
        """
        Update the segmentation silence timeout at runtime.
        Preferred: set recognizer property directly.
        Fallback: recreate recognizer with new timeout if live update is ignored.
        """
        if not self.recognizer:
            return
        try:
            # Try live property update
            self.recognizer.properties.set_property(
                speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
                str(segmentation_silence_ms)
            )
            print(f"[{self.websocket_id}] 🔄 Updated Speech_SegmentationSilenceTimeoutMs to {segmentation_silence_ms} ms")
        except Exception as e:
            print(f"[{self.websocket_id}] ⚠️ Live update failed: {e}")
            self._recreate_with_new_timeout(segmentation_silence_ms)

    def _recreate_with_new_timeout(self, segmentation_silence_ms: int):
        """
        Stop current recognizer and rebuild with the new timeout.
        Reuses the same push stream; rewires event handlers; restarts if it was running.
        """
        try:
            was_running = self.is_running
            if self.recognizer:
                try:
                    self.recognizer.stop_continuous_recognition()
                except Exception:
                    pass

            # Rebuild config
            speech_config = speechsdk.SpeechConfig(
                subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION
            )
            speech_config.speech_recognition_language = "en-US"
            speech_config.enable_dictation()

            # Keep these aligned with your baseline policy
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
                "1600"
            )
            speech_config.set_property(
                speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
                str(segmentation_silence_ms)
            )

            # Reuse same audio stream
            audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)
            self.recognizer = speechsdk.SpeechRecognizer(
                speech_config=speech_config,
                audio_config=audio_config
            )

            # Re-wire events
            self.recognizer.recognizing.connect(self._handle_recognizing)
            self.recognizer.recognized.connect(self._handle_recognized)
            self.recognizer.session_started.connect(self._handle_session_started)
            self.recognizer.session_stopped.connect(self._handle_session_stopped)
            self.recognizer.canceled.connect(self._handle_canceled)

            if was_running:
                self.recognizer.start_continuous_recognition()

            print(f"[{self.websocket_id}] ✅ Recreated recognizer with {segmentation_silence_ms} ms")
        except Exception as e:
            print(f"[{self.websocket_id}] ❌ Recreate recognizer failed: {e}")
    # ---------- /NEW ------------------------------------------------

    def start_continuous_recognition(self):
        """Launch the background thread for continuous recognition."""
        if self.recognizer and not self.recognition_thread:
            self.recognition_thread = threading.Thread(
                target=self._recognition_worker, daemon=True
            )
            self.recognition_thread.start()

    def _recognition_worker(self):
        """Thread: run the recognizer until stopped."""
        try:
            self.recognizer.start_continuous_recognition()
            print(f"Started continuous recognition")
            while self.is_running:
                # Keep thread alive
                asyncio.run(asyncio.sleep(0.1))
        except Exception as e:
            print(f"[{self.websocket_id}] Recognition worker error: {e}")
            if self.on_error:
                asyncio.create_task(self.on_error(str(e)))

    def feed_audio(self, audio_data: bytes):
        """Write each PCM chunk into the push stream."""
        if self.push_stream and self.is_running:
            try:
                self.push_stream.write(audio_data)
            except Exception as e:
                print(f"[{self.websocket_id}] Error feeding audio: {e}")

    def _handle_recognizing(self, evt):
        """Intermediate (partial) results."""
        if evt.result.text and self.on_partial_result:
            self.audio_queue.put(
                TranscriptionEvent(EventType.PARTIAL, evt.result.text, self.websocket_id)
            )

    def _handle_recognized(self, evt):
        """Final results (utterance complete)."""
        if evt.result.text and self.on_final_result:
            print(f" Final: {evt.result.text}")
            self.audio_queue.put(
                TranscriptionEvent(EventType.FINAL, evt.result.text, self.websocket_id)
            )

    def _handle_session_started(self, evt):
        print(f"[STT {self.websocket_id}] Speech session started")

    def _handle_session_stopped(self, evt):
        print(f"[STT {self.websocket_id}] Speech session stopped (session_id={getattr(evt, 'session_id', '?')})")

    def _handle_canceled(self, evt):
        # Always print the full cancellation context. If STT bails without
        # producing transcripts, this is the line that explains why.
        reason = getattr(evt, "reason", None)
        details = getattr(evt, "error_details", "")
        code = getattr(evt, "error_code", "")
        print(f"[STT {self.websocket_id}] ❌ Recognition canceled: reason={reason} code={code} details={details!r}")
        if reason == speechsdk.CancellationReason.Error and self.on_error:
            asyncio.create_task(self.on_error(f"Error: {details}"))

    def stop(self):
        """Stops recognition and cleans up."""
        self.is_running = False
        if self.recognizer:
            self.recognizer.stop_continuous_recognition()
        if self.push_stream:
            self.push_stream.close()
        if self.recognition_thread:
            self.recognition_thread.join(timeout=5.0)
        print(f" Cleanup complete")

    def start_async_event_handler(self, loop: asyncio.AbstractEventLoop):
        """Begin pulling events off the queue on your main loop."""
        loop.create_task(self._process_events())

    async def _process_events(self):
        while self.is_running:
            try:
                event = await asyncio.get_event_loop().run_in_executor(None, self.audio_queue.get)
                if event.event_type == EventType.PARTIAL and self.on_partial_result:
                    await self.on_partial_result(event.text)
                elif event.event_type == EventType.FINAL and self.on_final_result:
                    await self.on_final_result(event.text)
                elif event.event_type == EventType.ERROR and self.on_error:
                    await self.on_error(event.text)
            except Exception as e:
                print(f"[{self.websocket_id}] Error in event handler: {e}")
                break


class AzureRealtimeSttManager:
    """Keeps track of all concurrent STT sessions."""
    def __init__(self):
        self.active_sessions: Dict[str, AzureRealtimeSttService] = {}

    def create_session(self, websocket_id: str) -> AzureRealtimeSttService:
        if websocket_id in self.active_sessions:
            self.remove_session(websocket_id)
        session = AzureRealtimeSttService(websocket_id)
        self.active_sessions[websocket_id] = session
        return session

    def get_session(self, websocket_id: str) -> Optional[AzureRealtimeSttService]:
        return self.active_sessions.get(websocket_id)

    def remove_session(self, websocket_id: str):
        svc = self.active_sessions.pop(websocket_id, None)
        if svc:
            svc.stop()

    def cleanup_all(self):
        for wid in list(self.active_sessions):
            self.remove_session(wid)


# Global manager instance to import in your main app
stt_manager = AzureRealtimeSttManager()
