"""
Phase-transition detection for the denial follow-up flow.

After the pivot, the call has two conversational phases:
  - "denial_ivr" → still navigating the IVR, goal is to reach a representative
  - "denial_rep" → free-form conversation with a live human

The IVR signals an upcoming transfer with phrases like "transferring you now"
or "your estimated wait time is...". This module is the single source of
truth for detecting that transition; main.py consults it each turn while
phase == "denial_ivr". A GPT-driven `rep_mode` safety net in the denial-IVR
prompt covers the cases where the rep picks up cold without any of these
phrases (mirrors the claim_mode safety-net pattern).

Kept deliberately simple — substring matching on the normalized transcript.
Add phrases here as we observe real IVR variations payer by payer.
"""
import logging

logger = logging.getLogger(__name__)


# Phrases that signal the IVR is about to (or has just) transferred us to a
# live representative. Matched case-insensitively as substrings.
# Several of these come verbatim from real Humana call transcripts —
# e.g. "Okay, you will be transferred in a moment. ... I'm transferring you
# now." (2026-07 docs-required denial call).
_TRANSFER_PHRASES = (
    "transferring you now",
    "i am transferring you",
    "im transferring you",          # STT sometimes drops the apostrophe
    "transferring your call",
    "transferred in a moment",      # real Humana phrasing
    "will be transferred",
    "being transferred",
    "transfer you now",
    "transfer you to a representative",
    "connect you with a representative",
    "connect you to a representative",
    "connecting you now",
    "putting you through",
    "estimated wait time",
    "we are sorry to keep you waiting",
    "appreciate your patience",
    "please hold while",
    "please stay on the line",
    # Cigna reaches a human via a "customer service advocate" — same transfer
    # intent, different noun. (Refine with real Cigna transcripts.)
    "connect you to an advocate",
    "connect you with an advocate",
    "transfer you to an advocate",
    "connect you to customer service",
    "transfer you to customer service",
)


def _normalize(text: str) -> str:
    # Fold curly apostrophes and collapse whitespace — same discipline as
    # claims_helpers._normalize_for_match (STT output varies).
    return " ".join(text.replace("’", "'").lower().split())


def is_transfer_signal(transcript: str) -> bool:
    """True if the IVR transcript indicates we are being / have been
    transferred to a live representative.

    Once True, the caller flips call_state.phase = "denial_rep" so subsequent
    turns use the rep prompt (and looser rep-phase speech timings).
    """
    if not transcript:
        return False
    text = _normalize(transcript)
    for phrase in _TRANSFER_PHRASES:
        if phrase in text:
            logger.info(f"🔀 Transfer phrase detected: {phrase!r}")
            return True
    return False
