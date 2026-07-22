# CLAUDE.md — Outbound IVR Claim-Status Agent

Onboarding guide for the next Claude session working on this repo. Reads what the code IS,
how the CLAIM STATUS FLOW works end-to-end, WHAT we've fixed and WHY, what's in each branch,
and how to safely add new features / insurers.

Keep this file up to date when you change something meaningful.

---

## 1. What this project is

An automated outbound IVR agent for medical claim status inquiries. Given a `visit_id` +
`customer_id`, it:

1. Looks up the visit's insurance from the PracticeEHR Clinical API (routed by `payerId`).
2. Places an outbound phone call to that payer's IVR line via Telnyx.
3. Streams the audio through Azure Speech-to-Text (STT).
4. Uses Azure OpenAI (GPT-4.1 PTU) to decide what to say/press at each IVR prompt.
5. Speaks responses back via Azure TTS.
6. Captures the claim(s) the payer reads out.
7. Classifies the final claim status → PAID / DENIED / IN PROCESS / UNKNOWN / NOT ON FILE /
   PATIENT NOT FOUND / CALL FAILED.
8. Uploads recording + transcript to PracticeEHR + writes the outcome to the
   Billing-Agent/Log DB, then PATCHes the Ivr/ClaimStatus endpoint so the frontend sees it.

Deployed to Azure App Service (Linux container): `as-ai-billing-agent-prod-cus` (Central US).

---

## 2. Tech stack

- **Runtime**: Python 3.10, FastAPI, uvicorn, port 5000
- **Telephony**: Telnyx Call Control v2 (webhooks + bidirectional WebSocket audio streaming)
- **STT**: Azure Cognitive Services Speech (real-time, μ-law input, sends `on_partial` /
  `on_final` callbacks)
- **TTS**: Azure Speech TTS, voice `en-US-JennyNeural`, SSML-driven (dates spoken naturally,
  alphanumeric IDs spelled character-by-character)
- **LLM**: Azure OpenAI (Azure deployment name `gpt-4.1`, temperature=0, PTU)
- **HTTP**: `httpx.AsyncClient` singleton with connection pooling (fixed SNAT exhaustion)
- **Observability**: Application Insights via OpenTelemetry
- **CI/CD**: Azure Pipelines (`azure-pipelines.yml`)

---

## 3. End-to-end claim-status flow

