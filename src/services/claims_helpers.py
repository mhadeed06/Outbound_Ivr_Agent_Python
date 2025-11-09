CLAIM_NOT_FOUND_TRIGGERS = [
    "i couldn't find any claims",
    "i did not find any claims",
    "I didn't find any claims on that date",
    "I did not find any claims on that date",
    "no claims found",
    "there are no claims on that date",
    "no matching claims",
    "i’m not seeing any claims for that",
]
def is_claim_not_found(text: str) -> bool:
    return any(phrase in text.lower() for phrase in CLAIM_NOT_FOUND_TRIGGERS)
#### Claims helper functions 
# --- Claim-capture triggers (keep tight & cheap) ---
CLAIM_START_TRIGGERS = [
    "i found your claim",
    "i found a claim",
    "i found two claims",
    "i found three claims",
    "i found four claims",
    "i found five claims",
    "i found six claims",
    "i found seven claims",
    "i found eight claims",
    "i found nine claims",
    "here's the first one",
    "here is the first one",
    "the first one was for service",
    "the first claim",
    "there is one claim for this date of service",
]

def is_claim_start(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in CLAIM_START_TRIGGERS)

