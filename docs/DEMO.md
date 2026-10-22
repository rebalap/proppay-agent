# PropPay Agent: 5-minute demo script

Rehearsed end to end on 2026-10-16 in **~3m40s**; script updated for the redesigned UI (the live agent run takes 60-90 s of that).

## Before the demo (once, ~7 minutes)

```bash
ollama serve                                         # or open the Ollama app
uv run uvicorn mock_bank.main:app --port 8001        # terminal 1
uv run python scripts/prepare_demo.py                # terminal 2: seed + "overnight" batch + snapshot
uv run streamlit run ui/Home.py                      # terminal 2, after prepare finishes
```

`prepare_demo.py` processes 22 of Monday's 25 invoices ahead of time (the "overnight batch") and
keeps three for the live run. Between rehearsals reset in ~1 second with:

```bash
uv run python scripts/prepare_demo.py --restore
```

Then reload the browser tab.

## The script

| Time | Page | Do | Say |
|---|---|---|---|
| 0:00 | **Home** | Read the summary box, point at *At a glance* | "A property manager's Monday run: 25 vendor invoices across 12 properties. Overnight the agents already handled 22: 91% with no human touch, $4.50 in bank fees instead of $500 by wire." |
| 0:30 | **Home** | Click **Process them →** in *Needs your attention* | "Three invoices arrived this morning. Watch each one get read, matched, fraud-checked and decided. It's a local 8B model on this laptop; nothing leaves the machine." |
| 0:40 | **Payment Run** | Click **▶ Start processing (3)** | |
| 0:45 | | While it runs (~60-90 s) | "The golden rule: AI reads and explains, written rules decide whether money moves. The model can't approve anything." |
| 2:00 | | Read the three result cards | "An emergency HVAC repair, due today, clean vendor: paid instantly. A supplier on the sanctions list: stopped. And a plumbing invoice that came by email…" |
| 2:15 | **Approvals** | Switch *You are acting as* to **Dana**. Point at the table: *Needs · Key issue · Risk* | "Everything that needs a person, one row each. The top one: Acme Plumbing's **new bank details by email**, from acmeplum**bimg**.com." |
| 2:30 | | Click **👁** on the Acme row (side panel opens) | "Each red flag comes with evidence; the AI analyst explains, but doesn't decide. The list stays right there behind the panel." |
| 2:45 | | **📞 Record call-back…** → *They did NOT change banks* → **⛔ Stop this payment** | "Any bank change means phoning the vendor on the number we already have, whatever the score. They never changed banks, so it's stopped." |
| 3:00 | | **✓** on the Greenleaf duplicate → **Approve $1,893.00** | "A resubmission of an invoice we already paid. Some accounting systems do reuse numbers, so a person decides. Rejecting would ask for a reason." |
| 3:10 | | **✓** on the $480k Summit wire → **Approve $480,000.00**, switch to **Bob**, approve again | "Over $50,000 and by wire: two different people. The confirmation says exactly what moves, and Dana's ✓ is disabled after her approval." |
| 3:25 | **Invoice Details** | Pick *Acme Plumbing* (or click **See full invoice story**) | "One invoice's whole story: what the AI read, every check, the red flags, the decision in plain English, and a timeline." |
| 3:45 | **Reconciliation** | Click **🔍 Check statements** (up to 20 Nov) | "Fast-forward a month: the bank's statements. Every transaction matched. One payment bounced: the vendor's account doesn't exist. The agents reopened the invoice, flagged the vendor and drafted an email asking them to confirm by phone, never by email." |
| 4:15 | **Audit Trail** | *Show actions by: People* | "Every action, by an agent or a person, in plain English, with exactly what changed." |
| 4:30 | **Home** | | "80% handled with no human touch, a $2,840 fraud stopped, a sanctions hit blocked, $520 saved on bank fees and a 2% early-payment discount captured." |

## If something goes wrong

- **"Mock bank is not running"** in the sidebar: start terminal 1, then `prepare_demo.py --restore`.
- **The live run is slow**: Ollama is loading the model. Run any invoice once before the demo to warm it.
- **Wrong state**: sidebar → *Demo controls* → **↺ Reset the demo** (or `uv run python scripts/prepare_demo.py --restore`).
