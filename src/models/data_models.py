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

    # Billing-Agent/Log row id, created at call start with placeholder values
    # and PUT-updated with the final outcome at call end. Returned to the
    # frontend in /v1/Billing-Agent/Call so they can track the call. May be
    # None if the initial POST to Billing-Agent/Log failed — the call itself
    # still proceeds; post_call_upload falls back to a single POST in that case.
    ref_no: Optional[int] = None

    # Flat list of every utterance/action in the call.
    # Shape: [{"speaker": "ivr" | "agent", "text": "..."}]
    full_transcript: List[dict] = field(default_factory=list)

    claim_mode: bool = False
    debounce_seconds: float = None
    need_debounce_reset: bool = False

    segmentation_silence_ms: int = None
    need_segmentation_reset: bool = False

    # Reference to the currently-running debounce task in stream.py so
    # ensure_call_cleanup can cancel it when the call ends. Prevents late
    # STT finals from firing handle_user_speech for a call that's already
    # been cleaned out of active_calls (would otherwise crash with
    # KeyError on the first template placeholder).
    debounce_task: Optional[object] = None

    # Reference to the auto-hangup watchdog task started in orchestrate.py.
    # Stored so the denial pivot can cancel the claim-status timer and re-arm
    # a longer one (rep hold queues outlive the original budget).
    auto_hangup_task: Optional[object] = None

    # ── denial follow-up (in-call pivot) ────────────────────────────────────
    # Request opt-in: frontend sent denial_follow_up=true on /v1/Billing-Agent/Call.
    denial_follow_up: bool = False
    # Which conversational phase this call is in:
    #   "claim_status" → the normal flow (default; behavior identical to today)
    #   "denial_ivr"   → post-pivot, navigating the IVR to reach a representative
    #   "denial_rep"   → talking to a live human representative
    phase: str = "claim_status"
    # Live rule-based flag: a claim-readout chunk mentioned a denial.
    # Cheap signal only — the pivot decision is confirmed by one GPT check.
    denial_candidate: bool = False
    # Set once the pivot actually happened (used by post-call/logging).
    denial_pivoted: bool = False
    # Denial-reason engine state (populated in later phases of the feature):
    # registry key of the classified reason, verbatim reason text, and the
    # per-question checklist {index: "OPEN"|"ASKED"|"ANSWERED"}.
    denial_reason_key: Optional[str] = None
    denial_reason_verbatim: Optional[str] = None
    denial_checklist: Optional[dict] = None
    # Background GPT reason-classification task — cancelled in cleanup step 0.
    denial_reason_task: Optional[object] = None
    # How many GPT reason-classification attempts have fired (hard cap 2).
    denial_gpt_attempts: int = 0
    # Index into conversation_history where the denial flow began — the rep
    # prompt's history block only shows entries from this point on (earlier
    # claim-status menu turns would be noise to the rep conversation).
    denial_history_start: int = 0

    def __post_init__(self):
        if self.debounce_seconds is None:
            self.debounce_seconds = config_manager.get_debounce_seconds()

        if self.segmentation_silence_ms is None:
            self.segmentation_silence_ms = config_manager.get_segmentation_silence_ms()
