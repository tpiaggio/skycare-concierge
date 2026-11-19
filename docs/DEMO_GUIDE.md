# SkyCare Concierge Live Demo & Walkthrough Guide

This walkthrough demonstrates every production safeguard, model routing tier, HITL gate, PII masking control, and cross-session memory behavior using the two clean light-mode interfaces:

- **Passenger Recovery App (`/` & `/ui`)**: `http://127.0.0.1:8000/` — minimal bilingual (`EN` / `ES`) traveler chat with 3 realistic starters, automatic HITL live-sync polling, and a quiet **`Demo ▾`** script menu tucked beneath the composer.
- **Airline Supervisor Desk (`/supervisor`)**: `http://127.0.0.1:8000/supervisor` — auto-polling HITL escalation queue (`Approve` / `Deny`), live 3-row **System Safeguards** panel, and **Reset sample flights** (`POST /api/demo/reset`).

## Seeded Operational PNRs

The SQLite `ReservationRepository` (`app/storage/repository.py`) seeds four deterministic airline cases on startup:

| PNR | Passenger | Tier | Route | Disrupted Segment | Jurisdiction | Recommended Demo |
| --- | --- | --- | --- | --- | --- | --- |
| `SKY842` | Elena Rostova | Gold | `JFK -> LHR -> ZRH` | `SC402` Cancelled (Crew Timeout / Storm) | `UK_EU261` | Pro Planner optimization & EU261 EUR 600 check |
| `SKY319` | Marcus Vance | Silver | `ORD -> SFO -> NRT` | `SC118` Delayed 200m (Mechanical APU) | `US_DOT` | High-value `$210` hotel voucher HITL approval gate |
| `SKY905` | Priya Nair | Platinum | `ATL -> BOS` | `SC214` Delayed 75m (ATC Weather) | `US_DOT` | Low-value `$35` meal voucher auto-approval |
| `SKY404` | David Kim | Blue | `LAX -> SEA` | None (`SC510` On Time) | `US_DOT` | Negative control (no false compensation) |

---

## Scenario 1: Flash Triage -> Pro Strategic Planning -> HITL Rebooking (`SKY842`)

1. Open `/` (`http://127.0.0.1:8000/`), keep `EN` (or switch to `ES`), and click the first welcome card (`My flight SC402 was cancelled`) or select **Demo ▾ -> `1. SKY842 EU261 + Pro Rebook`**.
2. **Observed Flow**:
   - `skycare_concierge` (`gemini-3.8-flash`, `LOW`) pre-warms `SKY842` data and confirms EUR 600 UK/EU261 cash compensation eligibility, `$220` hotel allowance, `$45` meal allowance, and involuntary fare waiver.
   - For itinerary trade-offs, it delegates to `recovery_planner_agent` (`gemini-4-hydrogen`, `LOW`, hedged) -> evaluates nonstop `SC408` (`JFK -> ZRH`) vs 1-stop `SC410 -> SC822` (`JFK -> CDG -> ZRH`) and `SC404 -> SC814` (`JFK -> LHR -> ZRH`), recommending nonstop `SC408` with zero misconnect risk and aisle/vegetarian preference fit.
3. Optional HITL rebook commit: ask *"Book Elena on SC408 with an aisle seat and waived fare difference."*
   - `passenger_care_agent` calls `execute_passenger_rebooking` -> pauses cleanly with `APR-...` and shows a waiting banner on `/`.
4. Switch to `/supervisor` (`http://127.0.0.1:8000/supervisor`) and click **Approve** -> `/` automatically detects the decision within ~2s, finishes the booking, and displays confirmation `RBK-...`.

## Scenario 2: Policy Threshold Auto-Approve vs. Supervisor HITL (`SKY905` vs `SKY319`)

1. On `/`, click **Demo ▾ -> `SKY905 $35 Meal (Auto-Approve)`** (or the third starter card):
   - Amount `<= $150.00` auto-issues immediately with confirmation `VCH-...`.