```
Frontend --POST /v1/Billing-Agent/Call {visit_id, customer_id, Bearer token}
     |
     v
orchestrate.py
  1. Resolve api_key   (PracticeEHR /v1/Clients/AuthClients)
  2. Fetch visit_data  (Clinical API /v1/Clinical/Billing-Agent/Visit/{id})
                       → returns payer_id + tax_id/npi/member_id/dob/dos/member_name
  3. Route insurance   (lookup_by_payer_id → CIGNA / HUMANA / BAYLOR_SCOTT / OSCAR / ...)
  4. Validate required visit fields for this insurer (required_fields.py)
  5. Reserve RefNo     (POST Billing-Agent/Log — reserves the row for two-phase update)
  6. Telnyx.dial()     (outbound call to payer's IVR phone number)
  7. Return {succeeded, message, refNo} to frontend

Telnyx --webhook 'call.answered'-> webhooks.py       (restore log context + insurance ContextVar)
Telnyx --WebSocket 'start'-------> stream.py         (start Azure STT session, restore context)
Telnyx --WebSocket 'media' (μ-law inbound audio, 20ms frames)
     |
     v
stt_service.py  → convert_mulaw_to_pcm → push_stream.write → Azure STT
                → STT callbacks (on_partial/on_final) run inside CAPTURED ContextVar snapshot
                  (loop.call_soon_threadsafe with context=ctx  — see fix note in section 12)
     |
     v
stream.py debounce (per-utterance debounce; final chunks accumulate until N seconds of silence)
     |
     v
main.py::handle_user_speech(text, call_control_id)
  a) is_claim_not_found(text)  → cleanup with reason "claims: not found"  (SUCCESS: NOT ON FILE)
  b) is_claim_start(text)      → flip claim_mode + bump debounce + start claims session
  c) In claim_mode → forward chunk to claims_controller (per-insurer prompt: DETAILS/NEXT/STOP/CONTINUE)
  d) Otherwise → format the insurer's main prompt template with visit_data + transcript
                 → GPT → parse response (say / value / dtmf / endcall / fallback / claim_mode)
     |
     v
Response parser (llm_service.py + main.py post-GPT):
  - dtmf:N            → Telnyx send_dtmf
  - say/value/confirm → TTS (value: substitutes authoritative member_id if hallucinated)
  - endcall           → ensure_call_cleanup (send_hangup=True)
  - fallback / unknown → silent
  - claim_mode (NEW)  → GPT fallback signal → same helper as is_claim_start (see section 12)

Call ends (webhook 'call.hangup' | WebSocket close | endcall | is_claim_not_found | auto_hangup):
     |
     v
call_cleanup.py::ensure_call_cleanup (idempotent, guarded by cleanup_lock)
  1. Cancel pending debounce task (prevents late STT KeyError — see section 12)
  2. End claims_agent session
  3. Stop Azure STT session
  4. Optional Telnyx hangup
  5. Snapshot call state (visit_id, transcript, finalized_claims, cleanup_reason, ...)
  6. Fire-and-forget upload_call_artifacts(snapshot)  (background task, strong-refd)
     |
     v
post_call_upload.py
  1. Wait 5s for Telnyx recording to finalize
  2. Fetch recording (.wav) from Telnyx, upload to PracticeEHR under billing/agent/claim/{cid}/{vid}
  3. Build transcript JSON, upload alongside
  4. Determine outcome:
       is_incomplete (auto_hangup/shutdown)           → FAILED + classify_failure()
       finalized_claims present                       → SUCCESS + classify_claim() (paid/denied/inprocess/unknown)
       cleanup_reason == "claims: not found"          → SUCCESS + "no claim" (→ NOT ON FILE)
       else                                           → classify_failure()  (safety net!)
                                                         - matches "no claim" patterns → SUCCESS
                                                         - matches "patient not found" → FAILED
                                                         - else GPT semantic fallback  → FAILED
  5. update_billing_log_row(RefNo, requestStatus, claimStatus, description, paths)
  6. post_ivr_claim_status (PATCH IVR/ClaimStatus with mapped Status enum)
```

Status vocabulary end-to-end:

| Internal            | Billing-Agent/Log `claimStatus` | Ivr/ClaimStatus `Status`     |
|---------------------|---------------------------------|------------------------------|
| paid                | paid                            | PAID                         |
| inprocess           | inprocess                       | IN PROCESS                   |
| denied              | denied                          | DENIED                       |
| unknown             | unknown                         | UNKNOWN                      |
| no claim            | no claim                        | NOT ON FILE                  |
| patient not found   | patient not found               | PATIENT NOT FOUND            |
| call failed         | call failed                     | CALL FAILED                  |

---

## 4. Project structure

```
main.py                                  — FastAPI app, handle_user_speech, wiring
src/
  api/v1/
    orchestrate.py                       — POST /v1/Billing-Agent/Call (start call)
    webhooks.py                          — Telnyx call.answered/initiated/hangup handlers
    stream.py                            — WebSocket /stream: audio streaming, debounce loop
  auth/jwt_auth.py                       — Bearer token verify (frontend JWT)
  config/insurance_config.py             — InsuranceConfig, INSURANCE_CONFIGS dict, ContextVar
  core/
    claims/
      claims_agent.py                    — claims session lifecycle
      claims_controller.py               — pumps chunks through claims_controller GPT prompt
      claims_helpers.py                  — is_claim_not_found / is_claim_start triggers
      claims_intent_mapper.py            — one-word intent → action
    prompts/
      manager.py                         — get_main_prompt_template() (per active insurer)
      claims_prompts.py                  — get_claims_controller_template()
      loader.py                          — reads .txt templates from disk
      templates/
        cigna/                           — main + claims_controller templates
        humana/
        baylor_scott/
        oscar/
        health_first/
  models/data_models.py                  — CallState dataclass, SimpleCallRequest
  services/
    azure/
      stt_service.py                     — AzureRealtimeSttService (see STT ContextVar fix)
      tts_service.py                     — Azure TTS + SSML routing (dates vs codes)
    billing_log/
      classifier.py                      — classify_claim (paid/denied/inprocess/unknown)
      failure_classifier.py              — classify_failure (no claim/patient not found/call failed)
      claim_status_client.py             — PATCH Ivr/ClaimStatus
      log_client.py                      — Billing-Agent/Log CRUD
    clinical/
      client.py                          — Clinical /v1/Clinical/Billing-Agent/Visit
      required_fields.py                 — per-insurer required visit_data fields
    llm/llm_service.py                   — _call_gpt_api + response parser + hallucination guard
    practice_ehr/
      post_call_upload.py                — end-of-call orchestration (see section 3)
      telnyx_recording.py                — Telnyx recording fetch
      transcript_builder.py              — build JSON transcript
      uploader.py                        — upload artifact to PracticeEHR
    practice_ehr_auth/client.py          — resolve per-customer api_key
    telnyx/client.py                     — Telnyx REST helpers (dial, send_dtmf, hangup)
    call_cleanup.py                      — ensure_call_cleanup (idempotent)
    call_lifecycle.py                    — hangup_call, auto_hangup
    http_client.py                       — singleton httpx.AsyncClient (Adan's SNAT fix)
  utils/
    logging_config.py                    — call_id/visit_id/customer_id ContextVars + filter
    transcript.py                        — append_ivr/append_agent helpers
test_cigna_prompt.py                     — GITIGNORED — prompt regression harness
```

