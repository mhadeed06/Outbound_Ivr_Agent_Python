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
  plus the question checklist with [OPEN]/[ASKED]/[ANSWERED] markers.
  Questions come from the 13-reason registry (denial_reasons.py) once the
  reason is classified; the GENERIC set before that; and a graceful
  wrap-up block when the reason is OUT OF SCOPE.

  The ASKED/ANSWERED markers are recomputed from the conversation history
  on every render (stateless — no mutation to race on). They are ADVISORY:
  the history block in the prompt remains the ground truth GPT reasons
  over; the markers just sharpen the goal-check gate.

Templates are lru_cache'd by the loader, so ALL dynamic content must flow
through .format() placeholders — never edit template text at runtime.
"""
import logging
import os

from src.core.denials.denial_reasons import (
    CLOSING_QUESTIONS,
    GENERIC_QUESTIONS,
    OUT_OF_SCOPE_KEY,
    UNIVERSAL_QUESTIONS,
    get_reason,
)

logger = logging.getLogger(__name__)


def build_denial_format_kwargs(call_state) -> dict:
    """Knowledge-sheet values for the denial templates.

    visit_data keys pass through untouched; test-only fields fall back to
    env-configurable defaults (see module docstring).
    """
    visit_data = getattr(call_state, "visit_data", None) or {}
    provider_name = visit_data.get("provider_name") or os.getenv(
        "DENIAL_TEST_PROVIDER_NAME", "the provider on file"
    )
    # Rep authentication (Humana and others) often asks for the INDIVIDUAL
    # rendering provider / doctor name — distinct from the facility/group.
    # Falls back to the facility name if not provided, but the rep may reject
    # that; supply the real rendering provider whenever available.
    rendering_provider_name = (
        visit_data.get("rendering_provider_name")
        or visit_data.get("provider_first_last")
        or os.getenv("DENIAL_TEST_RENDERING_PROVIDER", provider_name)
    )
    # NPIs: a claim has an individual RENDERING provider NPI and the practice
    # has a GROUP/billing NPI. Reps may ask for either — carry both. {npi} (the
    # identification NPI used in the claim-status flow) is the rendering one by
    # convention here, so default rendering_provider_npi to it.
    rendering_provider_npi = visit_data.get("rendering_provider_npi") or visit_data.get("npi") or ""
    group_npi = visit_data.get("group_npi") or ""
    group_tax_id = visit_data.get("group_tax_id") or visit_data.get("tax_id") or ""
    return {
        **visit_data,
        "billed_amount": visit_data.get("billed_amount")
        or os.getenv("DENIAL_TEST_BILLED_AMOUNT", "not available"),
        "provider_name": provider_name,
        "rendering_provider_name": rendering_provider_name,
        "rendering_provider_npi": rendering_provider_npi or "not available",
        "group_npi": group_npi or "not available",
        "group_tax_id": group_tax_id or "not available",
        "agent_persona_name": os.getenv("DENIAL_AGENT_PERSONA_NAME", "Kevin"),
        "callback_number": os.getenv("DENIAL_CALLBACK_NUMBER", "469-581-2969"),
    }


def _denial_phase_history(call_state):
    """(role, text) pairs of the denial-phase conversation, oldest first.
    role is "bot" (our say-lines) or "rep" (payer-side utterances)."""
    start = getattr(call_state, "denial_history_start", 0)
    entries = (getattr(call_state, "conversation_history", None) or [])[start:]
    out = []
    for e in entries:
        heard = (e.get("transcript") or "").strip()
        replied = (e.get("gpt_result") or "").strip()
        if heard:
            out.append(("rep", heard.lower()))
        if replied:
            low = replied.lower()
            if low.startswith("say:"):
                out.append(("bot", low[4:].strip()))
    return out


def _question_status(question, history) -> str:
    """OPEN / ASKED / ANSWERED for one question, from the denial-phase history.

    ASKED    → one of our say-lines contains a question keyword.
    ANSWERED → a substantive rep utterance (>= 15 chars) came after that ask.
    """
    if not question.keywords:
        return "OPEN"
    asked_at = None
    for i, (role, text) in enumerate(history):
        if role == "bot" and any(kw in text for kw in question.keywords):
            asked_at = i
            break
    if asked_at is None:
        return "OPEN"
    for role, text in history[asked_at + 1:]:
        if role == "rep" and len(text) >= 15:
            return "ANSWERED"
    return "ASKED"


def render_denial_context(call_state) -> str:
    """Render the per-turn {denial_context_block} for the rep template."""
    reason_key = getattr(call_state, "denial_reason_key", None)
    verbatim = getattr(call_state, "denial_reason_verbatim", None)

    # ── Path C: reason outside the supported registry — graceful wrap-up ──
    if reason_key == OUT_OF_SCOPE_KEY:
        return (
            f'Denial reason (as stated): "{verbatim or "unrecognized"}"\n'
            "\n"
            "⚠️ This denial reason is OUTSIDE the scope this agent handles. Do NOT work\n"
            "through a detailed question list. Instead, wrap up gracefully:\n"
            "1. Confirm the denial reason back to the rep in one sentence so the\n"
            "   transcript captures it accurately.\n"
            "2. Get the ICN number (claim control number) if you don't have it yet.\n"
            "3. Get the call reference number.\n"
            "4. Thank the representative and end the call politely.\n"
            "The billing team will handle this denial manually using the captured reason."
        )

    reason = get_reason(reason_key)
    history = _denial_phase_history(call_state)

    if reason is not None:
        reason_line = (
            f"Denial reason (as understood so far): {reason.display_name} "
            f"[{reason.group_code} {'/'.join(reason.carc_codes)}]"
        )
        middle = reason.questions
    else:
        reason_line = (
            "Denial reason: NOT YET IDENTIFIED — your first priority is to ask the "
            "representative why the claim was denied."
        )
        middle = GENERIC_QUESTIONS

    questions = tuple(UNIVERSAL_QUESTIONS) + tuple(middle) + tuple(CLOSING_QUESTIONS)

    lines = [reason_line, "", "Questions to work through (ask only what's still open, ONE per turn):"]
    for i, q in enumerate(questions, 1):
        status = _question_status(q, history)
        lines.append(f"{i}. [{status}] {q.text}")
    lines.append("")
    lines.append(
        "A question marked [ANSWERED] is done — do not re-ask it. [ASKED] means you "
        "asked but may not have a usable answer yet — check the conversation history. "
        "The history below is the ground truth; these markers are hints."
    )
    lines.append(
        "NOTE: The call-reference number is NOT in this list on purpose — it is "
        "captured automatically from the transcript. Never ask the rep/IVR to read "
        "or repeat a reference/claim/fax number; stay silent while numbers are read."
    )
    return "\n".join(lines)