2. Next, select **Demo ▾ -> `2. SKY319 $210 Hotel HITL`** (`$210` > `$150` policy cap):
   - `issue_hotel_meal_voucher` blocks immediate issuance, creates a `PENDING` approval request (`APR-...`), and `/` enters a calm waiting state.
   - Open `/supervisor` side-by-side (`http://127.0.0.1:8000/supervisor`), inspect Marcus Vance's `$210` card (`ORD -> SFO -> NRT`), and click **Approve** (or **Deny**).
   - Watch `/` automatically update live without refreshing and issue the final `VCH-...` hotel voucher (replay attempts are deterministically blocked with `409 Conflict`).

## Scenario 3: Deterministic PII Masking & Async Memory Bank Recall

1. On `/`, select **Demo ▾ -> `3a. Save Preferences + PII Shield`** (*"Please remember I always prefer aisle seats and vegetarian meals. My email is elena@example.com and card is 4111-1111-1111-1111."*).
2. On `/supervisor`, inspect **System Safeguards -> PII Redaction Shield**:
   - The shield badge updates to **`Masked ✓`** showing redacted categories without storing plaintext email or card numbers (`[REDACTED_EMAIL]`, `[REDACTED_CARD]`).
   - `AsyncMemoryCoordinator` asynchronously indexes the sanitized preferences (`aisle seats`, `vegetarian meals`) into Memory Bank.
3. On `/`, select **Demo ▾ -> `3b. Fresh Chat → Recall Memory`** (starts a brand-new session with zero chat history for `elena_gold`) -> `PreloadMemoryTool` recalls Elena's saved preferences cleanly without any leaked PII.

## Scenario 4: Adversarial Prompt Injection Guardrail

1. On `/`, select **Demo ▾ -> `4. Prompt Injection Defense`**.
2. `SkyCareGuardrailsPlugin.on_user_message_callback` + `before_run_callback` halts execution prior to any tool or model call, resets `is_user_prompt_safe = True` to avoid session poisoning, returns a localized refusal, and updates **System Safeguards -> Prompt Injection Guardrail** on `/supervisor` to **`Blocked ✓`**.

---

## Scenario 5: Bilingual UI & Agent Verification (`EN` / `ES`)

1. Switch the top-right **Language selector** from **EN** to **ES** on either `/` or `/supervisor` (persisted via `skycare_lang` in `localStorage`, or via `?lang=es`).
2. Verify that welcome starters, the quiet `Demo ▾` scripts, waiting banners, `/supervisor` cards (`Aprobar` / `Denegar`), and **Controles del Sistema** switch cleanly to Spanish while keeping `SKY842`, `SC402`, and `APR-...` codes intact.
3. Run Demo scripts `1` (`SKY842`) and `2` (`SKY319`) + `/supervisor` approval in Spanish and confirm end-to-end streaming, HITL live-completion, and Spanish safety guardrails.

---

## Easy Production Testing Guide (Before Final Submission)

### Clean Reset Between Demo Takes

- Click **Reset sample flights** (`Restaurar vuelos de demo` in ES) in the top-right of `/supervisor` (or send `POST /api/demo/reset`) to wipe pending/resolved approvals, vouchers, rebookings, and in-memory audit events while restoring all 4 seeded PNRs cleanly.

### Option A: Direct Browser SSO via Identity-Aware Proxy (Recommended)

1. Open both views side-by-side in Chrome using an authorized Argolis identity (`admin@tpiaggio.altostrat.com` or `tpiaggio@gcp.altostrat.com`):
   - Passenger App: `https://skycare-concierge-515119749590.us-east1.run.app/`
   - Supervisor Desk: `https://skycare-concierge-515119749590.us-east1.run.app/supervisor`
2. Complete the Google IAP login prompt; run Scenarios 1–5 seamlessly across the two routes.

### Option B: Authenticated Local Proxy Tunnel (For `curl` / A2A / CLI Smoke Checks)

```bash
export CLOUDSDK_CONFIG="$HOME/.config/gcloud-argolis"
export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE="false"
gcloud run services proxy skycare-concierge \
  --project skycare-concierge-tp01 \
  --region us-east1 \
  --port 8080
```

In a second terminal:

```bash
curl -sS http://localhost:8080/api/status | python3 -m json.tool
curl -sS http://localhost:8080/a2a/app/.well-known/agent-card.json | python3 -m json.tool
```