---

## 5. Multi-insurance routing

**Key idea**: routing is by `payer_id` from the Clinical API, NOT by plan name. Plan names
in the billing DB change; payer IDs are stable.

- `INSURANCE_CONFIGS` in `src/config/insurance_config.py` — per-insurer timings + prompt names.
- `PAYER_ID_TO_INSURANCE` — maps payer_ids like `"62308"` → `"CIGNA"`.
- `set_active_insurance()` / `set_active_insurance_by_name()` — set the per-request
  `active_insurance` ContextVar. Every downstream `config_manager.get_*()` reads from it.
- The insurance context is set at ALL entry points: orchestrate (call creation), webhooks
  (each webhook), stream (WebSocket 'start' event). Also inside STT callbacks — see
  section 12 for the ContextVar propagation fix.

**Per-insurer knobs** (`InsuranceConfig`):
- `debounce_seconds` / `claim_debounce_seconds` — how long to wait after last STT final
  before processing the accumulated text. Higher = fewer chunks, more context per GPT call.
- `segmentation_silence_ms` / `claim_segmentation_silence_ms` — Azure STT boundary detection.
  Different for normal vs claim-listing phase (the claim phase needs longer silence
  detection because payers pause more between line items).
- `claims_tail_chars` — how much of the accumulated claim text to send in each
  claims_controller GPT call.
- `auto_hangup_seconds` — force-terminate after this many seconds (safety net for stuck IVRs).
- `dedupe_chunks` — Cigna-only: skip the GPT call if this chunk is near-identical to the
  last one sent. Was needed because Cigna's STT had trailing-char races producing
  duplicate DETAILS / NEXT responses.

---

## 6. Prompt architecture

Each insurer has TWO templates:

1. **`{insurer}_prompt_template.txt`** — used for the general IVR conversation.
   Response formats GPT can return: `say:<phrase>`, `value:<value>`, `dtmf:<digit>`,
   `confirm:<yes/no>`, `endcall`, `fallback`, **`claim_mode`** (new safety-net signal).
   Templates get filled in with `{tax_id}`, `{npi}`, `{member_id}`, `{dob}`, `{dos}`,
   `{member_name}`, `{transcript}`.

