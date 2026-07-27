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
    # provider_name is the ACTUAL provider (the doctor, e.g. "Irene Uke").
    provider_name = visit_data.get("provider_name") or os.getenv(
        "DENIAL_TEST_PROVIDER_NAME", "the provider on file"
    )
    # practice_name is the practice / group / facility (e.g. "ALPHA MENTAL
    # HEALTH SERVICES"). Optional — only used if the rep asks for the practice.
    practice_name = visit_data.get("practice_name") or os.getenv("DENIAL_TEST_PRACTICE_NAME", "")
    # NPIs: {npi} is the provider's individual NPI (also used in identification);
    # group_npi is the practice/billing NPI. Reps may ask for either.
    group_npi = visit_data.get("group_npi") or ""
    # provider_address — Cigna advocates ask for it during verification. Optional
    # for other payers (their templates don't reference {provider_address}, so
    # this extra key is harmless there — str.format ignores unused kwargs).
    provider_address = visit_data.get("provider_address") or os.getenv("DENIAL_TEST_PROVIDER_ADDRESS", "")
    return {
        **visit_data,
        "billed_amount": visit_data.get("billed_amount")
        or os.getenv("DENIAL_TEST_BILLED_AMOUNT", "not available"),
        "provider_name": provider_name,
        # Sentinel the prompt is told to NEVER read aloud — it defers instead of
        # saying "the practice is not provided".
        "practice_name": practice_name or "__UNKNOWN__",
        "group_npi": group_npi or "__UNKNOWN__",
        "provider_address": provider_address or "__UNKNOWN__",
        # Defaults are the real values, so these work WITHOUT any env vars set.
        # (env can still override per-deployment, but is not required.)
        "agent_persona_name": os.getenv("DENIAL_AGENT_PERSONA_NAME", "Miranda"),
        "agent_persona_last_name": os.getenv("DENIAL_AGENT_PERSONA_LAST_NAME", "Bell"),
        "callback_number": os.getenv("DENIAL_CALLBACK_NUMBER", "469-581-2936"),
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


# Rep stalls / non-answers — the rep is still working, NOT answering. These
# must not mark a question as ANSWERED.
_NON_ANSWER_PHRASES = (
    "bear with me",
    "let me check",
    "let me look",
    "let me pull",
    "let me see",
    "one moment",
    "just a moment",
    "give me a",
    "hold on",
    "please wait",
    "let me verify",
    "i do not have",
    "i don't have",
    "not sure",
)


def _is_non_answer(text: str) -> bool:
    t = text.strip().lower()
    if len(t) < 15:
        return True
    return any(p in t for p in _NON_ANSWER_PHRASES)


def _question_status(question, history) -> str:
    """OPEN / ASKED / ANSWERED for one question, from the denial-phase history.

    ASKED    → one of our say-lines contains a question keyword.
    ANSWERED → a REAL answer came after that ask (substantive AND not a stall
               like "bear with me" / "let me check" — those keep it ASKED).
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
        if role == "rep" and not _is_non_answer(text):
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

    # Reason-specific questions FIRST (the point of the call); the ICN is
    # lower priority (usually already captured from the readout).
    questions = tuple(middle) + tuple(UNIVERSAL_QUESTIONS) + tuple(CLOSING_QUESTIONS)

    lines = [reason_line, "", "Information you must OBTAIN for this denial, in priority order (ask ONE per turn, in order, starting from #1):"]
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q.text}")
    lines.append("")
    lines.append(
        f"🚨 COVERAGE — you must work through ALL {len(questions)} questions above before the "
        "call ends. Each turn: look at the Conversation So Far, find the LOWEST-numbered "
        "question you have NOT yet asked (and the rep did not already volunteer), and ask "
        "exactly that one — ONE per turn. Skip a question ONLY if the rep already gave that "
        "exact answer, or it's a conditional follow-up that clearly doesn't apply (e.g. a "
        "'if plan-specific' item when it's a universal exclusion). Do NOT jump ahead, do NOT "
        "re-ask something already answered, and do NOT end the call while any question is "
        "still unasked."
    )
    lines.append("")
    lines.append(
        "🚨 YOU must judge, from the Conversation So Far below, whether you have "
        "ACTUALLY obtained each item — do NOT assume. An item counts as obtained ONLY "
        "if the rep stated the real value (the primary carrier's NAME, a fax NUMBER, a "
        "specific date). A RELATED remark is NOT the value:"
    )
    lines.append(
        "   • \"we are secondary\" / \"there is a primary on file\" CONFIRMS coordination "
        "of benefits but is NOT the primary carrier's name — you still need to ask: "
        "\"Then who is the primary carrier on file?\""
    )
    lines.append(
        "   • A stall (\"let me check\", \"bear with me\") is NOT a value — wait for the real one."
    )
    lines.append(
        "Do NOT say \"that's all I needed\" or move to end the call while any item above is "
        "still missing its real value. If the rep says they genuinely cannot provide a "
        "required item, acknowledge that specific gap (\"understood, you don't have the "
        "primary carrier on file\") — never pretend you obtained it."
    )
    lines.append(
        "The call-reference number is NOT in this list — it's captured automatically from "
        "the transcript. Never ask the rep to read or repeat a reference/claim/fax number."
    )
    return "\n".join(lines)
