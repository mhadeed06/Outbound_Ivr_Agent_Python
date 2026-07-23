"""
Live denial detection for the in-call pivot.

Two-tier, mirroring the failure_classifier pattern:
  1. `mentions_denial()` — free rule-based substring scan, run on every
     claim-readout chunk while in claim mode. Sets the cheap
     `denial_candidate` flag on CallState.
  2. `confirm_denial_via_gpt()` — ONE GPT yes/no check over the captured
     claim readout, run only at the pivot decision moment (when the claims
     controller is about to STOP/hang up and the candidate flag is set).
     Off the hot path: we're deciding whether to say "Representative"
     instead of hanging up, so one extra GPT round-trip is acceptable.
"""
import logging

from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)


# Positive denial cues — matched as substrings on the normalized chunk.
# STT variants included (dropped apostrophes etc. handled by _normalize).
_DENIAL_CUES = (
    "denied",
    "denial",
    "not payable",
    "no payment was made",
    "payment was not made",
)

# Negations that void a positive cue found in the same chunk. Checked
# AFTER a positive hit; deliberately conservative — a false candidate flag
# costs one GPT confirm at pivot time, a false negative loses the pivot.
_NEGATION_CUES = (
    "not denied",
    "wasnt denied",
    "wasn't denied",
    "no denial",
    "rather than denied",
)


def _normalize(text: str) -> str:
    return " ".join(text.replace("’", "'").lower().split())


def mentions_denial(text: str) -> bool:
    """Cheap rule check: does this claim-readout chunk mention a denial?"""
    if not text:
        return False
    t = _normalize(text)
    if not any(cue in t for cue in _DENIAL_CUES):
        return False
    if any(neg in t for neg in _NEGATION_CUES):
        return False
    return True


_CONFIRM_PROMPT = """An automated phone agent just listened to an insurance IVR read out claim \
information for a patient. Decide whether the FINAL claim mentioned in the readout was DENIED, \
fully OR partially.

Rules:
- Only the FINAL claim in the readout counts (earlier claims may have other statuses).
- "denied" / "denial" stated for the final claim → YES.
- PARTIAL denials count as YES: "some lines were denied and some lines were paid",
  "line item ... was denied", "partially paid" with any denied line → YES.
- "in process", "fully paid", "finalized" with no denied lines, "approved", "pending",
  "no claims found" → NO.
- If the readout is ambiguous or you cannot tell, answer NO.

Answer with exactly one word: YES or NO.

Claim readout transcript:
\"\"\"{claims_text}\"\"\"
"""


async def confirm_denial_via_gpt(claims_text: str) -> bool:
    """One GPT check at the pivot moment: is the final claim DENIED?

    Never raises — any error/ambiguity returns False so the call falls back
    to the normal hangup path (fail-safe: worst case we skip the pivot and
    the call reports DENIED exactly as today).
    """
    text = (claims_text or "").strip()
    if not text:
        return False
    # Cap the tail so the prompt stays small — the final claim's status is
    # by definition near the end of the readout.
    tail = text[-3000:]
    try:
        raw = await _call_gpt_api(_CONFIRM_PROMPT.format(claims_text=tail))
        verdict = (raw or "").strip().upper().rstrip(".,!")
        logger.info(f"🩺 Denial pivot GPT confirm → {verdict!r}")
        return verdict == "YES"
    except Exception as e:
        logger.error(f"Denial pivot GPT confirm failed (skipping pivot): {e}")
        return False
