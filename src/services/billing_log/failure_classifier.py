"""
Failure-case classifier — determines WHY a call didn't reach a claim outcome,
so the frontend still gets an actionable status update at end of call.

Runs when we don't have finalized_claims and the cleanup reason isn't
"claims: not found" (the NOT ON FILE case is handled separately).

Two-tier approach:
  1. Rule-based (fast, free, deterministic): keyword + cleanup_reason match.
  2. GPT fallback (slower, costs a call): only when rules can't decide.

Returns:
  {"status": "patient not found" | "call failed", "description": "<plain-english>"}
"""
import json
import logging

from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)


_ALLOWED = {"patient not found", "call failed"}

# ── Rule-based patterns ────────────────────────────────────────────────────
# Substrings that indicate the IVR could not verify the patient. Matched
# case-insensitively. If ANY match on the transcript, we classify as
# "patient not found" without asking GPT.
#
# Covers verification failures for ANY identifier the IVR checks:
# tax_id, member_id, DOB, member name, NPI, or generic "we can't find you".
# Any of these means the same thing to the billing team: the patient could
# not be located in the payer's system with the info we provided.
_PATIENT_NOT_FOUND_PATTERNS = (
    # Generic "not found" / "not in records" — covers most cases
    "not found in our records",
    "not found in records",
    "not on file",
    "no record found",
    "no record on file",
    "we do not have a record",
    "we don't have a record",
    "we're unable to locate",
    "we are unable to locate",
    "unable to locate the member",
    "unable to locate this member",
    "unable to find",
    "could not be located",
    "could not be found",

    # Member ID rejections
    "we couldn't find the member",
    "we could not find the member",
    "we couldn't find that member",
    "we could not find that member",
    "member not found",
    "member id is not valid",
    "member id is invalid",
    "invalid member id",
    "member id you entered is not",
    "cannot verify the member",
    "we can't verify the member",

    # Tax ID rejections
    "tax id is not valid",
    "tax id is invalid",
    "invalid tax id",
    "please enter a different tax id",
    "tax id was not found",
    "tax id not on file",

    # NPI rejections
    "npi is not valid",
    "npi is invalid",
    "invalid npi",
    "npi was not found",
    "npi not on file",
    "please enter a different npi",

    # DOB mismatch (both apostrophe and STT-normalized variants)
    "date of birth does not match",
    "date of birth doesn't match",
    "date of birth doesnt match",
    "dob does not match",
    "dob doesn't match",
    "dob doesnt match",
    "the date of birth you entered does not",
    "the date of birth is not correct",

    # Name mismatch (both apostrophe and STT-normalized variants)
    "name does not match",
    "name doesn't match",
    "name doesnt match",
    "member name does not match",
    "member name doesn't match",
    "member name doesnt match",
    "the name you provided does not",

    # Provider not recognized
    "provider is not recognized",
    "provider is not on file",
    "we don't recognize that provider",
    "we do not recognize that provider",
)

# Cleanup reasons that are inherently non-verification failures — the call
# ran but was cut short. Always classified as "call failed".
_CALL_TIMEOUT_REASONS = {"auto_hangup"}
_SYSTEM_SHUTDOWN_REASONS = {"shutdown"}


def _identifier_hint_from_pattern(pattern: str) -> str:
    """Given a matched pattern, guess which identifier the IVR rejected —
    used to make the failure description more actionable for the billing team.
    Returns a string like " (tax ID rejected)" or "" if the pattern is generic.
    """
    p = pattern.lower()
    if "tax id" in p:
        return " (tax ID appears to be the rejected identifier)"
    if "member id" in p:
        return " (member ID appears to be the rejected identifier)"
    if "npi" in p:
        return " (NPI appears to be the rejected identifier)"
    if "date of birth" in p or "dob" in p:
        return " (date of birth appears to be the rejected identifier)"
    if "name" in p:
        return " (member name appears to be the rejected identifier)"
    if "provider" in p:
        return " (provider information appears to be the rejected identifier)"
    return ""  # generic "not found" — no specific hint


def _rule_based_classify(transcript: str, cleanup_reason: str, call_tag: str) -> dict | None:
    """Try to classify without hitting GPT. Returns None if no rule matches."""
    reason = (cleanup_reason or "").lower()
    text = (transcript or "").lower()

    # Priority 1: explicit patient-verification failure keywords in transcript.
    for pattern in _PATIENT_NOT_FOUND_PATTERNS:
        if pattern in text:
            logger.info(f"🔍 Rule-based failure: 'patient not found' (matched {pattern!r})")
            hint = _identifier_hint_from_pattern(pattern)
            return {
                "status": "patient not found",
                "description": (
                    f"The IVR could not verify the patient's identity{hint}. "
                    f"Trigger phrase in transcript: {pattern!r}. "
                    f"Recommendation: verify tax ID, NPI, member ID, member name, "
                    f"and date of birth against what the payer has on file. "
                    f"call_id={call_tag}"
                ),
            }

    # Priority 2: auto_hangup — call ran too long
    if reason in _CALL_TIMEOUT_REASONS:
        logger.info("🔍 Rule-based failure: 'call failed' (auto_hangup)")
        return {
            "status": "call failed",
            "description": (
                f"The call exceeded the maximum duration and was automatically "
                f"terminated before a claim outcome could be determined. This "
                f"usually happens when the IVR gets stuck in a loop or waits "
                f"too long between prompts. call_id={call_tag}"
            ),
        }

    # Priority 3: system shutdown
    if reason in _SYSTEM_SHUTDOWN_REASONS:
        logger.info("🔍 Rule-based failure: 'call failed' (shutdown)")
        return {
            "status": "call failed",
            "description": (
                f"The call was interrupted by a system shutdown before a claim "
                f"outcome could be determined. call_id={call_tag}"
            ),
        }

    # No rule fired — fall through to GPT.
    return None


