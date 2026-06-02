# insurance_config.py
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class InsuranceConfig:
    """Configuration for each insurance provider"""
    name: str
    phone_number: str
    debounce_seconds: float
    claim_debounce_seconds: float
    claims_tail_chars: int
    prompt_template: str
    claims_prompt_template: str
    segmentation_silence_ms: int  # For normal flow
    claim_segmentation_silence_ms: int  # For claim flow
    auto_hangup_seconds: int  # Force-hangup if the call runs longer than this
    # When True, skip a GPT claims-controller call if the new chunk is near-
    # identical to the last chunk we sent to GPT (handles STT trailing-char
    # races that caused duplicate "Details"/"Next claim" responses on CIGNA).
    dedupe_chunks: bool = False


# All insurance configurations
INSURANCE_CONFIGS: Dict[str, InsuranceConfig] = {
    "CIGNA": InsuranceConfig(
        name="CIGNA",
        phone_number="+18009971654",
        debounce_seconds=0.5,
        claim_debounce_seconds=2,
        claims_tail_chars=300,
        prompt_template="CIGNA_PROMPT_TEMPLATE",
        claims_prompt_template="CIGNA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=600,
        claim_segmentation_silence_ms=1700,
        auto_hangup_seconds=900,
        dedupe_chunks=True,
    ),

    "HUMANA": InsuranceConfig(
        name="HUMANA",
        phone_number="+18007834599",
        debounce_seconds=0.4,
        claim_debounce_seconds=1.2,
        claims_tail_chars=150,
        prompt_template="HUMANA_PROMPT_TEMPLATE",
        claims_prompt_template="HUMANA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=500,
        claim_segmentation_silence_ms=1300,
        auto_hangup_seconds=1600,
    ),

    "BAYLOR_SCOTT": InsuranceConfig(
        name="BAYLOR_SCOTT",
        phone_number="+18555727238",
        debounce_seconds=0.1,
        claim_debounce_seconds=1.7,
        claims_tail_chars=200,
        prompt_template="BAYLOR_SCOTT_PROMPT_TEMPLATE",
        claims_prompt_template="BAYLOR_SCOTT_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=600,
        claim_segmentation_silence_ms=1000,
        auto_hangup_seconds=900,
    ),

    "OSCAR": InsuranceConfig(
        name="OSCAR",
        phone_number="+18556722755",
        debounce_seconds=0.0,
        claim_debounce_seconds=1.2,
        claims_tail_chars=250,
        prompt_template="OSCAR_PROMPT_TEMPLATE",
        claims_prompt_template="OSCAR_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1400,
        claim_segmentation_silence_ms=1800,
        auto_hangup_seconds=900,
    ),

    # Kept for in-progress development. No payer_id maps to it yet, so it
    # is unreachable via /orchestrate_call_simple until it's wired up.
    "HEALTH_FIRST": InsuranceConfig(
        name="HEALTH_FIRST",
        phone_number="+18882502220",
        debounce_seconds=0.1,
        claim_debounce_seconds=1,
        claims_tail_chars=200,
        prompt_template="HEALTH_FIRST_PROMPT_TEMPLATE",
        claims_prompt_template="HEALTH_FIRST_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1400,
        claim_segmentation_silence_ms=1800,
        auto_hangup_seconds=900,
    ),
}


# ── payer_id → insurance mapping ────────────────────────────────────────────
# Only the insurers whose flow is production-ready. Unknown payer_ids are
# rejected by /orchestrate_call_simple with a 400 error.
PAYER_ID_TO_INSURANCE: Dict[str, str] = {
    "61101": "HUMANA",
    "62308": "CIGNA",
    "94999": "BAYLOR_SCOTT",
    "OSCAR": "OSCAR",
}

SUPPORTED_PAYER_IDS_HELP = ", ".join(
    f"{pid} ({name})" for pid, name in PAYER_ID_TO_INSURANCE.items()
)


