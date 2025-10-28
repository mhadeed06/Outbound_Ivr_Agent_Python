# models.py
"""
Data models for the outbound agent (extracted from main.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from pydantic import BaseModel
import uuid

from src.config.insurance_config import config_manager

if TYPE_CHECKING:
    # Only for type hints (prevents runtime circular imports)
    from src.services.azure.stt_service import AzureRealtimeSttService


# ⬇️ SAME as in your original main.py
class SimpleCallRequest(BaseModel):
    agent_id: str
    app_id: str
    # keep the same default if you had it in main.py
    wait_for_initiated_ms: int | None = 2000


# ⬇️ SAME as in your original main.py
@dataclass
class CallState:
    """State management for active calls"""
    call_control_id: str
    status: str = "initiated"
    start_time: datetime = field(default_factory=datetime.now)
    websocket_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    azure_stt_session: Optional["AzureRealtimeSttService"] = None
    conversation_history: List[str] = field(default_factory=list)
    # IDs from Telnyx
    agent_id: Optional[str] = None
    app_id: Optional[str] = None

    claim_mode: bool = False
    debounce_seconds: float = None  # set in __post_init__
    need_debounce_reset: bool = False

    def __post_init__(self):
        # default to the global baseline
        if self.debounce_seconds is None:
            self.debounce_seconds = config_manager.get_debounce_seconds()
            print("debounce secs")
            print(self.debounce_seconds)
