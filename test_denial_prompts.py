"""
Test harness for the denial follow-up prompts (IVR phase + rep phase).

Feeds real utterances — most taken verbatim from the 2026-07 Humana
docs-required call transcript — through the ACTUAL prompt-building path
(templates + knowledge sheet + denial_context_block + history) and checks
GPT's response format/content. Lets us iterate on the prompts without
spending live calls.

Run:
    python test_denial_prompts.py

Requires PTU_API_KEY / PTU_AZURE_ENDPOINT / PTU_API_VERSION in .env — same
env the prod app uses. Follows the test_cigna_prompt.py conventions
(gitignored, GPT-driven, tuple-style cases).
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

os.environ.setdefault("DENIAL_TEST_BILLED_AMOUNT", "4437.72")
os.environ.setdefault("DENIAL_TEST_PROVIDER_NAME", "KATRANJI HAND CENTER")

from src.config.insurance_config import set_active_insurance, INSURANCE_CONFIGS
from src.core.prompts.manager import get_denial_prompt_template
from src.core.denials.context import build_denial_format_kwargs, render_denial_context
from src.services.llm.llm_service import _call_gpt_api
from main import _format_denial_history  # real history renderer

set_active_insurance(INSURANCE_CONFIGS["HUMANA"])

VISIT_DATA = {
    "tax_id": "830510206",
    "npi": "1952408932",
    "member_id": "49693275",
    "member_name": "Thomas Gooden",
    "dob": "08/16/1954",
    "dos": "04/27/2026",
}

# The real rep utterance that states the docs-required denial reason.
REP_REASON_LINE = (
    "Okay, and as i check here claim received on 05222026 and it was last processed on "
    "06/29/2026 claim number is 82026142021399 and upon checking here the claim is partially "
    "paid, and you Humana allow for $822.40 Humana paid on the same amount and the member's "
    "responsibility. So, regarding here on the procedure code for the line-item number two got "
    "denied because the documentation submitted does not meet the code criteria."
)

GOAL_MET_HISTORY = [
    {"transcript": REP_REASON_LINE,
     "gpt_result": "say:Could I get the ICN number for this claim, please?"},
    {"transcript": "Sure, the claim number is 82026142021399.",
     "gpt_result": "say:Got it. Could you tell me which documentation is required for this claim?"},
    {"transcript": "Medical documentation that supports CPT 26116 per the code criteria.",
     "gpt_result": "say:Could you provide the fax where we submit the documentation?"},
    {"transcript": "Yes, it is 866-305-6655.",
     "gpt_result": "say:What is the time limit for submitting the documentation?"},
    {"transcript": "You have 120 days from the denial date.",
     "gpt_result": "say:May I get the call reference number for our conversation?"},
    {"transcript": "That is 200533377471.",
     "gpt_result": "say:Got it, thank you."},
]


@dataclass
class Case:
    name: str
    phase: str                       # "ivr" | "representative"
    transcript: str
    accept: tuple                    # acceptable response prefixes (lowercase)
    must_contain_any: tuple = ()     # at least one must appear (lowercase)
    reason_key: str | None = None
    history: list = field(default_factory=list)


class FakeCallState:
    def __init__(self, case: Case):
        self.phase = "denial_rep" if case.phase == "representative" else "denial_ivr"
        self.denial_reason_key = case.reason_key
        self.denial_reason_verbatim = (
            "documentation submitted does not meet the code criteria"
            if case.reason_key == "docs_required" else None
        )
        self.denial_history_start = 0
        self.conversation_history = case.history
        self.visit_data = VISIT_DATA


CASES: list[Case] = [
    # ── IVR phase (goal: reach a representative) — real Humana lines ───────
    Case("transfer confirm", "ivr",
         "Would you like to transfer to customer service?",
         accept=("confirm:yes", "confirm: yes", "say:yes")),
    Case("routing question -> Claims", "ivr",
         "Okay, so I can get you to the right area correctly. What would you like help with?",
         accept=("say:",), must_contain_any=("claims",)),
    Case("fax offer -> No", "ivr",
         "Would you also like a fax?",
         accept=("say:no", "say: no")),
    Case("reference number readout -> silent/No", "ivr",
         "Please make a note of the following reference number for future use. "
         "Your call reference number is 2000533377243 Would you like me to repeat that?",
         accept=("fallback", "say:no", "say: no")),
    Case("hold message -> silent", "ivr",
         "We are sorry to keep you waiting. Your call is important to us.",
         accept=("fallback",)),
    Case("human picks up -> rep_mode", "ivr",
         "Thank you for calling Humana, my name is Sarah. How can I help you today?",
         accept=("rep_mode", "rep mode")),

    # ── Rep phase — real (garbled) lines from the transcript ────────────────
    Case("garbled greeting asking callback", "representative",
         "Thank you for calling you, and this is Rob's snail Okay, it was assisting for your "
         "claim, and can you help me please. call back number and extension if there is any",
         accept=("say:",), must_contain_any=("469",),
         reason_key="docs_required"),
    Case("rep asks DOS + billed amount", "representative",
         "Sure, and from able to check the claim, can I have the data service, and the billed amount.",
         accept=("say:",), must_contain_any=("4437", "4,437"),
         reason_key="docs_required",
         history=[{"transcript": "can you help me please. call back number if there is any",
                   "gpt_result": "say:The callback number is 469-581-2969, and this is a direct line."}]),
    Case("reason stated -> ask a checklist question", "representative",
         REP_REASON_LINE,
         accept=("say:",),
         must_contain_any=("icn", "claim number", "criteria", "documentation", "document"),
         reason_key="docs_required",
         history=[{"transcript": "how can I help you today?",
                   "gpt_result": "say:Hi, my name is Kevin. I am calling from the provider office. "
                                 "I have a claim denial to discuss."}]),
    Case("mid-number readout -> silent", "representative",
         "so for the reference number that is 200",
         accept=("fallback",),
         reason_key="docs_required", history=list(GOAL_MET_HISTORY[:4])),
    Case("anything else, goal met -> no thanks", "representative",
         "Is there anything else I can help you with?",
         accept=("say:",), must_contain_any=("no",),
         reason_key="docs_required", history=list(GOAL_MET_HISTORY)),
    Case("goodbye after goal met -> endcall", "representative",
         "Okay, thanks so much for calling, have a great day.",
         accept=("endcall",),
         reason_key="docs_required",
         history=list(GOAL_MET_HISTORY) + [
             {"transcript": "Is there anything else I can help you with?",
              "gpt_result": "say:No, that's all. Thank you so much."}]),
    Case("premature goodbye, goal NOT met -> push back", "representative",
         "Okay thank you for calling, have a great day, bye.",
         accept=("say:",), must_contain_any=("denied", "denial", "reason", "before"),
         reason_key=None,
         history=[{"transcript": "this is Rob, how can I help you",
                   "gpt_result": "say:Hi, my name is Kevin. I am calling from the provider office. "
                                 "I have a claim denial to discuss."}]),
]


def _build_prompt(case: Case) -> str:
    cs = FakeCallState(case)
    fmt = build_denial_format_kwargs(cs)
    if case.phase == "representative":
        template = get_denial_prompt_template("representative")
        return template.format(
            transcript=case.transcript,
            conversation_history=_format_denial_history(cs),
            denial_context_block=render_denial_context(cs),
            **fmt,
        )
    template = get_denial_prompt_template("ivr")
    return template.format(transcript=case.transcript, **fmt)


async def _run_case(case: Case) -> bool:
    raw = await _call_gpt_api(_build_prompt(case), max_tokens=60)
    low = (raw or "").strip().lower()
    compact = low.replace(" ", "")
    ok = any(compact.startswith(a.replace(" ", "")) for a in case.accept)
    if ok and case.must_contain_any:
        ok = any(m in low for m in case.must_contain_any)
    icon = "PASS" if ok else "FAIL"
    print(f"[{icon}] {case.name:<42} got={raw[:80]!r}")
    return ok


async def main() -> int:
    results = []
    for case in CASES:
        results.append(await _run_case(case))
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
