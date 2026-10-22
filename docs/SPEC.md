# PropPay Agent — Product & Technical Spec

## 1. Purpose
A portfolio POC that shows how agentic AI can run a **US domestic B2B accounts-payable payment run** for a real estate operator safely. The target audience is hiring teams at B2B payments companies such as Redpin and RealPage.

**What it should prove**
1. Agents can take invoices from intake to reconciled payment with a high straight-through-processing (STP) rate.
2. Controls come first: vendor-impersonation fraud is caught (aligned with Nacha's 2026 fraud-monitoring rules), approvals are enforced, and every decision is auditable.
3. Payments domain fluency: rail economics (ACH / same-day ACH / RTP / FedNow / wire / virtual card / check), NACHA files, positive pay, return codes and reconciliation.

**Non-goals**
- No real money, no real bank or processor integrations (Stripe test mode is optional).
- No cross-border or FX payments.
- No multi-tenant, auth or production hardening.

## 2. Setup (macOS, 18GB RAM)
```bash
# Tooling
brew install ollama uv
ollama serve &                        # or run the Ollama app
ollama pull qwen3:8b                  # check ollama.com/library for the newest Qwen instruct model; 14B if reasoning is shaky
ollama pull nomic-embed-text          # embeddings (vendor-name similarity)

# Project
uv init proppay-agent && cd proppay-agent
uv add langgraph langchain-ollama langgraph-checkpoint-sqlite sqlmodel pydantic \
       fastapi uvicorn httpx streamlit pandas plotly pdfplumber reportlab faker \
       pyyaml rapidfuzz python-dotenv
uv add --dev pytest
```

`.env`
```
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
EMBED_MODEL=nomic-embed-text
DB_PATH=data/proppay.db
MOCK_BANK_URL=http://localhost:8001
MAX_AGENT_STEPS=5
```

## 3. Architecture
```
 Invoice PDFs / "vendor emails"
            │
            ▼
 ┌──────────────────── LangGraph pipeline (per invoice) ────────────────────┐
 │ 1 Intake ─► 2 Match (3-way) ─► 3 Fraud monitor ─► 4 Rules engine ──┐     │
 │                                                                    │     │
 │             ┌── needs approval? ── interrupt() ◄── Streamlit inbox ┘     │
 │             ▼                                                            │
 │ 5 Rail & timing optimizer ─► 6 Batch & execute ─► 7 Reconcile            │
 └──────────────────────────────────────────────────────────────────────────┘
            │                         │                       │
         SQLite                Mock Bank (FastAPI)      BAI2-style statement
   (vendors, invoices,       (ACH file intake, RTP,     + R-code returns
    payments, audit_log)      wire, card, positive pay)
```
Vendor onboarding (W-9 / TIN match / OFAC / account validation) runs as a separate small graph, triggered when a vendor is created or changes bank details.

## 4. Repo structure
```
proppay-agent/
├── CLAUDE.md
├── README.md                 # demo script + talking points
├── .env
├── config/
│   ├── rails.yaml            # limits, fees, cutoffs, settlement times
│   ├── policy.yaml           # approval thresholds, auto-pay rules
│   └── fraud.yaml            # signal weights, risk-score thresholds
├── data/
│   ├── invoices/             # generated PDFs
│   ├── emails/               # generated vendor emails (.txt / .eml)
│   ├── ofac/sdn.csv          # OFAC SDN list (or seeded sample)
│   └── proppay.db
├── app/
│   ├── models.py             # SQLModel tables
│   ├── schemas.py            # Pydantic LLM I/O schemas
│   ├── llm.py                # ChatOllama factory + structured-output helper with retry
│   ├── prompts/              # *.txt prompt templates
│   ├── agents/
│   │   ├── intake.py
│   │   ├── matcher.py
│   │   ├── fraud.py
│   │   ├── rail_optimizer.py
│   │   ├── executor.py
│   │   ├── reconciler.py
│   │   └── onboarding.py
│   ├── rules/
│   │   ├── policy.py         # approval decisions (deterministic)
│   │   ├── fraud_signals.py  # deterministic signal detectors
│   │   └── rails.py          # eligibility + cost calculation
│   ├── payments/
│   │   ├── nacha.py          # NACHA CCD+ file builder
│   │   ├── positive_pay.py
│   │   ├── routing.py        # ABA checksum validation
│   │   └── bai2.py           # simplified BAI2 parser
│   ├── graph.py              # LangGraph wiring + checkpointer
│   ├── audit.py
│   └── metrics.py
├── mock_bank/
│   └── main.py               # FastAPI mock bank
├── ui/
│   ├── Home.py               # dashboard
│   └── pages/
│       ├── 1_Payment_Run.py
│       ├── 2_Approvals.py
│       ├── 3_Vendors.py
│       ├── 4_Audit_Trail.py
│       └── 5_Reconciliation.py
├── scripts/
│   ├── seed_data.py          # vendors, POs, work orders, invoices, demo cases
│   └── make_invoice_pdfs.py
├── evals/
│   ├── gold_invoices.jsonl   # ground-truth extraction labels
│   └── run_extraction_eval.py
└── tests/
```

## 5. Data model (SQLite)
| Table | Key fields |
|---|---|
| `properties` | id, name, address, units |
| `vendors` | id, legal_name, dba, tin (masked), w9_received, tin_match_status, ofac_status, category, card_accepting, trusted (bool), created_at |
| `vendor_bank_accounts` | id, vendor_id, routing, account_last4, account_hash, account_type (checking/savings), validated, active, effective_from, source (onboarding / change_request) |
| `bank_change_requests` | id, vendor_id, requested_via (email/portal/phone), new_routing, new_account_hash, status (pending_callback / verified / rejected), callback_done_by |
| `purchase_orders` | id, vendor_id, property_id, amount_cents, status |
| `work_orders` | id, po_id, status (open / completed), completed_at |
| `invoices` | id, vendor_id, invoice_number, invoice_date, due_date, amount_cents, terms (e.g. `2/10 net 30`), po_number, remit_bank (as stated on invoice), source_file, status, risk_score, extracted_json |
| `payments` | id, invoice_id, rail, amount_cents, fee_cents, scheduled_date, status (scheduled / pending_approval / sent / settled / returned / blocked), idempotency_key, batch_id, return_code |
| `payment_batches` | id, rail, file_path, created_at, total_cents, entry_count |
| `approvals` | id, payment_id, required_level (single/dual), approver_1, approver_2, decision, reason, decided_at |
| `bank_statement_lines` | id, date, amount_cents, type_code, reference, matched_payment_id |
| `audit_log` | id, ts, actor, entity, entity_id, action, before_json, after_json, reason |

## 6. Agents

### 6.1 Intake agent
- **Input:** invoice PDF (via `pdfplumber` text) or vendor email text.
- **Output (`InvoiceExtraction`):** vendor_name, invoice_number, invoice_date, due_date, amount, currency, terms, po_number, line_items[], remit_to_bank {routing, account_last4} | null, email_mentions_bank_change (bool), confidence per field.
- **Rules:** validate the routing number with the ABA checksum (weights 3,7,1). Totals must equal the sum of line items. Low-confidence fields go to human review.

### 6.2 Match agent (3-way match)
- Resolve the vendor with fuzzy match (rapidfuzz) plus embedding similarity on name.
- Check invoice ↔ PO (amount within tolerance, default 2%) ↔ work order completed.
- **Output:** match_status (full / partial / none) with reasons.

### 6.3 Fraud monitoring agent
Deterministic detectors in `rules/fraud_signals.py`, each returning (signal, weight, evidence):

| Signal | Example |
|---|---|
| `BANK_DETAILS_CHANGED` | Invoice remit-to bank ≠ active vendor account |
| `BANK_CHANGE_VIA_EMAIL` | Email requests new bank details (BEC pattern) |
| `LOOKALIKE_VENDOR` | "Acme Plumbing LLC" vs "Acme Plumbimg LLC", or a look-alike email domain |
| `DUPLICATE_INVOICE` | Same vendor + amount + date within N days, or invoice number differs only by suffix or punctuation |
| `AMOUNT_ANOMALY` | > 3σ above vendor's historical mean |
| `FIRST_TIME_PAYEE` | No prior settled payments |
| `NEW_ACCOUNT_RECENT` | Bank account changed < 30 days ago |
| `URGENCY_LANGUAGE` | LLM-detected pressure ("pay today or work stops") |
| `OFAC_HIT` | Name match against the SDN list (hard block) |

The LLM summarizes these signals into a plain-English rationale for the reviewer. Risk score = weighted sum, with thresholds from `config/fraud.yaml`. **Any bank-detail change forces a call-back verification task before payment, regardless of score.**

### 6.4 Rules engine (`rules/policy.py`)
Example `config/policy.yaml`:
```yaml
auto_pay:
  max_amount_cents: 500000          # $5,000
  require_trusted_vendor: true
  require_full_match: true
  max_risk_score: 20
dual_approval:
  min_amount_cents: 5000000         # $50,000
  rails_always_dual: [wire]
  instant_min_amount_cents: 2500000 # RTP/FedNow ≥ $25k → dual
hard_blocks: [OFAC_HIT]
hold_until_callback: [BANK_DETAILS_CHANGED, BANK_CHANGE_VIA_EMAIL]
```
**Output:** `AUTO_APPROVE | SINGLE_APPROVAL | DUAL_APPROVAL | HOLD_CALLBACK | BLOCK`, plus reasons.

### 6.5 Rail & timing optimizer
Example `config/rails.yaml` (illustrative values; keep them configurable):
```yaml
ach_standard: { fee_cents: 25,   settle_days: 1, max_cents: null,      cutoff: "17:00" }
ach_same_day: { fee_cents: 100,  settle_days: 0, max_cents: 100000000, cutoff: "14:45" }
rtp:          { fee_cents: 50,   settle_days: 0, max_cents: 1000000000, always_on: true }
fednow:       { fee_cents: 45,   settle_days: 0, max_cents: 1000000000, always_on: true }
wire:         { fee_cents: 2500, settle_days: 0, max_cents: null,      cutoff: "16:30" }
virtual_card: { fee_cents: 0, rebate_bps: 100, requires: card_accepting }
check:        { fee_cents: 300,  settle_days: 5, positive_pay: true }
```
Logic (deterministic, with an LLM-written explanation):
1. Filter eligible rails (vendor enrollment, limits, cutoff).
2. Pick the payment date: capture an early-pay discount if `discount > cost of capital`; otherwise pay on the due date minus settlement days.
3. Choose the cheapest rail that meets the date. Instant rails only when urgent **and** risk is low, because they're irrevocable.
4. Record the fee, discount captured and rebate earned.

### 6.6 Execution
- Group ACH payments into a batch and build a **NACHA file** (`payments/nacha.py`):
  - 94-character fixed-width records: `1` file header, `5` batch header (SEC code **CCD**), `6` entry detail (transaction code **22** checking credit / **32** savings credit), `7` addenda type **05** carrying the invoice number for remittance (CCD+), `8` batch control, `9` file control.
  - Entry hash = sum of the first 8 digits of the receiving routing numbers (rightmost 10 digits). Pad to a blocking factor of 10 with `9…9` lines.
  - Unit-test the record lengths, control totals and hash.
- Checks: write a positive-pay CSV (account, check #, amount, issue date, payee).
- RTP, FedNow, wire and card: POST to the mock bank with an `Idempotency-Key` header.

### 6.7 Mock bank (`mock_bank/main.py`)
- `POST /ach/files` accepts a NACHA file and validates its structure. It schedules settlement and may return R-codes for seeded cases (`R01` insufficient funds, `R02` account closed, `R03` no account / unable to locate, `R04` invalid account number).
- `POST /instant` (rail = rtp | fednow), `POST /wire`, `POST /card`: idempotent (same key returns the same response).
- `GET /statement?date=` returns a simplified BAI2-style statement (CSV or BAI2 text).
- `POST /account-validation` and `POST /tin-match` are mocks with seeded outcomes.

### 6.8 Reconciliation agent
Match statement lines to payments by amount, date window and reference. Unmatched items and returns go to an exceptions queue. On a return, the invoice reopens, the vendor is flagged, and the LLM drafts a vendor notice.

### 6.9 Vendor onboarding (side graph)
W-9 captured → TIN match (mock) → OFAC screen (real SDN CSV with fuzzy name match) → bank account validation (mock) → `trusted` is set only after 3 clean settled payments.

## 7. LangGraph design
- `State`: invoice_id, extraction, match, signals, risk_score, decision, rail_plan, payment_id, errors, step_count.
- Nodes map 1:1 to the agents. Conditional edges on `decision`.
- Human approval uses `interrupt()`. Streamlit resumes the thread with `Command(resume={...})`.
- Use the SQLite checkpointer so approvals survive restarts.
- Process the run as a batch: fan out per invoice, then join into the rail/batch step.

## 8. Synthetic data and seeded demo cases
`scripts/seed_data.py` (Faker, fixed seed) creates a 12-property portfolio, about 30 vendors (plumbing, HVAC, landscaping, cleaning, security, utilities, a general contractor), POs, work orders, 6 months of payment history, and **25 invoices for "Monday's AP run"**. Required demo cases:

| # | Case | Expected outcome |
|---|---|---|
| 1 | 18 routine invoices, trusted vendors, < $5k | Auto-approve → standard ACH batch |
| 2 | Plumbing vendor email: "we've changed banks, please update", from a look-alike domain | BLOCK / HOLD_CALLBACK. Fraud rationale shown |
| 3 | Same landscaping invoice resubmitted as `INV-1043-A` | Duplicate flagged |
| 4 | Emergency HVAC repair, $3,800, due today, clean history | RTP, sent instantly |
| 5 | GC construction draw, $480,000 | Dual approval → wire |
| 6 | Two card-accepting vendors | Virtual card, rebate recorded |
| 7 | One ACH payment returns R03 | Reconciliation exception, invoice reopened, vendor notice drafted |
| 8 | Invoice with `2/10 net 30` terms | Paid early, discount captured |
| 9 | Vendor name close to an SDN entry (seeded sample) | OFAC hard block |

Also generate **invoice PDFs with varied layouts** (`reportlab`, 3–4 templates) so extraction is a real test.

## 9. UI (Streamlit)
- **Home / Dashboard:** STP rate, $ paid by rail, fees vs an all-wire baseline, discounts captured, card rebates, fraud prevented ($ and count), exceptions open.
- **Payment Run:** "Run Monday AP" button with live per-invoice status as it moves through the graph.
- **Approvals inbox:** risk card showing signals, LLM rationale and the rule that triggered it, plus Approve/Reject buttons. Dual approval needs two different users (simulate with a user selector).
- **Vendors:** onboarding status, bank-change requests and a call-back verification button.
- **Audit Trail:** filterable log showing who or what did what, and why.
- **Reconciliation:** matched vs exceptions, and return handling.

## 10. Metrics (`app/metrics.py`)
- **STP rate** = invoices paid with no human touch ÷ total invoices
- **Fraud prevented ($)** = sum of blocked or held payments later confirmed fraudulent (seeded)
- **Rail savings** = Σ(wire fee − chosen rail fee)
- **Discounts captured** and **card rebates** ($)
- **Extraction accuracy** (from evals), field-level

## 11. Evals and testing
- `tests/`: NACHA builder (lengths, totals, hash, padding), ABA checksum, policy decisions per demo case, rail selection, duplicate detection, mock-bank idempotency.
- `evals/`: 25–50 labeled invoices in `gold_invoices.jsonl`. Report field-level precision and recall and the end-to-end decision accuracy against expected outcomes. Compare 8B vs 14B models and record the results in the README. This is a strong PM talking point.

## 12. Build plan (phases with acceptance criteria)
| Phase | Scope | Done when |
|---|---|---|
| 0 | Scaffold, config, models, `llm.py` with structured output + retry | `pytest` passes; Ollama round-trip returns a valid Pydantic object |
| 1 | Seed data + invoice PDFs + emails | DB populated; 25 PDFs generated covering all 9 cases |
| 2 | Intake + match agents + extraction eval | ≥ 90% field accuracy on gold set |
| 3 | Fraud signals + rules engine | Every demo case gets its expected decision (unit-tested) |
| 4 | Rail optimizer + NACHA/positive-pay builders + mock bank | Valid NACHA file; idempotent instant payments |
| 5 | LangGraph wiring with interrupts | End-to-end run pauses for approvals and resumes |
| 6 | Reconciliation + returns | R03 case reopens the invoice; statement fully matched otherwise |
| 7 | Streamlit UI + dashboard | Full demo script runs in < 5 min |
| 8 | README, architecture diagram, demo video (Loom), evals table | Repo is portfolio-ready |

## 13. README talking points (for interviews)
- "LLMs triage and explain; deterministic rules move money."
- Aligned with Nacha's 2026 risk-based fraud-monitoring requirements for ACH credit originators, with a focus on payments made under false pretenses (vendor impersonation / BEC).
- Rail economics: instant payments are irrevocable, so fraud checks gate them.
- Idempotency, audit trail and dual control are first-class features.
- Measured, not vibes: extraction evals plus decision accuracy, model-size trade-off.
- Zero cost, fully local: no data leaves the machine, which matters for payments data.

## 14. Stretch ideas
- Agent mandates modeled on AP2 (signed "intent mandates" limiting what an agent may pay).
- Vendor self-service portal chat agent for "where's my payment?"
- Cash-forecast agent that sequences the payment run against an operating-account balance.
- Stripe test-mode integration for the card rail.
