"""
GPT-based classifier that collapses one or more claim transcripts into a
final status AND a short human-readable description of the LAST claim.

When multiple claims are present, the prompt instructs GPT to emphasize
the LAST claim — earlier claims are context only.
"""
import json
import logging

from src.services.llm.llm_service import _call_gpt_api

logger = logging.getLogger(__name__)

_ALLOWED = {"paid", "denied", "inprocess", "unknown"}

_PROMPT = """You are a medical insurance claim summarizer for a billing team.

You will be given one or more claim transcripts captured from an IVR call.
Return a JSON object with exactly two keys:
  "status"      — one of: paid, denied, inprocess, unknown
  "description" — a clear, plain-English summary of the claim that a biller can
                  understand at a glance WITHOUT reading the full transcript.

Writing the description:
- Describe the claim directly. Do NOT call it "the last claim" or refer to its
  position/order — just summarize the claim itself.
- Write 2-5 short sentences in natural language; explain the numbers, don't just
  list them.
- Cover ONLY what the transcript states. Depending on the insurer, that may
  include: date(s) of service, billed amount, the outcome and the reason if
  given, how much the plan paid, the patient's responsibility, deductible,
  copay, not-covered amounts + reasons, and check / remittance numbers.
- A claim can have several "claim line details" (line items) — these ARE part
  of the claim. Include the relevant line-level details; if there are many,
  summarize them concisely rather than dropping them.

CRITICAL — never invent or assume anything:
- Different insurers format claims differently, so some fields will be absent.
- Only state values that actually appear in the transcript. If a value is not
  mentioned, leave it out — never guess a number, date, amount, or reason.

Status meaning — decide ONLY from the LAST claim, and pick EXACTLY ONE by going
through these IN ORDER. Use the FIRST one that applies and stop:

1. paid       → an actual payment was made to the provider. Evidence: "the plan
                paid $X", "the provider was paid $X", "we paid $X", or a check /
                EFT / remittance amount greater than $0. Any real payment (even
                partial) counts here and takes priority over everything below.
2. inprocess  → NO payment yet AND no final outcome: the claim is pending, in
                review, still processing, or has been forwarded / resubmitted /
                sent to another payer or address for reprocessing.
3. denied     → NO payment AND a FINAL negative outcome with nothing further:
                the claim was denied or rejected, or the billed amount was fully
                not covered and no further action will be taken.
4. unknown    → none of the above clearly applies, or the outcome isn't stated.

These four are mutually exclusive — exactly one applies. Tie-breakers:
- "processed" only means adjudication happened; it is NOT a payment. Never treat
  "processed" as paid on its own.
- "no payment was made" rules out paid → it is inprocess or denied.
- "not covered" + forwarded / resubmitted / reprocessing → inprocess (NOT denied).
- "not covered" + final, nothing more will happen → denied.
- If you cannot place it in 1–3 with confidence, use unknown.

Rules:
- If multiple claims are present, base BOTH fields on the LAST claim. Earlier
  claims are context only.
- Do NOT invent anything. Only summarize what the transcript actually says.
- If the status is unclear, use "unknown".
- Return ONLY the JSON object. No markdown, no extra text.

Claim transcripts (in chronological order; the LAST one is the answer):
---
{claims_text}
---"""


async def classify_claim(finalized_claims: list[str]) -> dict:
    """
    Return {"status": <paid|denied|inprocess|unknown>, "description": <str>}.
    Never raises — on any error, returns status="unknown", description="".
    """
    if not finalized_claims:
        return {"status": "unknown", "description": ""}

    claims_text = "\n\n--- CLAIM BREAK ---\n\n".join(finalized_claims)
    prompt = _PROMPT.format(claims_text=claims_text)

    try:
        # Room for a JSON object + a multi-sentence plain-English summary.
        raw = await _call_gpt_api(prompt, max_tokens=400)
    except Exception as e:
        logger.exception(f"Claim classification GPT call failed: {e}")
        return {"status": "unknown", "description": ""}

    if not raw:
        return {"status": "unknown", "description": ""}

    status, description = _parse(raw)
    logger.info(f"📊 Claim classified: status={status} description={description!r}")
    return {"status": status, "description": description}


def _parse(raw: str) -> tuple[str, str]:
    """Parse GPT output into (status, description). Tolerant of stray text /
    markdown fences around the JSON."""
    text = raw.strip()

    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # Try to isolate the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    try:
        obj = json.loads(text)
        status = str(obj.get("status", "")).strip().lower().strip(".,!? \"'")
        description = str(obj.get("description", "")).strip()
        if status not in _ALLOWED:
            status = "unknown"
        return status, description
    except Exception:
        # Fallback: maybe GPT returned just a bare word
        word = raw.strip().lower().strip(".,!? \"'")
        if word in _ALLOWED:
            return word, ""
        logger.warning(f"Could not parse claim classification {raw!r}; defaulting to unknown")
        return "unknown", ""