# ── GPT fallback ──────────────────────────────────────────────────────────
_GPT_PROMPT = """You are analyzing a failed insurance IVR call that did NOT reach a claim outcome.

Determine WHY the call failed. Return JSON with exactly two keys:
  "status"      — one of: "patient not found" | "call failed"
  "description" — 2-4 short sentences describing what happened, actionable for a
                  medical billing team member. Include specific quotes from the
                  transcript when they help explain the failure.

Status meaning — pick EXACTLY ONE:
- "patient not found": the payer's IVR could not verify the patient/provider.
  Signals: the IVR said ANY of these were not recognized, invalid, or not on
  file — tax ID, NPI, member ID, patient name, date of birth, or provider
  info — and asked us to enter it again, or transferred us because
  verification failed. When you see this, IN THE DESCRIPTION mention which
  identifier appeared to be the problem (tax ID / member ID / DOB / etc).
- "call failed": everything else. The call ended before a claim outcome for
  any other reason — routed to a live agent, IVR error, technical failure,
  ended prematurely, or the outcome is unclear.

Rules:
- Base your decision on what the transcript ACTUALLY says. Don't invent details.
- If unsure, use "call failed".
- Return ONLY the JSON object. No markdown, no extra text.

Cleanup reason (why our end of the call terminated): {cleanup_reason}
Call ID (include in the description for grep-ability): {call_tag}

Transcript:
---
{transcript}
---"""


async def _gpt_classify(transcript: str, cleanup_reason: str, call_tag: str) -> dict:
    """Fallback GPT call for when rules couldn't decide."""
    # Cap transcript length to keep the token cost bounded. The tail of the
    # transcript is where the failure signal usually lives (the IVR's last
    # utterance before we gave up).
    tail = transcript[-3000:] if len(transcript) > 3000 else transcript
    prompt = _GPT_PROMPT.format(
        transcript=tail,
        cleanup_reason=cleanup_reason or "(unknown)",
        call_tag=call_tag or "(unknown)",
    )

    try:
        raw = await _call_gpt_api(prompt, max_tokens=300)
    except Exception as e:
        logger.exception(f"Failure classifier GPT call failed: {e}")
        return {
            "status": "call failed",
            "description": (
                f"The call did not complete successfully and the reason could not "
                f"be automatically determined (classifier error). call_id={call_tag}"
            ),
        }

    if not raw:
        return {
            "status": "call failed",
            "description": (
                f"The call did not complete successfully and the classifier "
                f"returned no result. call_id={call_tag}"
            ),
        }

    status, description = _parse(raw)
    logger.info(f"🤖 GPT failure classified: status={status!r}")
    return {"status": status, "description": description}


def _parse(raw: str) -> tuple[str, str]:
    """Parse GPT output into (status, description). Tolerant of stray text."""
    text = raw.strip()

    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    try:
        obj = json.loads(text)
        status = str(obj.get("status", "")).strip().lower().strip(".,!? \"'")
        description = str(obj.get("description", "")).strip()
        if status not in _ALLOWED:
            status = "call failed"
        return status, description
    except Exception:
        logger.warning(f"Could not parse failure classification {raw!r}; defaulting to call failed")
        return "call failed", ""


# ── Public API ────────────────────────────────────────────────────────────
async def classify_failure(
    transcript: str,
    cleanup_reason: str,
    call_tag: str,
) -> dict:
    """Determine why the call failed. Never raises.

    Returns:
        {"status": "patient not found" | "call failed",
         "description": "<plain-english explanation with call_id>"}
    """
    # Try rules first — cheap and deterministic.
    ruled = _rule_based_classify(transcript, cleanup_reason, call_tag)
    if ruled is not None:
        return ruled

    # No rule matched — GPT decides.
    return await _gpt_classify(transcript, cleanup_reason, call_tag)


def build_plain_transcript(full_transcript_entries: list[dict]) -> str:
    """Convert the CallState.full_transcript list-of-dicts into plain text
    for the failure classifier prompt.

    Shape of each entry: {"speaker": "ivr" | "agent" | ..., "text": "..."}
    """
    lines: list[str] = []
    for entry in full_transcript_entries or []:
        speaker = (entry.get("speaker") or "").lower()
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        if speaker == "ivr":
            prefix = "IVR"
        elif speaker == "agent":
            prefix = "AGENT"
        else:
            prefix = speaker.upper() if speaker else "?"
        lines.append(f"{prefix}: {text}")
    return "\n".join(lines)