def lookup_by_payer_id(payer_id) -> Optional[InsuranceConfig]:
    """Return the InsuranceConfig for a given payer_id, or None if unsupported."""
    if payer_id is None:
        return None
    key = str(payer_id).strip()
    # Be case-tolerant only for string-keyed payers like "OSCAR"
    name = PAYER_ID_TO_INSURANCE.get(key) or PAYER_ID_TO_INSURANCE.get(key.upper())
    if not name:
        return None
    return INSURANCE_CONFIGS.get(name)


# ── plan name → insurance mapping ───────────────────────────────────────────
# Insurance is chosen from the Clinical API's `planShortName` value.
# Matching is case-insensitive.
PAYER_NAME_TO_INSURANCE: Dict[str, str] = {
    "CIGNA-TEST": "CIGNA",
    "HUMANA-TEST": "HUMANA",
    "BAYLOR-TEST": "BAYLOR_SCOTT",
    "OSCAR-TEST": "OSCAR",
}


def lookup_by_payer_name(plan_short_name) -> Optional[InsuranceConfig]:
    """Return the InsuranceConfig for a given Clinical API planShortName,
    or None if unknown. Case-insensitive."""
    if not plan_short_name:
        return None
    key = str(plan_short_name).strip().upper()
    for name, insurance in PAYER_NAME_TO_INSURANCE.items():
        if name.upper() == key:
            return INSURANCE_CONFIGS.get(insurance)
    return None


# ── per-call active insurance (ContextVar) ──────────────────────────────────
# Each asyncio task has its own isolated value, so concurrent calls with
# different insurers never stomp on each other. asyncio.create_task copies
# the current context, so background work (post_call_upload etc.) inherits
# the correct insurance automatically.
_active_insurance_var: contextvars.ContextVar[Optional[InsuranceConfig]] = (
    contextvars.ContextVar("active_insurance", default=None)
)


def set_active_insurance(config: InsuranceConfig) -> None:
    """Set the active insurance for the current async task.
    Must be called at each call-entry point:
      - /orchestrate_call_simple (once the insurance is resolved from the plan name)
      - /webhooks/calls (restored from CallState.insurance_name)
      - /stream (restored from CallState.insurance_name on 'start')
    """
    _active_insurance_var.set(config)


def get_active_insurance() -> Optional[InsuranceConfig]:
    return _active_insurance_var.get()


def set_active_insurance_by_name(name: str) -> bool:
    """Convenience for webhooks/stream: set context by insurer name.
    Returns True on success, False if the name is unknown."""
    cfg = INSURANCE_CONFIGS.get(name) if name else None
    if cfg is None:
        return False
    _active_insurance_var.set(cfg)
    return True


class ConfigManager:
    """Reads the active insurance config from the current async task's context.

    All getters raise if set_active_insurance() hasn't been called in the
    current task. This is intentional: it surfaces misconfigured call flows
    immediately rather than silently falling back to the wrong insurer.
    """

    def get_config(self) -> InsuranceConfig:
        cfg = _active_insurance_var.get()
        if cfg is None:
            raise RuntimeError(
                "No active insurance config. set_active_insurance() must be "
                "called at each call-entry point (orchestrate / webhooks / stream)."
            )
        return cfg

    def get_phone_number(self) -> str:
        return self.get_config().phone_number

    def get_debounce_seconds(self) -> float:
        return self.get_config().debounce_seconds

    def get_claim_debounce_seconds(self) -> float:
        return self.get_config().claim_debounce_seconds

    def get_claims_tail_chars(self) -> int:
        return self.get_config().claims_tail_chars

    def get_insurance_name(self) -> str:
        return self.get_config().name

    def get_segmentation_silence_ms(self) -> int:
        return self.get_config().segmentation_silence_ms

    def get_claim_segmentation_silence_ms(self) -> int:
        return self.get_config().claim_segmentation_silence_ms

    def get_auto_hangup_seconds(self) -> int:
        return self.get_config().auto_hangup_seconds

    def get_dedupe_chunks(self) -> bool:
        return self.get_config().dedupe_chunks


# Global instance — stateless, reads from ContextVar on every call.
config_manager = ConfigManager()
