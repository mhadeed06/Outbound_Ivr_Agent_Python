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


# STRONG denial cues: "denied" used as an actual outcome on the claim/line.
# When any of these appear (and no negation), the denial is certain — we
# pivot WITHOUT asking GPT, so a GPT hallucination can never veto a real
# denial. Missing a denial (billing thinks it's paid) is far worse than an
# unnecessary rep call, so we bias hard toward pivoting on clear signals.
_STRONG_DENIAL_CUES = (
    "was denied",
    "were denied",
    "been denied",
    "is denied",
    "are denied",
    "got denied",
    "claim denied",
    "claim was denied",
    "line was denied",
    "lines were denied",
    "line item was denied",
    "denied because",
    "denied due to",
    "denied for",
    "denied as",
    "denial of the claim",
)

# WEAK cues: a denial MIGHT be present but the phrasing is ambiguous
# (conditional "if ... denied", a noun "denial" in passing, "not payable").
# These only flag a candidate; the pivot then asks GPT to confirm.
_WEAK_DENIAL_CUES = (
    "denied",
    "denial",
    "not payable",
    "no payment was made",
    "payment was not made",
    "not covered",
)

# Negations that void any positive cue in the same chunk.
_NEGATION_CUES = (
    "not denied",
    "wasnt denied",
    "wasn't denied",
    "no denial",
    "not a denial",
    "rather than denied",
    "instead of denied",
)

# Conditional / hypothetical phrasings — "IF the claim is denied", "may be
# denied", etc. These contain a strong cue but describe a possibility, not a
# fact (same trap as the old is_claim_start "the first claim" false positive).
# When present, downgrade STRONG → WEAK so GPT adjudicates instead of pivoting
# outright.
_CONDITIONAL_MARKERS = (
    "if this claim is denied",
    "if the claim is denied",
    "if it is denied",
    "if any claim is denied",
    "if denied",
    "may be denied",
    "could be denied",
    "might be denied",
    "should the claim be denied",
    "in case the claim is denied",
    "were it denied",
)


def _normalize(text: str) -> str:
    return " ".join(text.replace("’", "'").lower().split())


def denial_signal(text: str) -> str:
    """Classify the denial signal in a piece of payer speech.

    Returns "strong" (certain denial → pivot without GPT), "weak" (maybe →
    GPT confirms), or "none".
    """
    if not text:
        return "none"
    t = _normalize(text)
    if any(neg in t for neg in _NEGATION_CUES):
        return "none"
    conditional = any(m in t for m in _CONDITIONAL_MARKERS)
    if not conditional and any(cue in t for cue in _STRONG_DENIAL_CUES):
        return "strong"
    if conditional or any(cue in t for cue in _WEAK_DENIAL_CUES):
        return "weak"
    return "none"


def mentions_denial(text: str) -> bool:
    """Cheap rule check: does this claim-readout chunk mention a denial
    (strong OR weak)? Used to raise the candidate flag during the readout."""
    return denial_signal(text) in ("strong", "weak")


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
