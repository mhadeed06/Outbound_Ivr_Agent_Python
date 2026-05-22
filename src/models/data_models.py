from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from pydantic import BaseModel
import uuid

from src.config.insurance_config import config_manager

if TYPE_CHECKING:
    from src.services.azure.stt_service import AzureRealtimeSttService


class SimpleCallRequest(BaseModel):
    visit_id: str
    customer_id: str
    wait_for_initiated_ms: int | None = 2000


@dataclass
class CallState:
    """State management for active calls"""
    call_control_id: str
    status: str = "initiated"
    start_time: datetime = field(default_factory=datetime.now)
    websocket_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    azure_stt_session: Optional["AzureRealtimeSttService"] = None
    conversation_history: List[dict] = field(default_factory=list)

    # Request-provided IDs (from the frontend)
    visit_id: Optional[str] = None
    customer_id: Optional[str] = None

    # Resolved from the Clinical API plan name at call creation. Used by
    # webhooks/stream to restore the insurance ContextVar when a new request
    # for this call arrives.
    insurance_name: Optional[str] = None

    # Telnyx session ID (for fetching the recording later)
    call_session_id: Optional[str] = None

    # Bearer token captured from the frontend request — reused for PracticeEHR upload
    auth_token: Optional[str] = None

    # Per-customer API key resolved from /v1/Clients/AuthClients at call start.
    # Reused by the Clinical API and the post-call Billing-Agent/Log call.
    api_key: Optional[str] = None

    # Visit data fetched from the Clinical API at call start.
    # Keys match the prompt placeholders: tax_id, npi, member_id, dob, member_name, dos
    # Required keys vary by insurance (see src/services/clinical/required_fields.py).
    visit_data: Optional[dict] = None

    # Flat list of every utterance/action in the call.
    # Shape: [{"speaker": "ivr" | "agent", "text": "..."}]
    full_transcript: List[dict] = field(default_factory=list)

    claim_mode: bool = False
    debounce_seconds: float = None
    need_debounce_reset: bool = False

    segmentation_silence_ms: int = None
    need_segmentation_reset: bool = False

    def __post_init__(self):
        if self.debounce_seconds is None:
            self.debounce_seconds = config_manager.get_debounce_seconds()

        if self.segmentation_silence_ms is None:
            self.segmentation_silence_ms = config_manager.get_segmentation_silence_ms()
