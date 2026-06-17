"""
Phase transition detection for denial_inquiry calls.

The flow has two phases:
  - "ivr"            → menu navigation, rule-based prompts
  - "representative" → free-form conversation with a human

The IVR signals an upcoming transfer with phrases like "transferring you now"
or "your estimated wait time is...". This module is the SINGLE source of truth
for detecting that transition; the routing in handle_user_speech consults it
each turn while phase == "ivr".

Kept deliberately simple — phrase matching on the IVR transcript. Add more
phrases here as we observe real IVR variations in the wild.
"""
import logging
import re

logger = logging.getLogger(__name__)


# Phrases that signal the IVR is about to (or has just) transferred us to a
# live representative. Matched case-insensitively as substrings.
# Tune this list as we observe new IVR variations during testing.
_TRANSFER_PHRASES = (
    "transferring you now",
    "i am transferring you",
    "transferring your call",
    "estimated wait time",
    "we are sorry to keep you waiting",
    "appreciate your patience",
)


def is_transfer_signal(transcript: str) -> bool:
    """True if the IVR transcript indicates we are being / have been transferred.

    Once True, the caller should set call_state.phase = "representative" so
    subsequent turns use the rep prompt instead of the IVR menu prompt.
    """
    if not transcript:
        return False
    text = transcript.lower()
    for phrase in _TRANSFER_PHRASES:
        if phrase in text:
            logger.info(f"🔀 Transfer phrase detected: {phrase!r}")
            return True
    return False