2. **`{insurer}_claims_controller_template.txt`** — used ONCE claim_mode is active.
   Returns one of `DETAILS`, `NEXT`, `STOP`, `CONTINUE`. Has duplicate-prevention rule
   (don't send NEXT twice in a row — send CONTINUE between). Gets filled in with
   `{transcript_chunk}` and `{last_response}`.

**All three "spoken value" prompt templates** (cigna, humana, baylor_scott) have a
"CRITICAL: Alphanumeric ID Fidelity" section warning GPT NOT to substitute look-alike
characters (1↔I, 0↔O, 5↔S, etc.). See section 12.

Oscar and HealthFirst are **DTMF-only** — GPT types the ID digit-by-digit via keypad, no
letter/digit confusion possible, so no `value:` branch and no ID fidelity section.

**Adding a new insurer's prompt**: see section 16.

---

## 7. Configuration / env vars

Required at runtime (see `.env.example`):

| Var                              | Purpose                                                  |
|----------------------------------|----------------------------------------------------------|
| `WEBHOOK_BASE_URL`               | Public URL Telnyx sends webhooks to                      |
| `STREAM_BASE_URL`                | Public WebSocket URL for `/stream`                       |
| `TELNYX_API_KEY`                 | Telnyx REST API key                                      |
| `TEL_FROM`                       | Outbound "from" number                                   |
| `CALL_CONTROL_APP_ID`            | Telnyx Call Control application ID                       |
| `AZURE_SPEECH_KEY` / `REGION`    | Azure Cognitive Services Speech                          |
| `PTU_API_KEY` / `ENDPOINT` / `VERSION` | Azure OpenAI (PTU)                                 |
| `OPENAI_MODEL`                   | Azure deployment name (default `gpt-4.1`)                |
| `JWT_SECRET_KEY`                 | HS256 secret used to verify frontend Bearer tokens       |
| `PRACTICE_EHR_BASE_URL`          | PracticeEHR file upload                                  |
| `PRACTICE_EHR_AUTH_BASE_URL`     | Auth API (resolve per-customer api_key)                  |
| `CLINICAL_API_BASE_URL`          | Clinical /v1/Clinical/Billing-Agent/Visit                |
| `BILLING_AGENT_LOG_BASE_URL`     | Billing-Agent/Log CRUD                                   |
| `IVR_CLAIM_STATUS_BASE_URL`      | PATCH Ivr/ClaimStatus                                    |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights                                      |

Prod values live in `.env.prod` (gitignored). QA in `.env.qa` (gitignored).
`.env.example` is committed as a schema reference.

---

## 8. Branches (as of 2026-07)

| Branch                       | Purpose |
|------------------------------|---------|
| `main` (azure-new)           | Deployment branch — what runs in prod. |
| `develop` (azure-new)        | Active integration branch. **You work here.** |
| `refactor` (origin/azure)    | Legacy — where the big code split from monolith to `src/` happened. Superseded by develop. |
| `feature/uhc-claim-status`   | WIP: UHC (United Healthcare) claim-status endpoint + prompts. Not merged. |
| `denial-inquiry-wip`         | See below. |
| `oscar`                      | Old Oscar-specific branch — superseded. |
| `refactor-backup` / `refactor-test` / `refactor-template` | Old checkpoints. |

Remotes:
- `azure-new` → `https://dev.azure.com/PracticeEHR/PracticeEHR-AI/_git/PracticeEHR-AI-Billing-Agents` ← current
- `azure` → older Azure DevOps location (legacy)
- `origin` → github.com fork (legacy)

### The `denial-inquiry-wip` branch

Adds a **separate endpoint** `/v1/Denial-Inquiry/Call` for a DIFFERENT flow:
- **What it does**: automated calls to inquire about DENIED claims — talks to the payer
  representative to understand denial reasons and next steps, gathering PCP/authorization
  numbers/codes as needed.
- **Two-phase prompt architecture**: (a) IVR phase to reach a rep, (b) rep-phase for the
  actual conversation. Longer conversation history (12 turns vs 8) so the bot stays
  oriented through longer rep conversations.
- **Files added**: `src/api/v1/denial_inquiry.py` (207 lines), plus Humana-specific denial
  templates: `humana_denial_ivr_template`, `humana_denial_rep_template`.
- **Distinct required fields**: `visit_id`, `patient_name`, `dob`, `dos`, `billed_amount`,
  `member_id`, `plan`, `provider_npi`, `provider_name` (more than the claim-status flow —
  reps ask for provider info).
- **TTS voice on that branch**: `en-US-AndrewMultilingualNeural` (not Jenny). Also uses
  a manual PCM→μ-law conversion path (different from the SSML-driven claim-status flow).
- **Key commits**: `fb9ac38` (initial), `4e0893f` (voice + audio conversion),
  `0fbca57` (smarter prompt rules + 12-turn history).
- **Status**: not merged into develop. If we want to merge, we'll need to:
  1. Reconcile the TTS voice/audio-conversion divergence.
  2. Rebase on top of the new httpx singleton + STT ContextVar fixes.
  3. Bring the two-phase prompt architecture cleanly into the current file layout.

---

## 9. Deployment

- **Prod**: Azure App Service `as-ai-billing-agent-prod-cus` (Central US, Linux container).
- **Build**: `azure-pipelines.yml` triggers on `main`. Builds Docker image, pushes to
  ACR, deploys to App Service.
- **`Dockerfile`**: bullseye base with STT system deps, uvicorn entrypoint on port 5000.
- **Startup**: uvicorn runs `main:app`. `main.py` at import time calls `setup_logging()`,
  wires all routers with shared `active_calls` / `initiated_events` dicts, registers
  claims_agent callbacks, and mounts Application Insights.
- **App Service quirks**:
  - Anything on stderr is classified as ERROR by App Service. `BelowErrorFilter` in
    `logging_config.py` splits INFO/WARN → stdout, ERROR/CRIT → stderr.
  - SNAT ports are limited per instance — that's why we MUST use the singleton
    `httpx.AsyncClient` from `src/services/http_client.py`. Never create per-request
    clients.

---

## 10. Two-phase Billing-Agent/Log write

We reserve the DB row at CALL START so the frontend gets a `RefNo` immediately:

1. **POST** `Billing-Agent/Log` at call creation → row inserted with placeholders,
   `requestStatus="in_progress"`. Returns the row id (this becomes `RefNo`).
2. **PUT** `Billing-Agent/Log/{RefNo}` at call end (from `post_call_upload.py`) with
   the real `requestStatus` / `claimStatus` / description / uploaded file paths.

If the initial POST fails (`ref_no is None`), we fall back at call end to a single POST
so the call is still logged — the frontend won't have a RefNo to display, but the DB
record + downstream `Ivr/ClaimStatus` PATCH still happen.

---

## 11. Logging

Every log line is tagged with `c=<call_id> v=<visit_id> cid=<customer_id>` via ContextVars
in `logging_config.py`. Cost is a couple of dict lookups per record — not measurable.

Format:
```
[2026-07-21 14:32:11 - INFO - c=DthoBVG0 v=88231 cid=5567 - main.py - handle_user_speech] ...
```

KQL to filter by visit or customer in App Insights:
```kql
traces | where message contains "v=88231"
traces | where message contains "cid=5567"
```

Entry points that set the context:
- `orchestrate.py` — `set_call_id` + `set_visit_context` at call creation
- `webhooks.py` — restore from `active_calls` on each webhook
- `stream.py` — restore on WebSocket 'start'

The context propagates automatically into `asyncio.create_task` children (including the
post-call upload background task) so post-call logs are also tagged.

---

## 12. History — bugs we hit and how we handled them

Understanding these makes it easier to keep the invariants intact when adding features.

### 12.1 SNAT port exhaustion in prod (Adan's fix)
- **Symptom**: `ConnectTimeout` across every outbound API (Auth, Clinical, Billing-Agent/Log,
  Telnyx, GPT, PracticeEHR upload) — random, correlated with call volume.
- **Cause**: per-request `httpx.AsyncClient()` opened fresh TCP connections, exhausting the
  App Service instance's limited SNAT port pool.
- **Fix**: `src/services/http_client.py` — a process-wide singleton `AsyncClient` with a
  connection pool (max_connections=100, max_keepalive=20, short connect timeout so failed
  ports free fast). All service clients (Clinical, Auth, Billing-Log, IVR, Telnyx recording,
  TTS, uploader) use `get_http_client()` now. Never make a per-request client.
- **Bonus**: `stt_service.py` used to run `asyncio.run(asyncio.sleep(0.1))` 10×/sec per call
  (spinning up and tearing down a fresh event loop each time). Replaced with `time.sleep(0.1)`
  in the worker thread. Massive CPU improvement under load.

### 12.2 STT ContextVar propagation (Adan's fix + our diagnosis)
- **Symptom**: `RuntimeError: No active insurance config` firing dozens of times per
  concurrent call — but ONLY during active calls, not at cleanup.
- **Cause**: Old STT code created a pump task via `loop.create_task(self._process_events())`
  which then invoked `on_final` in ITS context — a context that never had `set_active_insurance`
  called in it. Everything downstream (`asyncio.create_task` for debounce, then
  `handle_user_speech`) inherited that empty context. Under a single call this sometimes
  "worked by accident" (stale ContextVar leftover). Under 6 concurrent calls the accident
  broke immediately.
- **Fix**: `stt_service.py::start_async_event_handler` now captures the WebSocket task's
  context (`contextvars.copy_context()`) at wire-up time. All SDK-thread callbacks dispatch
  through `_dispatch()` which schedules the coroutine on the loop **inside the captured
  context** (`loop.call_soon_threadsafe(_schedule, context=ctx)`). The debounce task and
  everything below it now inherit the correct `active_insurance`.

### 12.3 Late-STT-after-cleanup race (`KeyError: 'tax_id'`)
- **Symptom**: `KeyError: 'tax_id'` in `main.py` right after call cleanup — visible as
  "Task exception was never retrieved" in App Insights.
- **Cause**: STT can emit `on_final` AFTER `ensure_call_cleanup` popped the call from
  `active_calls`. The debounce task then fires `handle_user_speech`, which finds no
  call_state, defaults `visit_data` to `{}`, and `prompt.format(...)` crashes on the first
  `{placeholder}`.
- **Fix**: two layers.
  1. **Guard** at the top of `handle_user_speech` — if `call_state is None`, log a warning
     and return.
  2. **Cancel the debounce task** at the start of `ensure_call_cleanup`. We added a
     `debounce_task: Optional[object]` field to `CallState` and `stream.py::_reschedule_debounce`
     writes the task into it whenever it creates one. Cleanup cancels it before removing the
     call from `active_calls`.

### 12.4 GPT hallucinates member_id characters (U1 → UI)
- **Symptom**: Cigna call. `member_id` was `U16684575`, GPT spoke `UI6684575`. TTS spelled
  it correctly char-by-char — GPT was the substitution point.
- **Cause**: LLM tokenizer confuses look-alike alphanumeric characters (1↔I, 0↔O, 5↔S, 8↔B).
  Happens on alphanumeric IDs, doesn't happen on all-digit IDs (tax_id, npi) or structured
  formats (dates, names).
- **Fix**: two layers.
  1. **Prompt hardening** — added a "CRITICAL: Alphanumeric ID Fidelity" section to the
     top of each spoken-value template (cigna, humana, baylor_scott) telling GPT never to
     substitute look-alikes and to copy character-by-character from the Call Information block.
  2. **Post-process guard** in `llm_service.py::_correct_hallucinated_member_id` — when GPT
     returns `value:<val>`, if `val` is same-length + edit distance ≤ 2 from the authoritative
     `visit_data["member_id"]`, and only for alphanumeric IDs, we substitute the real value.
     Fires a WARN log. Exact matches pass through with no correction, no log.
- **Not touched**: TTS SSML routing, which already correctly spells alphanumeric IDs
  character-by-character.

### 12.5 "No claim" phrasings the real-time detector missed → call marked failed
- **Symptom**: IVR said "I couldn't find any claims on that date of service..." — a genuine
  successful outcome (payer confirmed no-claim). Real-time `is_claim_not_found` missed the
  phrasing. `finalized_claims` empty → fell into failure classifier → classified as
  "call failed" ❌.
- **Fix**: three layers of defense.
  1. **Broaden the real-time triggers** in `claims_helpers.py` — shorter substrings that
     match more variants; added `_normalize_for_match()` (folds curly apostrophes `’` → `'`,
     collapses whitespace).
  2. **Add "no claim" as a status in the failure classifier** — `_ALLOWED = {"no claim",
     "patient not found", "call failed"}`; new `_NO_CLAIM_FOUND_PATTERNS` checked at
     PRIORITY 0 (before patient-not-found); GPT prompt updated with "no claim" definition
     + priority rules.
  3. **Route "no claim" to SUCCESS** in `post_call_upload.py`'s else-branch — no longer
     hardcodes `REQUEST_STATUS_FAILED`; trusts the classifier's verdict.

### 12.6 "Claims found" phrasings the real-time detector missed → call marked failed
- **Symptom**: mirror of 12.5. IVR started reading claims but our `is_claim_start` didn't
  match. Call ended with no `finalized_claims` and no matched `NO_CLAIM_FOUND` pattern.
- **Fix**: `claim_mode` GPT-driven fallback. Added a new response format in the prompt:
  ```
  - claim_mode → the IVR just started reading claim details (e.g. "I found your claim",
    "I found 2 claims", "here's the first claim"). Return ONLY the exact word claim_mode.
  ```
  In `main.py::handle_user_speech`, after the GPT call, `_is_gpt_claim_mode_signal()` checks
  for it (strict-equality on compact form — no collision with any other format). If matched,
  calls the shared helper `_enter_claim_mode_and_forward()` — same wiring as the real-time
  `is_claim_start` path.
- **Defense in depth**: `llm_service.py::_process_llama_response` also silently swallows
  `claim_mode` (log-only, no action) so it can never fall through as "unrecognized".

### 12.7 Cigna NEXT trigger too narrow
- **Symptom**: Cigna IVR listing options said `"you can say repeat that fax the full list
  next item previous item or..."`. Our claims_controller prompt required `"press 3 for next
  item"` or `" 4 next claim"` — bare `"next item"` didn't match → GPT returned STOP → we
  stopped listing before all claims read.
- **Fix**: broadened PRIORITY 2 in `cigna_claims_controller_template.txt` to also accept
  bare `"next item"` and `"next claim"`. Added a matching example. Priority 4 STOP rule
  still says "NEVER RETURN STOP IF THERE ARE WORDS LIKE NEXT CLAIM, DETAILS" — reinforced.

### 12.8 IVR/ClaimStatus 400 (data seeding)
- **Symptom**: PATCH IVR/ClaimStatus returns `Success=false, Message='Update Ivr claim status
  operation failed.' ErrorCode=400` — even though our payload is correct.
- **Cause (working hypothesis)**: test visit_id is in the Clinical API but NOT in the parent
  Visits table that Ivr/ClaimStatus writes to. Oracle FK violation manifests as this generic
  400. Same call flow with a real production visit_id succeeds.
- **Action items**: (a) test with real visits; (b) enhance logging to include the full
  response body so future failures self-diagnose.

### 12.9 Port 5000 already in use
- **Cause**: previous `python main.py` still holding port on the dev machine.
- **Fix**: `netstat -ano | findstr :5000` → `taskkill /PID <pid> /F`.

### 12.10 Character-substitution history
- Old versions of `is_claim_start` had `"the first claim"` as a trigger. Cigna IVR sometimes
  says "if this is the first claim you filed with this tax ID..." while REJECTING the tax
  ID — false-positive fired claim_mode → call was logged as a "successful unknown" claim.
  Removed. See comment in `claims_helpers.py`.

---

## 13. Testing

- **`test_cigna_prompt.py`** (gitignored) — regression harness for the Cigna claims_controller
  prompt. Feeds handcrafted transcripts + `last_response` seeds through GPT and checks the
  one-word verdict (DETAILS/NEXT/STOP/CONTINUE). Extend by appending tuples to `CASES`.
  Run: `python test_cigna_prompt.py`. Requires `.env` with `PTU_API_KEY`/endpoint.
- **`test.py`** (gitignored) — legacy sandbox.
- No formal pytest suite yet. Manual testing is via real Telnyx calls to sandbox IVR lines,
  observing behaviour in App Insights.

---

## 14. Non-obvious quirks / gotchas

- **`is_tts_active` gates media forwarding**. While TTS is playing, we STOP feeding inbound
  audio into STT so the payer doesn't hear our own voice bounced back. Managed in
  `stream.py`. If you touch TTS or STT lifecycle, keep this invariant.
- **`ensure_call_cleanup` is idempotent + guarded by `cleanup_lock`**. Called from webhook,
  WebSocket finally, LLM `endcall`, auto_hangup, shutdown. All paths must remain safe to
  call multiple times.
- **Post-call upload is fire-and-forget** (`asyncio.create_task`). We hold a strong
  reference in `_pending_upload_tasks` so it isn't GC'd mid-flight. On shutdown, we
  `drain_pending_uploads(timeout=8s)` BEFORE closing the shared http client.
- **The claims_controller has a duplicate-prevention rule**: if last response was NEXT and
  the transcript says next again, return CONTINUE. Prevents infinite loops. Don't remove.
- **Cigna has `dedupe_chunks=True`**. If a new claim chunk is near-identical to the last
  one we sent to the claims_controller GPT, we skip the call. Fixed a race where trailing
  STT chars produced duplicate DETAILS/NEXT responses.
- **`raw_full_transcript` vs `finalized_claims`**: `finalized_claims` is populated by
  `claims_agent.end_session()` ONLY if claim_mode was ever entered. If we never entered
  claim_mode, `finalized_claims` is empty even if claim text is in `full_transcript`.
- **Never log PHI**: Clinical client is careful to log only visit_id, payer_id, plan_name,
  plan_description. Not member_id, member_name, DOB, DOS. Follow the same discipline for
  any new logging.
- **Log truncation at 500 chars**: HTTP-level failures log `resp.text[:500]`. If you're
  debugging a mystery error, increase temporarily.

---

## 15. How to add a new insurer

1. Add an `InsuranceConfig` entry to `INSURANCE_CONFIGS` in `src/config/insurance_config.py`
   with the phone number, timing knobs, and template names.
2. Add the payer_id → insurer mapping to `PAYER_ID_TO_INSURANCE` in the same file.
3. Add required fields to `src/services/clinical/required_fields.py` (which visit_data
   keys must be present for this insurer before we make the call).
4. Create `src/core/prompts/templates/<insurer>/`:
   - `<insurer>_prompt_template.txt` — main IVR conversation. Include the "CRITICAL:
     Alphanumeric ID Fidelity" section if the payer uses spoken member_ids. Add
     `claim_mode` to the response format list.
   - `<insurer>_claims_controller_template.txt` — the DETAILS/NEXT/STOP/CONTINUE prompt.
     Keep the duplicate-prevention rule.
5. Register the template names in `src/core/prompts/manager.py` and
   `src/core/prompts/claims_prompts.py`.
6. Test with `test_cigna_prompt.py` as a model — copy it to `test_<insurer>_prompt.py`,
   add to `.gitignore`, seed a few transcripts.

If the payer's flow is DTMF-only (no spoken IDs), skip the "Alphanumeric ID Fidelity"
section and the `value:` handling in the prompt (see Oscar / HealthFirst).

---

## 16. How to add a new feature

1. **Understand where in the flow it belongs** (section 3). Most features are one of:
   (a) new IVR prompt patterns → prompt template changes,
   (b) new status vocabulary → `classifier.py` / `failure_classifier.py` + downstream
   mapping in `claim_status_client.py`,
   (c) new post-call action → `post_call_upload.py`,
   (d) new endpoint → new file under `src/api/v1/` + router mount in `main.py`.
2. **Preserve invariants**:
   - Any new outbound HTTP call MUST use `get_http_client()` (never `httpx.AsyncClient()`).
   - Any code that touches `active_calls` after cleanup MUST guard against `None`.
   - Any new ContextVar-dependent logic MUST be reachable from the STT callback context
     (Adan's `contextvars.copy_context()` snapshot handles it; don't fight the pattern).
   - Never log PHI.
3. **Layer defense** if the feature depends on GPT/STT:
   - Real-time cheap detector first.
   - Post-call semantic classifier as backup.
   - Log both when they fire so you can KQL-measure the safety-net effectiveness.
4. **Test with the prompt harness** BEFORE running full calls. GPT calls are cheap, real
   Telnyx calls are not.
5. **Add App Insights KQL queries** for the new signals to `# Verification path` sections
   in the commit message so ops can monitor.

---

## 17. Useful KQL queries

```kql
// All logs for one call
traces | where message contains "c=DthoBVG0" | order by timestamp asc

// All logs for one visit
traces | where message contains "v=88231" | order by timestamp asc

// GPT member_id hallucination corrections
traces | where message contains "GPT hallucinated member_id: sent"

// No-claim caught by post-call safety net (real-time detector missed)
traces | where message contains "'no claim' (matched" and message contains "post-call safety net"

// GPT-driven claim_mode fallback (real-time is_claim_start missed)
traces | where message contains "GPT signaled claim_mode (safety net"

// IVR/ClaimStatus PATCH failures
traces | where message contains "IVR/ClaimStatus Success=false"

// Auto-hangups (calls exceeded max duration)
traces | where message contains "auto_hangup" | summarize count() by cloud_RoleInstance, bin(timestamp, 1h)
```

---

## 18. Contact / handoff

- **Repo**: https://dev.azure.com/PracticeEHR/PracticeEHR-AI/_git/PracticeEHR-AI-Billing-Agents
- **Prod App Service**: `as-ai-billing-agent-prod-cus` (Central US)
- **Other dev on repo**: Adan Abbas (Azure DevOps)
- **Backend team** (PracticeEHR APIs, visit seeding): Waseem
- **Frontend integration**: consumes `/v1/Billing-Agent/Call`, displays `RefNo` returned in
  the response, then polls Billing-Agent/Log for the final outcome.
