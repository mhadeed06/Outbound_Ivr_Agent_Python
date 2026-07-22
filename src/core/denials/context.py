"""
Per-turn context building for the denial follow-up prompts.

- `build_denial_format_kwargs()` — assembles the knowledge-sheet values the
  denial templates need. Sources from CallState.visit_data (the call already
  fetched it for the claim-status flow) and fills the fields the Clinical API
  does not provide yet (billed_amount, provider_name, persona, callback
  number) from env-configurable TEST DEFAULTS. Those defaults exist because
  the Clinical APIs on QA are seeded for manual test calls — once the backend
  exposes the real fields, visit_data wins automatically.

- `render_denial_context()` — renders the {denial_context_block} injected
  into the rep template every turn: the denial reason as understood so far
  plus the question checklist. Until the 13-reason registry lands (next
  phase), this always renders the GENERIC question set — ICN + cause +
  corrective action + deadline are safe and useful for every denial.

Templates are lru_cache'd by the loader, so ALL dynamic content must flow
through .format() placeholders — never edit template text at runtime.
"""
import logging
import os

logger = logging.getLogger(__name__)


# Safe-for-every-denial question set, used until the reason is classified
# (and permanently for reasons outside the supported registry).
GENERIC_QUESTIONS = (
    "The ICN number (claim control number) for the denied claim",
    "What specifically caused the denial",
    "What the corrective action is, and where to send it (fax number, portal, or address)",
    "The time limit for resubmission or appeal from the date of the denial",
)


def build_denial_format_kwargs(call_state) -> dict:
    """Knowledge-sheet values for the denial templates.

    visit_data keys pass through untouched; test-only fields fall back to
    env-configurable defaults (see module docstring).
    """
    visit_data = getattr(call_state, "visit_data", None) or {}
    return {
        **visit_data,
        "billed_amount": visit_data.get("billed_amount")
        or os.getenv("DENIAL_TEST_BILLED_AMOUNT", "not available"),
        "provider_name": visit_data.get("provider_name")
        or os.getenv("DENIAL_TEST_PROVIDER_NAME", "the provider on file"),
        "agent_persona_name": os.getenv("DENIAL_AGENT_PERSONA_NAME", "Kevin"),
        "callback_number": os.getenv("DENIAL_CALLBACK_NUMBER", "469-581-2969"),
    }


def render_denial_context(call_state) -> str:
    """Render the per-turn {denial_context_block} for the rep template.

    Phase 1: reason display + generic checklist. The 13-reason registry
    (denial_reasons.py) will swap in reason-specific questions here without
    touching the template.
    """
    reason_key = getattr(call_state, "denial_reason_key", None)
    verbatim = getattr(call_state, "denial_reason_verbatim", None)

    if verbatim:
        reason_line = f'Denial reason (as understood so far): "{verbatim}"'
    elif reason_key:
        reason_line = f"Denial reason (as understood so far): {reason_key}"
    else:
        reason_line = (
            "Denial reason: NOT YET IDENTIFIED — your first priority is to "
            "ask the representative why the claim was denied."
        )

    lines = [reason_line, "", "Questions to work through (ask only what's still open, one at a time):"]
    for i, q in enumerate(GENERIC_QUESTIONS, 1):
        lines.append(f"{i}. [OPEN] {q}")
    return "\n".join(lines)
