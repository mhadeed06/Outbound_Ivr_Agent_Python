CLAIM_NOT_FOUND_TRIGGERS = [
    # Short, high-recall substrings — variations on "no claims found" that
    # survive STT rewording. Keep them lowercased and use ONLY straight
    # ASCII apostrophes; _normalize_for_match() folds Unicode variants below.
    "couldn't find any claims",
    "could not find any claims",
    "didn't find any claims",
    "did not find any claims",
    "not find any claims",
    "no claims found",
    "no matching claims",
    "no claim was found",
    "there are no claims on that date",
    "no claims on that date",
    "no claims for that date",
    "no claims for this date of service",
    "not seeing any claims",
    "we don't have any claims on file",
    "we do not have any claims on file",
]


def _normalize_for_match(text: str) -> str:
    """Fold curly apostrophes → straight and collapse whitespace so triggers
    match across STT and prompt rewordings. Cheap: one lower() + one replace()
    + a split/join."""
    return " ".join(text.lower().replace("’", "'").split())


def is_claim_not_found(text: str) -> bool:
    n = _normalize_for_match(text)
    return any(phrase in n for phrase in CLAIM_NOT_FOUND_TRIGGERS)
#### Claims helper functions 
# --- Claim-capture triggers (keep tight & cheap) ---
CLAIM_START_TRIGGERS = [
    "i found your claim",
    "i found a claim",
    "i found two claims",
    "i found three claims",
    "i found four claims",
    "i found five claims",
    "here's the first one",
    "here is the first one",
    "the first one was for service",
    # NOTE: "the first claim" was removed because it caused false positives.
    # Example: CIGNA IVR said "if this is the first claim you filed with this
    # tax ID..." while REJECTING the tax ID — the substring match fired
    # claim_mode incorrectly, causing the call to be logged as a "successful
    # unknown" claim rather than a failed verification.
    "please wait for the silence while we locate your claim",
    "We found the requested claim",
    "there is one claim for this date of service",
]

def is_claim_start(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in CLAIM_START_TRIGGERS)

